"""Change the password on the admin console, then prove it by signing in with it somewhere else.

    admin browser:   sign in as admin -> change steps -> read the result
    fresh browser:   sign in as the account with the NEW password       -> success
    fresh browser:   (only if that failed) try the OLD password         -> tells why it failed
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote, urljoin

from selenium.common.exceptions import (
    NoSuchFrameException,
    StaleElementReferenceException,
    UnexpectedAlertPresentException,
    WebDriverException,
)

from .browser import (
    POLL, Deadline, Rejected, StepError, click, find_visible, fresh_browser, take_alert,
    type_into, wait_alert, wait_visible,
)
from .config import Config, LoginPage, render_selector, render_value

# exit codes (also used by the CLI)
OK = 0
ADMIN_LOGIN_FAILED = 10
STEP_FAILED = 20
REJECTED = 21
NOT_CHANGED = 30          # the old password still signs in: the save never happened
BOTH_REFUSED = 31         # neither password signs in: locked, disabled, or saved differently
UNVERIFIED = 32           # new password refused, old one deliberately not tried


class Failure(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Run:
    cfg: Config
    account: str
    admin_user: str
    admin_password: str
    new_password: str
    old_password: str | None
    deadline: Deadline
    log: Callable[[str], None]
    diagnose: bool = True
    browser: str = "chrome"
    headless: bool = True
    insecure: bool = False
    screenshot_dir: str | None = None

    def browser_session(self):
        return fresh_browser(self.browser, self.headless, self.insecure)

    def sel(self, selectors):
        return tuple(render_selector(s, self.account) for s in selectors)

    def screenshot(self, driver, tag: str) -> None:
        """Opt-in only: the screen may show account lists and internal addresses."""
        if not self.screenshot_dir:
            return
        try:
            os.makedirs(self.screenshot_dir, exist_ok=True)
            path = os.path.join(self.screenshot_dir, f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}.png")
            take_alert(driver)  # a screenshot cannot be taken while a dialog is open
            driver.save_screenshot(path)
            os.chmod(path, 0o600)
            self.log(f"screenshot: {path}")
        except (OSError, WebDriverException) as e:
            self.log(f"screenshot failed: {type(e).__name__}")


def login(run: Run, driver, page: LoginPage, user: str, password: str) -> tuple[bool, str]:
    """Sign in. Success is judged by where the browser ends up, then by an element, never by
    the HTTP status: a failed login usually answers 200 or 302 just like a good one."""
    dl = run.deadline
    driver.get(urljoin(run.cfg.base_url, page.url))
    user_el = wait_visible(driver, page.frame, run.sel(page.user), page.timeout, dl)
    user_el.clear()
    user_el.send_keys(user)
    pw_el = wait_visible(driver, page.frame, run.sel(page.password), page.timeout, dl)
    pw_el.clear()
    pw_el.send_keys(password)
    click(driver, wait_visible(driver, page.frame, run.sel(page.submit), page.timeout, dl))

    end = time.monotonic() + min(page.timeout, max(dl.remaining(), 0))
    while time.monotonic() < end:
        try:
            driver.switch_to.default_content()
            if page.success_url and page.success_url in driver.current_url:
                return True, f"landed on {driver.current_url}"
            if page.success and find_visible(driver, page.frame, run.sel(page.success)):
                return True, "success element shown"
            if page.error:
                err = find_visible(driver, page.frame, run.sel(page.error))
                if err is not None and err.text.strip():
                    return False, err.text.strip()
        except UnexpectedAlertPresentException:
            return False, f"alert: {take_alert(driver)!r}"
        except (NoSuchFrameException, StaleElementReferenceException, WebDriverException):
            pass  # page is navigating
        time.sleep(POLL)
    dl.check()
    return False, f"no sign of success within {page.timeout:g}s (still on {driver.current_url})"


def change(run: Run, driver) -> str:
    values = {"account": run.account, "new": run.new_password, "old": run.old_password or ""}
    total = len(run.cfg.steps)
    for i, step in enumerate(run.cfg.steps, 1):
        label = f"step {i}/{total} {step.describe()}"
        try:
            if step.action == "open":
                driver.get(urljoin(run.cfg.base_url, render_value(step.value, {"account": quote(run.account)})))
            elif step.action == "accept_alert":
                text = wait_alert(driver, run.cfg.step_timeout, run.deadline)
                if step.value and step.value.lower() not in text.lower():
                    raise StepError(f"alert said {text!r}, expected it to contain {step.value!r}")
            else:
                el = wait_visible(driver, step.frame, run.sel(step.target), run.cfg.step_timeout, run.deadline)
                if step.action == "click":
                    click(driver, el)
                else:
                    type_into(driver, el, render_value(step.value, values))
        except StepError as e:
            raise Failure(STEP_FAILED, f"{label}: {e}") from None
        except Rejected as e:
            raise Failure(REJECTED, f"{label}: {e}") from None
        run.log(f"{label}: ok")
    return read_result(run, driver)


def read_result(run: Run, driver) -> str:
    """After the last step the console must say yes or no. Silence is a failure, not a success."""
    r = run.cfg.result
    end = time.monotonic() + min(r.timeout, max(run.deadline.remaining(), 0))
    while time.monotonic() < end:
        text = take_alert(driver)
        if text is not None:
            if r.success_alert and r.success_alert.lower() in text.lower():
                return f"alert {text!r}"
            raise Failure(REJECTED, f"change refused: alert {text!r}")
        try:
            if r.success and find_visible(driver, r.frame, run.sel(r.success)):
                return "success element shown"
            if r.error:
                err = find_visible(driver, r.frame, run.sel(r.error))
                if err is not None and err.text.strip():
                    raise Failure(REJECTED, f"change refused: {err.text.strip()}")
        except UnexpectedAlertPresentException:
            continue  # read it on the next turn
        except (NoSuchFrameException, StaleElementReferenceException):
            pass
        time.sleep(POLL)
    run.deadline.check()
    raise Failure(STEP_FAILED, f"no success or error shown within {r.timeout:g}s after the last step")


def rotate(run: Run) -> str:
    # 1) change, as admin
    with run.browser_session() as d:
        try:
            ok, note = login(run, d, run.cfg.admin, run.admin_user, run.admin_password)
            if not ok:
                raise Failure(ADMIN_LOGIN_FAILED, f"admin sign-in failed: {note}")
            run.log(f"admin sign-in: ok ({note})")
            run.log(f"result: {change(run, d)}")
        except (Failure, StepError):
            run.screenshot(d, "change")
            raise

    # 2) verify in a new browser. The admin browser still holds a session cookie; some portals
    #    would let it straight in and the check would pass whatever the password is.
    run.log("verify: new browser, empty profile")
    with run.browser_session() as d:
        ok, note = login(run, d, run.cfg.verify, run.account, run.new_password)
        if ok:
            run.log(f"verify: new password accepted ({note})")
            return f"password of {run.account} changed and verified"
        run.screenshot(d, "verify")
    run.log(f"verify: new password refused ({note})")

    # 3) why? Trying the old password costs one more failed attempt if it is wrong too,
    #    which matters where accounts lock after a few. Hence --no-diagnose.
    if not run.diagnose or not run.old_password:
        raise Failure(UNVERIFIED, f"new password does not sign in ({note}); old password not tried")
    with run.browser_session() as d:
        old_ok, _ = login(run, d, run.cfg.verify, run.account, run.old_password)
    if old_ok:
        raise Failure(NOT_CHANGED, "password was not changed: the old password still signs in")
    raise Failure(BOTH_REFUSED, f"neither the new nor the old password signs in ({note})")
