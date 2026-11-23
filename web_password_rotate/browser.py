"""Browser start-up and the few primitives the flow needs: enter a frame, wait, click, type, alerts.

Every lookup starts again from the top document. Every wait is bounded twice: by the step's
own timeout and by the deadline of the whole run.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from contextlib import contextmanager

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoAlertPresentException,
    NoSuchFrameException,
    StaleElementReferenceException,
    UnexpectedAlertPresentException,
    WebDriverException,
)
from selenium.webdriver.common.by import By

from .config import Selector

POLL = 0.2

# Chromium slows down timers and rendering in windows it thinks nobody is looking at
# (minimised, covered, or on a virtual display without a viewer). Pages drawn by JavaScript
# then appear late and waits time out at random.
NO_THROTTLING = [
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]


class BrowserStartError(Exception):
    pass


class OverallTimeout(Exception):
    pass


class StepError(Exception):
    """An element did not show up, or something unexpected was on the screen."""


class Rejected(Exception):
    """The screen refused the value (policy message, field too short for it...)."""


class Deadline:
    def __init__(self, seconds: float):
        self.seconds = seconds
        self.end = time.monotonic() + seconds

    def remaining(self) -> float:
        return self.end - time.monotonic()

    def check(self) -> None:
        if self.remaining() <= 0:
            raise OverallTimeout(f"the whole run took longer than {self.seconds:g}s")


@contextmanager
def fresh_browser(browser: str = "chrome", headless: bool = True, insecure: bool = False,
                  window: str = "1280,1000"):
    """A browser with its own empty profile: no cookies, no session, no cache from earlier runs.
    The profile directory is deleted afterwards."""
    profile = tempfile.mkdtemp(prefix="wpr-profile-")
    opts = webdriver.EdgeOptions() if browser == "edge" else webdriver.ChromeOptions()
    for arg in NO_THROTTLING + [f"--user-data-dir={profile}", f"--window-size={window}",
                                "--no-first-run", "--no-default-browser-check"]:
        opts.add_argument(arg)
    if headless:
        opts.add_argument("--headless=new")
    if insecure:
        opts.set_capability("acceptInsecureCerts", True)
    # Leave alerts open until we read them. The default closes them and only reports an error,
    # and the alert is often the only place a console says whether the save worked.
    opts.set_capability("unhandledPromptBehavior", "ignore")
    try:
        driver = webdriver.Edge(options=opts) if browser == "edge" else webdriver.Chrome(options=opts)
    except WebDriverException as e:
        shutil.rmtree(profile, ignore_errors=True)
        raise BrowserStartError(str(e).splitlines()[0] if str(e) else type(e).__name__) from None
    try:
        driver.set_page_load_timeout(30)
        yield driver
    finally:
        try:
            driver.quit()
        finally:
            shutil.rmtree(profile, ignore_errors=True)


def take_alert(driver) -> str | None:
    """Accept an open alert/confirm and return its text, or None when there is none."""
    try:
        alert = driver.switch_to.alert
        text = alert.text
        alert.accept()
        return text
    except NoAlertPresentException:
        return None


def enter(driver, frame: str) -> None:
    """Go to the top document, then down the frame path ("main" or "main/inner").
    From inside one frame a sibling frame cannot be reached, so never start from where we are."""
    driver.switch_to.default_content()
    for name in filter(None, frame.split("/")):
        driver.switch_to.frame(name)


def find_visible(driver, frame: str, selectors: tuple[Selector, ...]):
    """First visible element matched by the selectors, tried in order. None if nothing yet."""
    enter(driver, frame)
    for sel in selectors:
        by = By.XPATH if sel.kind == "xpath" else By.CSS_SELECTOR
        for el in driver.find_elements(by, sel.value):
            if el.is_displayed():
                return el
    return None


def wait_visible(driver, frame: str, selectors: tuple[Selector, ...], timeout: float, deadline: Deadline):
    end = time.monotonic() + min(timeout, max(deadline.remaining(), 0))
    while True:
        try:
            el = find_visible(driver, frame, selectors)
            if el is not None:
                return el
        except UnexpectedAlertPresentException:
            text = take_alert(driver)
            raise StepError(f"unexpected alert: {text!r}") from None
        except (NoSuchFrameException, StaleElementReferenceException):
            pass  # frame not loaded yet, or the page is being redrawn
        if time.monotonic() >= end:
            deadline.check()
            where = f" in frame {frame}" if frame else ""
            raise StepError(f"not found within {timeout:g}s{where}: {' | '.join(map(str, selectors))}")
        time.sleep(POLL)


def click(driver, el) -> None:
    try:
        el.click()
    except (ElementClickInterceptedException, ElementNotInteractableException):
        # framework buttons are often a styled span under another element
        driver.execute_script("arguments[0].click();", el)
    except UnexpectedAlertPresentException:
        pass  # the click went through and opened a dialog; the next step deals with it


def type_into(driver, el, text: str) -> None:
    """Type and check the field took all of it. Only the length is read back, never the value."""
    limit = el.get_attribute("maxlength")
    if limit and limit.isdigit() and 0 < int(limit) < len(text):
        # the browser would cut the password silently and the console would save the cut one
        raise Rejected(f"field accepts at most {limit} characters, value has {len(text)}")
    el.clear()
    el.send_keys(text)
    got = driver.execute_script("return arguments[0].value.length;", el)
    if got != len(text):
        raise Rejected(f"field holds {got} characters after typing {len(text)} (input mask or script?)")


def wait_alert(driver, timeout: float, deadline: Deadline) -> str:
    end = time.monotonic() + min(timeout, max(deadline.remaining(), 0))
    while True:
        text = take_alert(driver)
        if text is not None:
            return text
        if time.monotonic() >= end:
            deadline.check()
            raise StepError(f"no alert within {timeout:g}s")
        time.sleep(POLL)
