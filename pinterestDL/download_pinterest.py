#! /usr/bin/env python

import concurrent.futures
from datetime import datetime
import os
from shutil import which
import signal
import sys
from time import sleep

import argparse
from bs4 import BeautifulSoup
from PIL import Image
from selenium import webdriver
import urllib.request

from memory_set import MemorySet

"""Use this script to download pinterest pages or boards. Requires python >= 3.6 and selenium chrome driver in $PATH."""

def find_num_pins(body):
    """
    :param body: the HTML body of a pinterest page.
    :returns the number of pins in the pinterest board, or infinity if a tag page was given.
    """
    spans = body.find_elements_by_tag_name("span")
    num_elements = float("inf")  # If we download from a tag page, return as many as possible
    for span in spans:
        if "Pins" in span.text:
            num_elements = int(span.text.split(" ")[0])
            break

    return num_elements


def find_board_name(board_url):
    """
    :param board_url: The url of a pinterest page.
    :returns the name of that page. If it is a board, that is the name of the board, otherwise the list of tags used.
    """
    if "?q=" in board_url:
        # We're downloading a tag page, find the search tags in the url
        name_start = board_url.index("=") + 1
        name_end = board_url.index("&")
        return board_url[name_start:name_end]
    else:
        # We're downloading a board, extract the title
        name_idx = -1
        if board_url[-1] == "/":
            name_idx = -2
        return board_url.split("/")[name_idx]


def find_high_res_links(body):
    """
    :param body: The body of a pinterest html page.
    :returns a list of links to the image sources of the pins in the body element.
    """
    soup = BeautifulSoup(body.get_attribute("outerHTML"), "html.parser")
    low_res_imgs = soup.find_all("img")
    return [img["src"] for img in low_res_imgs], len(low_res_imgs)


def retrieve_bord_info(board_url, download_folder, body, num_pins=None, board_name=None):
    """
    Collects some useful information from a pinterest page and changes the user input on the command line
    to follow the requirements of the script.
    :param board_url: URL of a pinterest page.
    :param download_folder: Folder into which to download the pins.
    :param body: HTML body of that page.
    :param num_pins: The number of pins to download, if supplied by the user.
    :param board_name: The name of the board, if supplied by the user.

    :returns the board name to be used, the number of pins to download, the folder to store the pins in.
    """
    if board_name is None:
        board_name = find_board_name(board_url)

    # Find the number of pins to download, minimum between available pins and requested pins
    num_available_pins = find_num_pins(body)
    if num_pins is None:
        num_pins = num_available_pins
    else:
        num_pins = min(num_available_pins, num_pins)

    # Choose the destination folder so that we can download into an existing folder
    if os.path.basename(download_folder) != board_name:
        download_folder = os.path.join(download_folder, board_name)
    os.makedirs(download_folder, exist_ok=True)

    return board_name, num_pins, download_folder


def _get_size_verifier(min_x, min_y, mode):
    """
    Depending on what the user wants, we need to filter image sizes differently.
    This function generates the filter according to the user's wishes.
    :param min_x: Minimal x-coordinate length of image.
    :param min_y: Minimal y-coordinate length of image.
    :param mode: If equal to 'area': Only filter images whose area is below min_x*min_y.
                 If equal to 'individual' or anything else: Both sides of the image must be bigger than the
                 given x and y coordinates.
    :returns function that decides wether an image should be kept or discarded according to the size constraints.
    """
    def by_area(width, height):
        return width * height >= min_x*min_y
    def by_both(width, height):
        return width >= min_x and height >= min_y
    def anything_goes(width, height):
        return True
    if mode == "area":
        return by_area
    elif mode == "individual":
        return by_both
    else:
        return anything_goes


def _handle_download_report(future, url):
    """
    Handles the result of downloading an image.
    :param future: The Future instance that downloaded an image.
    :param url: The url where the image was downloaded from.

    :return True, if the image was downloaded. False, if the image was skipped, discarded, or timed out.
    """
    download_report = future.result()
    downloaded = True
    if not download_report["downloaded"]:
        reason = download_report["reason"]
        downloaded = False
        if reason == "err_timeout":
            print(f"Could not download {url}: {reason}")

    return downloaded


class Downloader(object):

    def __init__(self, download_folder, size_verifier):
        """
        Downloader of individual links to images.

        :param download_folder: The folder to download the image to.
        :param size_verifier: Filter function to discard image based on its size.
        """
        self.download_folder = download_folder
        self.verify_size = size_verifier
        # Read the directory for images that have already been downloaded
        self.previously_downloaded = os.listdir(self.download_folder)

    def __call__(self, *args, **kwargs):
        """
        Calling the Downloader directly is the same as calling the download_high_res function on it.
        """
        return self.download_high_res(*args, **kwargs)

    def download_high_res(self, high_res_source):
        """
        Download an image from a URL that points to a single image.
        The name of the image is extracted from the link, if possible.
        :param high_res_source: The source URL of the image to download.
        :returns the status report on how the download went.
                 A status report is a dict containing "downloaded" and "reason" fields.
        """
        # Try to extract a title
        stripped_slashes = high_res_source.split("/")[-1]
        title = stripped_slashes.split("--")[-1]

        status_report = {"downloaded": True, "reason": "valid"}

        # Check if the image is already present in the folder from previous runs of the script
        if title in self.previously_downloaded:
            status_report["downloaded"] = False
            status_report["reason"] = "err_present"
        else:
            destination = os.path.join(self.download_folder, title)
            try:
                # Download the image
                urllib.request.urlretrieve(high_res_source, destination)
                img = Image.open(destination)
                width, height = img.size
                # If the image was smaller then we want, we delete it again
                if not self.verify_size(width, height):
                    os.remove(destination)
                    status_report["downloaded"] == False
                    status_report["reason"] == "err_size"
            except urllib.request.ContentTooShortError:
                print(f"Connection died during download of Pin {title}.")
                status_report["downloaded"] = False
                status_report["reason"] = "err_timeout"

        return status_report


class PinterestDownloader(object):

    def __init__(self, num_threads=4,
                 min_resolution="0x0",size_compare_mode=None):
        """
        Downloader for pinterest boards or tag pages.
        This will open a selenium instance for scrolling.

        :param num_threads: Number of threads to download images with at the same time.
        :param min_resolution: The minimal resolution an image must have to be downloaded and kept.
               Format: XxY.
        :param size_compare_mode: Wether to use an image's area or both sides as resolution guidelines.
               One of 'area' or 'individual'.
        """
        self.browser = None
        self.browser_type = "phantomjs"  # Only support phantomjs for now, but can easily be extended

        self.num_threads = num_threads
        # Pick a minimal image resolution
        min_x, min_y = [int(r) for r in min_resolution.split("x")]
        self.size_verifier = _get_size_verifier(min_x, min_y, size_compare_mode)

    def __enter__(self):
        """
        Use this class with a with-statement as it needs to open a selenium instance.
        """
        if "phantomjs" == self.browser_type.lower():
            if which("phantomjs") is None:
                raise EnvironmentError("""No executable for PhantomJS found. Please install PhantomJS and make sure it's visible on the system.""")
            self.browser = webdriver.PhantomJS()
            # Set a fake window size for phantomJS, see https://github.com/ariya/phantomjs/issues/11637
            self.browser.set_window_size(1120, 550)
        else:
            raise ValueError("Unsupported browser type. Only phantomjs is supported right now.")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """
        Close the selenium instance.
        """
        self.browser.close()

    def download_board(self, board_url, download_folder,
                   board_name=None, num_pins=None,
                   skip_tolerance=float('inf')):
        """
        Download a specific pinterest page.
        :param board_url: The url to the pinterest board. Also works with tag pages.
        :param download_folder: The folder to download the images to. A new folder inside this folder will be created
               with the name of the board (unless that is already the name of the given folder).
        :param board_name: Name of the folder to be created, if not given the name will be guessed from the URL.
        :param num_pins: The number of pins to download. If not given, will download as many as possible. Interrupt
               the script with CTRL+C to stop. If the board has less pins than this number, the maximal possible number
               of pins will be downloaded. Pins that are skipped, because they have already been downloaded previously,
               or do not meet the size constraint of the PinterestDownloader, do not count towards the total count,
               so it is always tried to download num_pins new pins.
        :param skip_tolerance: Automatically stops the script if more than skip_tolerance pins have been skipped because
               they have already been downloaded (not because they do not meet the size constraint).
        :returns None.
        """
        self.browser.get(board_url)
        body = self.update_body_html()
        board_name, num_pins, download_folder = retrieve_bord_info(board_name=board_name,
                                                                   download_folder=download_folder,
                                                                   board_url=board_url,
                                                                   num_pins=num_pins,
                                                                   body=body)


        num_srcs = 0
        num_skipped = 0
        downloaded_this_time = 0
        url_cache = MemorySet()
        downloader = Downloader(download_folder, self.size_verifier)

        # Extract sources of images and download the found ones in parallel
        with concurrent.futures.ThreadPoolExecutor(self.num_threads) as consumers:
            print("Starting download...")

            while downloaded_this_time < num_pins and num_skipped < skip_tolerance:

                high_res_srcs, new_num_srcs = find_high_res_links(body)
                retrieved_new_urls = url_cache.update(high_res_srcs)
                if not retrieved_new_urls:
                    print(f"Stopped, no new pins found. Skipped {num_skipped} pins.")
                    break
                else:
                    print("Found some pins.")

                future_to_url = {}
                for high_res_link in url_cache:

                    future = consumers.submit(downloader,
                                              high_res_source=high_res_link)
                    future_to_url[future] = high_res_link
                    if len(future_to_url) + downloaded_this_time == num_pins:
                        break

                # Wait for the batch of images to complete
                for fut in concurrent.futures.as_completed(future_to_url):
                    url = future_to_url[fut]
                    succesfully_downloaded = _handle_download_report(future=fut, url=url)
                    num_skipped += not succesfully_downloaded
                    downloaded_this_time += succesfully_downloaded

                # Pinterest loads further images with JS, so selenium needs to scroll down to load more images
                num_srcs = new_num_srcs
                if num_srcs < num_pins:
                    print(f"Need to scroll down because {num_srcs} < {num_pins}")
                    body =  self.scroll_down_for_new_body(times=7)

        if num_skipped >= skip_tolerance:
            print("Skip limit reached. Stopping.")
        print(f"""Downloaded {downloaded_this_time} pins to {download_folder}.
              Skipped {num_skipped} pins.""")

    def update_body_html(self):
        """
        :returns the body of the current HTML page.
        """
        return self.browser.find_element_by_tag_name("body")

    def scroll_down_for_new_body(self, times=5, sleep_time=0.5):
        """
        Scroll down a page in selenium. This is needed because pinterest loads content dynamically on scroll down.

        :param times: Number of times to scroll down in one go. Since the time it takes pinterest to load the content
            can not be predicted, we need to scroll a few times to load more than a few new images.
        :param sleep_time: Time in ms to sleep in between scrolls to let pinterest load and re-enable the scroll bar.
            The scroll bar is disabled if we scrolled to the bottom and no new content has been loaded yet.
        :returns the new HTML body of the document including the newly loaded pins.
        """
        scroll_js = "let height = document.body.scrollHeight; window.scrollTo(0, height);"
        for _ in range(times):
            self.browser.execute_script(scroll_js)
            sleep(sleep_time)
        return self.update_body_html()


def handle_sig_int(signal, frame):
    """
    Exit gracefully on CTRL+C or other source of SIGINT.

    :param signal: The signal that was received.
    :param frame: The stack frame in which the signal was received.
    :return None.
    """
    print("Aborted, download may be incomplete.")
    sys.exit(0)

def parse_cmd():
    """
    Parses command line flags that control how pinterest will be scraped.
    Start the script with the '-h' option to read about all the arguments.

    :returns a namespace populated with the arguments supplied (or default arguments, if given).
    """
    parser = argparse.ArgumentParser(description="""Download a pinterest board or tag page. When downloading a tag page,
    and no maximal number of downloads is provided, stop the script with CTRL+C.""")
    # Required arguments
    parser.add_argument(dest="link", help="Link to the pinterest page you want to download.")
    parser.add_argument(dest="dest_folder",
                        help="""Folder into which the board will be downloaded. Folder with board name is automatically created or found, if it already exists.""")
    # Optional arguments
    parser.add_argument("-n", "--name", default=None, required=False, dest="board_name",
                        help="The name for the folder the board is downloaded in. If not given, will try to extract board name from pinterest.")
    parser.add_argument("-c", "--count", default=None, type=int, required=False, dest="num_pins",
                        help="""Download only the first 'c' pins found on the page. If bigger than the number of pins on the board, all pins in the board will be downloaded. The default is to download all pins.""")
    parser.add_argument("-j", "--threads", default=4, type=int, required=False, dest="nr_threads",
                        help="Number of threads that download images in parallel.")
    parser.add_argument("-r", "--resolution", default="0x0", required=False, dest="min_resolution",
                        help="""Minimal resolution to download an image. Both dimension must be bigger than the given dimensions. Input as widthxheight.""")
    parser.add_argument("-m", "--mode", default="individual", required=False, choices=["individual", "area"], dest="mode",
                        help="""Pick how the resolution limit is treated:
                             'individual': Both image dimensions must be bigger than the given resolution.
                             'area': The area of the image must be bigger than the provided resolution.""")
    parser.add_argument("-s" "--skip-limit", default=float("inf"), type=int, required=False, dest="skip_limit",
                        help="""Abort the download after so many pins have been skipped. A pin is skipped if it was already present in the download folder.""")
    args = parser.parse_args()

    return args

if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_sig_int)
    arguments = parse_cmd()
    with PinterestDownloader(num_threads=arguments.nr_threads,
                            min_resolution=arguments.min_resolution,
                            size_compare_mode=arguments.mode) as dl:
        dl.download_board(board_url=arguments.link, download_folder=arguments.dest_folder,
                      num_pins=arguments.num_pins, board_name=arguments.board_name,
                      skip_tolerance=arguments.skip_limit)