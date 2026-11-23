"""Command line entry point. Progress goes to stderr, one result line to stdout, the cause in the exit code."""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import flow
from .browser import BrowserStartError, Deadline, OverallTimeout, StepError
from .config import ConfigError, load

EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_BROWSER = 40
EXIT_TIMEOUT = 41


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="web-password-rotate",
        description="Change an account's password on a web admin console, then sign in with the new "
                    "password in a fresh browser to prove it worked.",
        epilog="Passwords are read from environment variables, never from the command line "
               "(the process list shows every argument).",
    )
    p.add_argument("--config", required=True, help="TOML file describing the console (see examples/)")
    p.add_argument("--account", required=True, help="account whose password is changed")
    p.add_argument("--admin-user", required=True, help="admin account used for the change")
    p.add_argument("--admin-password-env", required=True, metavar="NAME", help="env var holding the admin password")
    p.add_argument("--new-password-env", required=True, metavar="NAME", help="env var holding the new password")
    p.add_argument("--old-password-env", metavar="NAME",
                   help="env var holding the current password; used to explain a failed verification")
    p.add_argument("--no-diagnose", action="store_true",
                   help="never try the old password (for consoles that lock accounts after failed sign-ins)")
    p.add_argument("--base-url", help="override base_url from the config file")
    p.add_argument("--timeout", type=float, default=150, help="limit for the whole run, in seconds (default 150)")
    p.add_argument("--browser", choices=["chrome", "edge"], default="chrome")
    p.add_argument("--show", action="store_true", help="show the browser window instead of running headless")
    p.add_argument("--insecure", action="store_true", help="accept self-signed certificates")
    p.add_argument("--screenshot-dir", help="save a screenshot on failure (off by default: screens show account data)")
    p.add_argument("--check-config", action="store_true", help="validate the config and print the steps, then exit")
    return p.parse_args(argv)


def secret_from_env(name: str | None, what: str, required: bool = True) -> str | None:
    if not name:
        return None
    value = os.environ.get(name)
    if not value:
        if required:
            raise ConfigError(f"{what}: environment variable {name} is not set or empty")
        return None
    return value


def redact(text: str, secrets) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "[redacted]")
    return text


def main(argv=None) -> int:
    args = parse_args(argv)
    secrets: list[str] = []

    def log(msg: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {redact(msg, secrets)}", file=sys.stderr, flush=True)

    def result(code: int, msg: str) -> int:
        line = "OK: " + msg if code == 0 else f"FAIL {code}: {msg}"
        print(redact(line, secrets), flush=True)
        return code

    try:
        cfg = load(args.config, args.base_url)
        if args.check_config:
            for i, s in enumerate(cfg.steps, 1):
                print(f"{i}. {s.describe()}")
            return 0
        admin_pw = secret_from_env(args.admin_password_env, "admin password")
        new_pw = secret_from_env(args.new_password_env, "new password")
        old_pw = secret_from_env(args.old_password_env, "old password")
        secrets.extend(s for s in (admin_pw, new_pw, old_pw) if s)
        if cfg.needs_old and not old_pw:
            raise ConfigError("the change steps type {old}: give --old-password-env")
        if old_pw is not None and old_pw == new_pw:
            raise ConfigError("new password equals the old one; the verification could not tell them apart")
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE

    run = flow.Run(
        cfg=cfg, account=args.account, admin_user=args.admin_user, admin_password=admin_pw,
        new_password=new_pw, old_password=old_pw, deadline=Deadline(args.timeout), log=log,
        diagnose=not args.no_diagnose, browser=args.browser, headless=not args.show,
        insecure=args.insecure, screenshot_dir=args.screenshot_dir,
    )
    log(f"start: account {args.account} on {cfg.base_url}")
    try:
        return result(flow.OK, flow.rotate(run))
    except flow.Failure as e:
        return result(e.code, str(e))
    except StepError as e:
        return result(flow.STEP_FAILED, str(e))
    except OverallTimeout as e:
        return result(EXIT_TIMEOUT, str(e))
    except BrowserStartError as e:
        return result(EXIT_BROWSER, f"browser did not start: {e}")
    except Exception as e:  # report, never dump a traceback that may hold page content
        return result(EXIT_ERROR, f"unexpected {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")


if __name__ == "__main__":
    sys.exit(main())
