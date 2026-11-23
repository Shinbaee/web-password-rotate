"""Config parsing and placeholder handling. No browser needed."""

import pytest
from conftest import EXAMPLE

from web_password_rotate import cli
from web_password_rotate.config import (
    ConfigError, Selector, css_literal, load, parse, render_selector, render_value, xpath_literal,
)

BASE = {
    "base_url": "http://x",
    "admin": {"url": "/login", "user": "css:#u", "password": "css:#p", "submit": "css:#s", "success_url": "/home"},
    "change": [{"click": "css:#go"}],
    "result": {"success_alert": "ok"},
    "verify": {"url": "/login", "user": "css:#u", "password": "css:#p", "submit": "css:#s", "success_url": "/home"},
}


def with_(**over):
    raw = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in BASE.items()}
    raw.update(over)
    return raw


def test_example_config_loads():
    cfg = load(EXAMPLE)
    assert [s.action for s in cfg.steps] == ["click", "click", "click", "type", "type", "click", "accept_alert"]
    assert len(cfg.steps[1].target) == 2  # fallback selector
    assert cfg.needs_old is False
    assert load(EXAMPLE, "http://other:9").base_url == "http://other:9"


def test_secret_placeholder_outside_a_type_value_is_refused():
    with pytest.raises(ConfigError, match="only allowed in the value of a type step"):
        parse(with_(change=[{"click": "xpath://input[@value='{new}']"}]))
    with pytest.raises(ConfigError, match="only allowed"):
        parse(with_(change=[{"open": "/reset?pw={old}"}]))


def test_unknown_placeholder_and_key():
    with pytest.raises(ConfigError, match="unknown placeholder"):
        parse(with_(change=[{"type": "css:#x", "value": "{password}"}]))
    with pytest.raises(ConfigError, match="unknown key"):
        parse(with_(change=[{"click": "css:#x", "wait": 3}]))


def test_step_needs_exactly_one_action():
    with pytest.raises(ConfigError, match="exactly one"):
        parse(with_(change=[{"click": "css:#a", "type": "css:#b", "value": "x"}]))
    with pytest.raises(ConfigError, match="type needs a value"):
        parse(with_(change=[{"type": "css:#b"}]))


def test_selector_needs_a_kind():
    with pytest.raises(ConfigError, match="'css:' or 'xpath:'"):
        parse(with_(change=[{"click": "#go"}]))


def test_result_must_be_confirmable():
    with pytest.raises(ConfigError, match="cannot be confirmed"):
        parse(with_(result={"error": "css:.err"}))


def test_success_url_inside_login_url_is_refused():
    admin = dict(BASE["admin"], url="/home/login")
    with pytest.raises(ConfigError, match="part of the login url"):
        parse(with_(admin=admin))


def test_needs_old():
    assert parse(with_(change=[{"type": "css:#o", "value": "{old}"}])).needs_old


@pytest.mark.parametrize("name,expected", [
    ("svc", "'svc'"),
    ("o'neil", '"o\'neil"'),
    ("a'b\"c", "concat('a', \"'\", 'b\"c')"),
])
def test_xpath_literal(name, expected):
    assert xpath_literal(name) == expected


def test_account_cannot_break_out_of_the_selector():
    sel = render_selector(Selector("xpath", "//div[.={account}]"), "x'] | //*[@id='admin")
    assert sel.value == "//div[.=\"x'] | //*[@id='admin\"]"  # one string, not a second path
    sel = render_selector(Selector("xpath", "//div[.={account}]"), "x\"'] | //*[@id='admin")
    assert sel.value.startswith("//div[.=concat(")
    assert css_literal('a"b') == '"a\\"b"'


def test_render_value():
    assert render_value("{new}", {"new": "N", "old": "O", "account": "a"}) == "N"
    assert render_value("{old}-{account}", {"new": "N", "old": "O", "account": "a"}) == "O-a"


# --- CLI checks that happen before any browser starts ---

def args(*extra):
    return ["--config", EXAMPLE, "--account", "svc_app", "--admin-user", "admin",
            "--admin-password-env", "T_ADMIN", "--new-password-env", "T_NEW", *extra]


def test_missing_env_var_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.delenv("T_ADMIN", raising=False)
    monkeypatch.setenv("T_NEW", "x")
    assert cli.main(args()) == cli.EXIT_USAGE
    assert "T_ADMIN is not set" in capsys.readouterr().err


def test_new_equal_to_old_is_refused(monkeypatch, capsys):
    for k, v in {"T_ADMIN": "a", "T_NEW": "Same-pass-1", "T_OLD": "Same-pass-1"}.items():
        monkeypatch.setenv(k, v)
    assert cli.main(args("--old-password-env", "T_OLD")) == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "could not tell them apart" in err and "Same-pass-1" not in err


def test_check_config_lists_steps_without_values(capsys):
    assert cli.main(args("--check-config")) == 0
    out = capsys.readouterr().out
    assert "4. type css:#new-pw in frame main" in out
    assert "{new}" not in out


def test_redact():
    assert cli.redact("got S3cret-1 twice: S3cret-1", ["S3cret-1", None]) == "got [redacted] twice: [redacted]"
