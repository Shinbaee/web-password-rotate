"""End to end: headless Chrome against the fake console in tests/fake_console.py.

Each run starts two or three browsers, as the real tool does. Skipped when Chrome cannot start.
"""

import pytest
from conftest import EXAMPLE
from fake_console import ACCOUNT, ADMIN, OLD

from web_password_rotate import cli, flow
from web_password_rotate.browser import Deadline, fresh_browser
from web_password_rotate.config import load

NEW = "New-pass-4567"


@pytest.fixture(scope="module", autouse=True)
def need_browser():
    try:
        with fresh_browser():
            pass
    except Exception as e:
        pytest.skip(f"browser not available: {e}")


def run(console, monkeypatch, *extra, new=NEW, admin_pw=ADMIN[1], account=ACCOUNT, old=OLD):
    url, _ = console
    monkeypatch.setenv("T_ADMIN", admin_pw)
    monkeypatch.setenv("T_NEW", new)
    argv = ["--config", EXAMPLE, "--base-url", url, "--account", account, "--admin-user", ADMIN[0],
            "--admin-password-env", "T_ADMIN", "--new-password-env", "T_NEW", "--timeout", "60", *extra]
    if old is not None:
        monkeypatch.setenv("T_OLD", old)
        argv += ["--old-password-env", "T_OLD"]
    return cli.main(argv)


def no_secrets(capsys, *secrets):
    out = capsys.readouterr()
    text = out.out + out.err
    for s in secrets:
        assert s not in text
    return out


def test_change_and_verify(console, monkeypatch, capsys):
    _, state = console
    assert run(console, monkeypatch) == 0
    assert state.passwords[ACCOUNT] == NEW
    assert state.passwords["svc_report"] == "Report-pass-9"  # only the selected row changed
    assert state.portal_attempts == [(ACCOUNT, True)]         # verified once, old never tried
    out = no_secrets(capsys, NEW, OLD, ADMIN[1])
    assert out.out.strip() == f"OK: password of {ACCOUNT} changed and verified"
    assert "step 7/7 accept alert" in out.err and "result: alert 'Password updated'" in out.err


def test_slow_grid_is_waited_for(console, monkeypatch, capsys):
    console[1].grid_delay_ms = 2500
    assert run(console, monkeypatch) == 0


def test_wrong_admin_password(console, monkeypatch, capsys):
    _, state = console
    assert run(console, monkeypatch, admin_pw="wrong-admin-pw") == flow.ADMIN_LOGIN_FAILED
    assert state.passwords[ACCOUNT] == OLD
    assert "Invalid user or password" in no_secrets(capsys, "wrong-admin-pw", NEW).out


def test_policy_message_is_reported(console, monkeypatch, capsys):
    _, state = console
    assert run(console, monkeypatch, new="Short-1") == flow.REJECTED
    assert state.passwords[ACCOUNT] == OLD
    assert "Password must be at least 10 characters" in no_secrets(capsys, "Short-1").out


def test_password_longer_than_the_field_is_not_cut(console, monkeypatch, capsys):
    _, state = console
    long_pw = "L" * 40
    assert run(console, monkeypatch, new=long_pw) == flow.REJECTED
    assert state.passwords[ACCOUNT] == OLD  # nothing was saved, not even the first 32 characters
    assert "at most 32 characters" in no_secrets(capsys, long_pw).out


def test_unknown_account_names_the_step(console, monkeypatch, capsys):
    assert run(console, monkeypatch, account="nobody") == flow.STEP_FAILED
    assert "step 2/7" in capsys.readouterr().out


def test_console_says_saved_but_did_not(console, monkeypatch, capsys):
    _, state = console
    state.silent_no_save = True
    assert run(console, monkeypatch) == flow.NOT_CHANGED
    assert state.portal_attempts == [(ACCOUNT, False), (ACCOUNT, True)]
    assert "old password still signs in" in no_secrets(capsys, NEW, OLD).out


def test_no_diagnose_costs_one_failed_attempt_only(console, monkeypatch, capsys):
    _, state = console
    state.silent_no_save = True
    assert run(console, monkeypatch, "--no-diagnose") == flow.UNVERIFIED
    assert state.portal_attempts == [(ACCOUNT, False)]


def test_saved_but_account_cannot_sign_in(console, monkeypatch, capsys):
    _, state = console
    state.disable_after_change = True
    assert run(console, monkeypatch) == flow.BOTH_REFUSED
    assert state.passwords[ACCOUNT] == NEW


def test_overall_timeout(console, monkeypatch, capsys):
    console[1].grid_delay_ms = 30000
    url, _ = console
    monkeypatch.setenv("T_ADMIN", ADMIN[1])
    monkeypatch.setenv("T_NEW", NEW)
    code = cli.main(["--config", EXAMPLE, "--base-url", url, "--account", ACCOUNT, "--admin-user", ADMIN[0],
                     "--admin-password-env", "T_ADMIN", "--new-password-env", "T_NEW", "--timeout", "8"])
    assert code == cli.EXIT_TIMEOUT
    assert "longer than 8s" in capsys.readouterr().out


def test_reusing_the_admin_browser_would_fake_a_success(console):
    """Why verification gets its own browser: the admin session cookie alone gets past the portal."""
    url, state = console
    cfg = load(EXAMPLE, url)
    r = flow.Run(cfg=cfg, account=ACCOUNT, admin_user=ADMIN[0], admin_password=ADMIN[1],
                 new_password="not-the-password", old_password=None, deadline=Deadline(60), log=lambda m: None)
    with fresh_browser() as d:
        assert flow.login(r, d, cfg.admin, ADMIN[0], ADMIN[1])[0]
        d.get(url + "/portal/")
        assert d.current_url.endswith("/portal/home")  # never saw the sign-in form
    with fresh_browser() as d:
        ok, _ = flow.login(r, d, cfg.verify, ACCOUNT, "not-the-password")
        assert not ok
