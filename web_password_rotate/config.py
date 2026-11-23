"""Everything that differs from one console to the next lives in one TOML file.

The code below never names a page, a frame or a button. Selectors, frame names and the
order of clicks come from the file, so another console means another file, not another script.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass

PLACEHOLDER = re.compile(r"\{(\w+)\}")
SECRET_NAMES = {"new", "old"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Selector:
    kind: str    # "css" or "xpath"
    value: str   # may contain {account}

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"


@dataclass(frozen=True)
class LoginPage:
    url: str
    frame: str
    user: tuple[Selector, ...]
    password: tuple[Selector, ...]
    submit: tuple[Selector, ...]
    success_url: str
    success: tuple[Selector, ...]
    error: tuple[Selector, ...]
    timeout: float


@dataclass(frozen=True)
class Step:
    action: str                    # open | click | type | accept_alert
    frame: str = ""                # "" = top document, "main" or "main/inner" = frame path by name or id
    target: tuple[Selector, ...] = ()
    value: str = ""                # open: url; type: text template; accept_alert: expected text

    def describe(self) -> str:
        where = f" in frame {self.frame}" if self.frame else ""
        if self.action == "open":
            return f"open {self.value}"
        if self.action == "accept_alert":
            return f"accept alert{f' containing {self.value!r}' if self.value else ''}"
        # the value of a type step is never shown: it usually holds the new password
        return f"{self.action} {self.target[0]}{where}"


@dataclass(frozen=True)
class ResultCheck:
    frame: str
    success: tuple[Selector, ...]
    success_alert: str
    error: tuple[Selector, ...]
    timeout: float


@dataclass(frozen=True)
class Config:
    base_url: str
    step_timeout: float
    admin: LoginPage
    steps: tuple[Step, ...]
    result: ResultCheck
    verify: LoginPage

    @property
    def needs_old(self) -> bool:
        return any("{old}" in s.value for s in self.steps if s.action == "type")


# --- parsing -------------------------------------------------------------------------------

def _no_unknown(table: dict, allowed: set[str], where: str) -> None:
    extra = sorted(set(table) - allowed)
    if extra:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(extra)}")


def _placeholders_ok(text: str, allowed: set[str], where: str) -> None:
    for name in PLACEHOLDER.findall(text):
        if name in SECRET_NAMES and name not in allowed:
            # selectors and URLs end up in logs and error messages; passwords must not
            raise ConfigError(f"{where}: {{{name}}} is only allowed in the value of a type step")
        if name not in allowed:
            raise ConfigError(f"{where}: unknown placeholder {{{name}}}")


def parse_selector(text, where: str) -> Selector:
    if not isinstance(text, str):
        raise ConfigError(f"{where}: selector must be a string")
    kind, sep, value = text.partition(":")
    if not sep or kind not in ("css", "xpath") or not value.strip():
        raise ConfigError(f"{where}: selector must start with 'css:' or 'xpath:' (got {text!r})")
    _placeholders_ok(value, {"account"}, where)
    return Selector(kind, value)


def _selectors(raw, where: str, required: bool = True) -> tuple[Selector, ...]:
    """A selector or a list of selectors tried in order (first visible match wins)."""
    if raw is None:
        if required:
            raise ConfigError(f"{where}: missing")
        return ()
    items = raw if isinstance(raw, list) else [raw]
    if not items and required:
        raise ConfigError(f"{where}: empty list")
    return tuple(parse_selector(s, f"{where}[{i}]" if len(items) > 1 else where) for i, s in enumerate(items))


def _login_page(t: dict, where: str, default_timeout: float) -> LoginPage:
    if not isinstance(t, dict):
        raise ConfigError(f"[{where}] missing")
    _no_unknown(t, {"url", "frame", "user", "password", "submit", "success_url", "success", "error", "timeout"}, f"[{where}]")
    url = t.get("url")
    if not isinstance(url, str) or not url:
        raise ConfigError(f"[{where}] url: missing")
    _placeholders_ok(url, set(), f"[{where}] url")
    success_url = t.get("success_url", "")
    page = LoginPage(
        url=url,
        frame=t.get("frame", ""),
        user=_selectors(t.get("user"), f"[{where}] user"),
        password=_selectors(t.get("password"), f"[{where}] password"),
        submit=_selectors(t.get("submit"), f"[{where}] submit"),
        success_url=success_url,
        success=_selectors(t.get("success"), f"[{where}] success", required=False),
        error=_selectors(t.get("error"), f"[{where}] error", required=False),
        timeout=float(t.get("timeout", default_timeout)),
    )
    if not page.success_url and not page.success:
        raise ConfigError(f"[{where}]: set success_url or success, otherwise a login can never be confirmed")
    if page.success_url and page.success_url in page.url:
        # we would "succeed" while still standing on the login page
        raise ConfigError(f"[{where}] success_url {success_url!r} is part of the login url itself")
    return page


def _step(t: dict, i: int) -> Step:
    where = f"[[change]] #{i}"
    actions = [a for a in ("open", "click", "type", "accept_alert") if a in t]
    if len(actions) != 1:
        raise ConfigError(f"{where}: needs exactly one of open / click / type / accept_alert")
    action = actions[0]
    _no_unknown(t, {action, "frame", "value"}, where)
    frame = t.get("frame", "")
    if action == "open":
        _placeholders_ok(t["open"], {"account"}, f"{where} open")
        return Step("open", frame, (), t["open"])
    if action == "accept_alert":
        return Step("accept_alert", "", (), str(t["accept_alert"]))
    target = _selectors(t[action], f"{where} {action}")
    if action == "type":
        if "value" not in t:
            raise ConfigError(f"{where}: type needs a value, e.g. value = \"{{new}}\"")
        _placeholders_ok(t["value"], {"account", "new", "old"}, f"{where} value")
        return Step("type", frame, target, t["value"])
    if "value" in t:
        raise ConfigError(f"{where}: value only belongs to a type step")
    return Step("click", frame, target)


def load(path: str, base_url: str | None = None) -> Config:
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    return parse(raw, base_url)


def parse(raw: dict, base_url: str | None = None) -> Config:
    _no_unknown(raw, {"base_url", "step_timeout", "admin", "change", "result", "verify"}, "top level")
    step_timeout = float(raw.get("step_timeout", 10))
    steps = raw.get("change")
    if not isinstance(steps, list) or not steps:
        raise ConfigError("[[change]]: at least one step is needed")
    r = raw.get("result")
    if not isinstance(r, dict):
        raise ConfigError("[result] missing")
    _no_unknown(r, {"frame", "success", "success_alert", "error", "timeout"}, "[result]")
    result = ResultCheck(
        frame=r.get("frame", ""),
        success=_selectors(r.get("success"), "[result] success", required=False),
        success_alert=r.get("success_alert", ""),
        error=_selectors(r.get("error"), "[result] error", required=False),
        timeout=float(r.get("timeout", step_timeout)),
    )
    if not result.success and not result.success_alert:
        raise ConfigError("[result]: set success or success_alert; a change that cannot be confirmed is not a success")
    url = base_url or raw.get("base_url", "")
    if not url:
        raise ConfigError("base_url missing (in the file or with --base-url)")
    return Config(
        base_url=url,
        step_timeout=step_timeout,
        admin=_login_page(raw.get("admin"), "admin", step_timeout),
        steps=tuple(_step(s, i) for i, s in enumerate(steps, 1)),
        result=result,
        verify=_login_page(raw.get("verify"), "verify", step_timeout),
    )


# --- filling in placeholders ---------------------------------------------------------------

def xpath_literal(s: str) -> str:
    """Quote s as an XPath 1.0 string. XPath has no escape character, so mixed quotes need concat()."""
    if "'" not in s:
        return f"'{s}'"
    if '"' not in s:
        return f'"{s}"'
    parts = s.split("'")
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"


def css_literal(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_selector(sel: Selector, account: str) -> Selector:
    """{account} becomes a quoted string literal, so an account name can never change the selector."""
    quote = xpath_literal if sel.kind == "xpath" else css_literal
    return Selector(sel.kind, sel.value.replace("{account}", quote(account)))


def render_value(template: str, values: dict[str, str]) -> str:
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], template)
