#!/usr/bin/env python

from __future__ import print_function

# -*- coding: utf-8 -*-
__author__ = "perfguru87@gmail.com"
__copyright__ = "Copyright 2018, The PerfTracker project"
__license__ = "MIT"

"""
Skeleton for webdriver based browser
"""

import logging
import httputils
import re
import os
import datetime
import time
import json
import sys
import tempfile
import atexit
import shutil

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
import helpers.timeparser
import helpers.largelogfile
import helpers.timeutil

from browser.utils import parse_url, get_val
from browser.browser_base import BrowserBase, BrowserExc, BrowserExcTimeout
from browser.page import Page, PageEvent, PageRequest, PageTimeline
from browser.browser_base import DEFAULT_WAIT_TIMEOUT, DEFAULT_AJAX_THRESHOLD


if sys.version_info[0] < 3:
    from urllib2 import URLError
else:
    from urllib.error import URLError


def help():
    ret = ["-" * 80,
           "The browser library requires:",
           "* Install python > 2.6 and *selenium*, *pyvirtualdisplay* and *psutil* modules",
           "     # sudo pip install selenium pyvirtualdisplay psutil"
           ]

    if sys.platform == "linux2":
        ret += ["     # yum install Xvfb"]

    ret += ["* To enable Chrome:",
            "     install Chrome browser version >= 40",
            "     install chromedriver from here http://chromedriver.storage.googleapis.com/index.html",
            "     copy the crhromedriver to /usr/bin/ and make it executable",
            "* To enable Firefox:",
            "     install Firefox browser version >= 31",
            ]
    return ret


def abort(msg):
    print("Error: " + str(msg), file=sys.stderr)
    print("\n".join(help()), file=sys.stderr)
    sys.exit(-1)


try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.common.exceptions import NoSuchElementException
    from selenium.common.exceptions import ElementNotVisibleException
    from selenium.common.exceptions import StaleElementReferenceException
    import psutil
except ImportError as e:
    abort(e)


class BrowserWebdriver(BrowserBase):
    skip_urls = []

    def __init__(self, *args, **kwargs):
        BrowserBase.__init__(self, *args, **kwargs)
        self._first_navigation_ts = None
        self._first_navigation_netloc = None
        self._ts_offset = None

    def _skip_url(self, page, url):
        if not url:
            return False

        _, req_netloc, _ = parse_url(url)

        for su in self.skip_urls:
            if su in req_netloc:
                _, page_netloc, _ = parse_url(page.url)
                if not any(x in page_netloc for x in self.skip_urls):
                    self.log_debug("skipping URL %s" % req_netloc)
                    return True
        return False

    def _browser_clear_caches(self):
        BrowserBase._browser_clear_caches(self)
        self.driver.quit()
        self.pid = self.browser_start()

    def _browser_navigate(self, location, cached=True, name=None):
        url = location.url if isinstance(location, Page) else location
        real_navigation = self._http_get(url)
        return Page(self, url, cached, name=name, real_navigation=real_navigation)

    def _browser_wait(self, page, timeout=DEFAULT_WAIT_TIMEOUT):

        self.log_info("_browser_wait()...")

        start = time.time()
        while time.time() - start < timeout / 2:
            time.sleep(0.5)
            if self.driver.execute_script("return window.performance.timing.loadEventEnd"):
                break
            # onload event has not been processed yet, so need to wait and retry
            self.log_info("Waiting for loadEventEnd ... ")

        while time.time() - start < timeout:
            time.sleep(DEFAULT_AJAX_THRESHOLD)

            # hack. Execute something in browser context to flush logs...
            self.driver.execute_script("return window.performance.timing.loadEventEnd")

            self._browser_get_events(page)

            ir = page.get_incomplete_reqs()
            if not ir:
                break
            self.log_info("Waiting for incomplete requests:\n    %s" %
                          ("\n    ".join(["%s - %s" % (r.id, r.url) for r in ir])))

        if time.time() - start >= timeout:
            if not self.driver.execute_script("return window.performance.timing.loadEventEnd"):
                self.log_error("Page '%s' load timeout, window.performance.timing.loadEventEnd = 0" % page.url)

            ir = page.get_incomplete_reqs()
            if ir:
                self.log_error("Can't wait for page '%s' load completion, "
                               "see '%s' for details\nincomplete requests:\n    %s" %
                               (page.url, self.log_path, "\n    ".join(["%s - %s" % (r.id, r.url) for r in ir])))

        page.complete(self)

    def _browser_warmup_page(self, location, name=None):
        self.navigate_to(location, timeout=DEFAULT_WAIT_TIMEOUT, cached=False, stats=False, name=name)

    def _browser_display_init(self, headless, resolution):
        if headless:
            try:
                from pyvirtualdisplay import Display
            except ImportError as e:
                abort(e)
            self.display = Display(visible=0, size=resolution)
            self.display.start()
        else:
            self.display = None

    def _browser_execute_script(self, js):
        val = self.driver.execute_script("return %s" % js)
        self.log_debug("%s = %s" % (js, val))
        return val

    def browser_get_name(self):
        c = self.driver.capabilities
        # return "%s %s" % (c['browserName'], c['version'])
        return c['browserName']

    def browser_get_screenshot_as_file(self, filename):
        self.driver.get_screenshot_as_file(filename)

    def browser_get_page_timeline(self, page):

        values = {}
        for t in PageTimeline.types:
            if t in PageTimeline.jstypes:
                js = "window.performance.timing.%s" % PageTimeline.jstypes[t]
                values[t] = self._browser_execute_script(js)

        return PageTimeline(page, values)

#    def browser_set_session(self, domain, session_id):
#        self._http_get(domain)
#        self.driver.add_cookie({'name': 'sessionid', 'value': session_id})

    def browser_get_current_url(self):
        return self.driver.current_url

    def browser_get_screenshot(self, filename):
        self.driver.get_screenshot_as_file(filename)

    def browser_stop(self):
        try:
            if self.driver:
                self.driver.quit()
                self.driver = None
            if self.display:
                self.display.stop()
                self.display = None
        except URLError:
            pass

    def _xpath_click(self, xpath):
        exc = None

        # take into account possible replacements of %23/#
        xpaths = [xpath]
        if "%23" in xpath:
            xpaths.append(xpath.replace("%23", "#"))
        if "#" in xpath:
            xpaths.append(xpath.replace("#", "%23"))

        for x in xpaths:
            self.log_debug("Looking for xpath: %s ..." % x)
            try:
                el = self.driver.find_element_by_xpath(x)
                el.click()
                self.log_debug("Looking for xpath: %s ... OK" % x)
                return
            except NoSuchElementException as e:
                self.log_debug("Looking for xpath: %s ... Failed, no such element" % x)
                exc = e
            except ElementNotVisibleException as e:
                self.log_warning("Looking for xpath: %s ... Failed, element not visible" % x)
                exc = e

        self.log_error("NoSuchElementException, xpath: %s, see debug log" % xpath)
        self.log_debug("page source:\n%s" % self.driver.page_source.encode('ascii', 'ignore'))
        raise BrowserExc(e)

    def _http_get(self, url, validator=None):
        self.log_debug("Execute GET request: %s" % url)

        if not self._first_navigation_ts:
            self._first_navigation_ts = time.time()
            _, self._first_navigation_netloc, _ = parse_url(url)

        ar = url.split("^")
        if len(ar) > 1:
            self._xpath_click(ar[1])
            return False

        try:
            self.driver.get(url)
        except WebDriverException as e:
            raise BrowserExc(e)
        return True

    @staticmethod
    def _get_val(d, keys):
        for key in keys:
            if key in d:
                return d[key]
        return "unknown"

    def print_browser_info(self):
        c = self.driver.capabilities
        self.print_stats_title("Browser summary")
        print("C:", c)
        print("  - platform: %s" % self._get_val(c, ['platform', 'platformName']))
        print("  - browser:  %s %s" % (c['browserName'], self._get_val(c, ['version', 'browserVersion'])))
        print("  - PID:      %d" % self.pid)
        print("  - log file: %s" % self.log_path)

    def print_log_file_path(self):
        self.print_stats_title("Browser log file")
        print("  %s" % self.log_path)

    # === virtual methods that must be implemented in every webdriver-based browser === #

    def _browser_parse_logs(self):
        raise BrowserExcNotImplemented

    def _browser_get_events(self):
        raise BrowserExcNotImplemented

    # === webdriver specific === #

    def dom_wait_element_stale(self, el, timeout_s=DEFAULT_WAIT_TIMEOUT, name=None):
        start_time = time.time()

        # http://www.obeythetestinggoat.com/how-to-get-selenium-to-wait-for-page-load-after-a-click.html
        while time.time() < start_time + timeout_s:
            try:
                el.find_elements_by_id('doesnt-matter')
                pass
            except StaleElementReferenceException:
                break
            time.sleep(0.2)

        if time.time() > start_time + timeout_s:
            msg = "DOM element '%s' click() timeout: %.1fs" % (name, time.time() - start_time)
            self.log_error(msg)
            raise BrowserExcTimeout(msg)

    def dom_click(self, el, timeout_s=DEFAULT_WAIT_TIMEOUT, name=None, wait_callback=None, wait_callback_obj=None):
        self.log_debug("dom_click(%s, %s)" % (str(el), str(name)))

        p = Page(self, self.browser_get_current_url(), True, name=name, real_navigation=False)
        p.start()

        # 1. click on the element

        old_page = self.driver.find_element_by_tag_name('html')
        el.click()

        # 2. wait for selenium onclick completion

        if wait_callback:
            self.log_debug("wait callback: %s, %s" % (str(wait_callback.__name__), str(wait_callback_obj)))
            wait_callback(wait_callback_obj, el, timeout_s, name)
        else:
            self.log_debug("wait stale: %s, %s, %s" % (el, timeout_s, name))
            self.dom_wait_element_stale(el, timeout_s, name)

        # 3. wait for ajax completion, because browser URL can be update only after that

        self._browser_wait(p, timeout=timeout_s)
        p.url = self.browser_get_current_url()

        time.sleep(0.5)

    def dom_find_element_by_id(self, id):
        try:
            return self.driver.find_element_by_id(id)
        except NoSuchElementException as e:
            raise BrowserExc(e)

    def dom_find_element_by_name(self, name):
        try:
            return self.driver.find_element_by_name(name)
        except NoSuchElementException as e:
            raise BrowserExc(e)

    def dom_find_frames(self):
        frames = []
        for name in ("frame", "iframe"):
            try:
                frames += self.driver.find_elements_by_tag_name(name)
            except NoSuchElementException as e:
                pass
        return frames

    def dom_switch_to_frame(self, frame):
        self.log_info("Switching to frame %s" % frame)
        return self.driver.switch_to.frame(frame)

    def dom_switch_to_default_content(self):
        self.log_info("Switching to default content")
        return self.driver.switch_to.default_content()

    def dom_send_keys(self, el, keys):
        for ch in keys:
            el.send_keys(ch)
            time.sleep(0.2)
        val = el.get_attribute('value')
        if val == keys:
            return True

        self.log_warning("Bogus selenium send_keys(). Entered: '%s', "
                         "but see: '%s', using set_attribute()..." % (keys, val))
        time.sleep(2.0)
        self.driver.execute_script("arguments[0].value = '%s'" % keys, el)
        time.sleep(2.0)
        val = el.get_attribute('value')
        if val == keys:
            self.log_info("Ok, set_attribute() works fine")
            return True

        self.log_error("Bogus selenium send_keys() and set_attribute(), can't enter value into the element")
        return False

    # === some predefined scenarios === #

    def _do_universal_login_single(self, url, user, password):
        def get_list(d):
            ret = [k for k in d]
            for k in d:
                if type(k) is tuple:
                    ret.append((k[0].capitalize(), k[1].capitalize()))
                else:
                    ret.append(k.capitalize())
            return ret

        user_input_found = False
        pass_input_found = False

        for tag, name in get_list([("input", "user"), ("input", "username"), ("input", "login")]):
            try:
                el = self.dom_find_element_by_name(name)
                if el.tag_name != tag:
                    continue
                if not self.dom_send_keys(el, user):
                    return False
                user_input_found = True
            except BrowserExc as e:
                pass

        time.sleep(1)

        if not user_input_found:
            self.log_error("Couldn't find user name input field")
            return False

        for tag, name in get_list([("input", "pass"), ("input", "password"), ("input", "login_password")]):
            try:
                el = self.dom_find_element_by_name(name)
                if el.tag_name != tag:
                    continue
                if not self.dom_send_keys(el, password):
                    return False
                pass_input_found = True
            except BrowserExc as e:
                pass

        if not pass_input_found:
            self.log_error("Couldn't find user password input field")
            return False

        time.sleep(1)
        for tag, name in get_list([("button", "login"), ("button", "login_submit")]):
            try:
                el = self.dom_find_element_by_name(name)
                if el.tag_name != tag:
                    continue
                self.dom_click(el, name=name)

                try:
                    el = self.dom_find_element_by_name(name)
                except BrowserExc:
                    self.log_info("Login successed")
                    return True

            except BrowserExc as e:
                pass

        for tag, id in get_list([("button", "login"), ("button", "submit"),
                                 ("button", "submit1"), ("button", "signin_button")]):
            try:
                el = self.dom_find_element_by_id(id)
                if el.tag_name != tag:
                    continue
                self.dom_click(el, name=id)

                try:
                    el = self.dom_find_element_by_id(id)
                except BrowserExc:
                    self.log_info("Login successed")
                    return True

            except BrowserExc as e:
                pass

        self.log_info("Login failed")
        return False

    def do_universal_login(self, url, user, password):
        self.log_info("Trying to login to '%s' under user %s" % (url, user))
        self.navigate_to(url, cached=None)

        if self._do_universal_login_single(url, user, password):
            return True

        for frame in self.dom_find_frames():
            self.dom_switch_to_frame(frame)
            if self._do_universal_login_single(url, user, password):
                return True

        self.log_error("Login to '%s' under user '%s' has been failed" % (url, user))
        return False


##############################################################################
# Autotests
##############################################################################


if __name__ == "__main__":
    try:
        b = BrowserWebdriver()
    except NotImplementedError:
        print("OK")
        sys.exit(0)
    sys.exit(-1)
