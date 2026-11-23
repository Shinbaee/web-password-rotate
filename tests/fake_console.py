"""A small stateful admin console and user portal for the tests. Not modelled on any product.

  /admin/login     form sign-in, session cookie
  /admin/console   two frames: nav (menu) and main (content)
  /admin/users     JavaScript grid drawn after a delay; div checkboxes, span buttons, an edit
                   dialog inside the same frame, a confirm() before saving, alert() on success
  /portal/         redirects to /portal/login; a valid session cookie of ANY user skips the form
  /portal/login    the account's own sign-in

Switches in State reproduce the failures the tool must tell apart.
"""

from __future__ import annotations

import html
import json
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ADMIN = ("admin", "Admin-pass-1")
ACCOUNT, OLD = "svc_app", "Old-pass-123"


class State:
    def __init__(self):
        self.reset()

    def reset(self):
        self.passwords = {ADMIN[0]: ADMIN[1], ACCOUNT: OLD, "svc_report": "Report-pass-9"}
        self.sessions: dict[str, str] = {}
        self.portal_attempts: list[tuple[str, bool]] = []
        self.min_length = 10
        self.grid_delay_ms = 300
        self.silent_no_save = False        # says "updated", stores nothing
        self.disable_after_change = False  # stores it, but the account can no longer sign in
        self.disabled: set[str] = set()


PAGE = "<!doctype html><meta charset=utf-8><title>{title}</title>{body}"

ADMIN_LOGIN = """
<form method=post action=/admin/login>
  <label>User <input id=user name=user></label>
  <label>Password <input id=pass name=pass type=password></label>
  <button type=submit>Sign in</button>
  {error}
</form>"""

CONSOLE = """
<style>body{margin:0;display:flex;height:100vh} iframe{border:0;height:100%} #navf{width:180px} #mainf{flex:1}</style>
<iframe id=navf name=nav src=/admin/nav></iframe>
<iframe id=mainf name=main src=/admin/welcome></iframe>"""

NAV = """<a id=nav-users href=/admin/users target=main>Users</a>"""

USERS = """
<style>
 .row-checker{width:14px;height:14px;border:1px solid #888} .row-checker.on{background:#36c}
 .btn{display:inline-block;border:1px solid #888;padding:2px 8px;cursor:pointer}
 #edit-modal{display:none;border:1px solid #333;padding:8px;margin-top:8px}
 .form-error{display:none;color:#c00}
</style>
<div class=btn id=btn-edit><span class=btn-text>Edit</span></div>
<table id=grid><tbody></tbody></table>
<div id=edit-modal>
  <div>Edit user <span id=m-account></span></div>
  <label>Password <input id=new-pw type=password maxlength=32></label>
  <label>Confirm <input id=confirm-pw type=password maxlength=32></label>
  <div class=form-error></div>
  <div class=btn id=btn-ok><span class=btn-text>OK</span></div>
</div>
<script>
const USERS = __USERS__;
setTimeout(() => {
  const tb = document.querySelector('#grid tbody');
  for (const u of USERS) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td><div class="row-checker"></div></td><td><div class="cell"></div></td>';
    tr.querySelector('.cell').textContent = u;
    tr.onclick = () => {                       // clicking anywhere on the row selects it
      document.querySelectorAll('.row-checker.on').forEach(x => x.classList.remove('on'));
      tr.querySelector('.row-checker').classList.add('on');
    };
    tb.appendChild(tr);
  }
}, __DELAY__);
function selected() {
  const on = document.querySelector('.row-checker.on');
  return on ? on.closest('tr').querySelector('.cell').textContent : null;
}
document.getElementById('btn-edit').onclick = () => {
  const a = selected();
  if (!a) { alert('Select a user first'); return; }
  document.getElementById('m-account').textContent = a;
  document.getElementById('edit-modal').style.display = 'block';
};
document.getElementById('btn-ok').onclick = async () => {
  const a = selected();
  const err = document.querySelector('.form-error');
  err.style.display = 'none';
  if (!confirm('Change the password of ' + a + '?')) return;
  const r = await fetch('/admin/api/password', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({account: a, password: document.getElementById('new-pw').value,
                          confirm: document.getElementById('confirm-pw').value})});
  const j = await r.json();
  if (j.ok) { document.getElementById('edit-modal').style.display = 'none'; alert(j.message); }
  else { err.textContent = j.message; err.style.display = 'block'; }
};
</script>"""

PORTAL_LOGIN = """
<form id=auth method=post action=/portal/login>
  <input name=username> <input name=password type=password> <input type=submit value="Log on">
  {error}
</form>"""


class Handler(BaseHTTPRequestHandler):
    state: State  # set on the subclass made by start()

    def log_message(self, *args):  # keep test output clean (and request bodies out of it)
        pass

    # --- helpers ---
    def _session(self) -> str | None:
        c = SimpleCookie(self.headers.get("Cookie", ""))
        sid = c["sid"].value if "sid" in c else None
        return self.state.sessions.get(sid) if sid else None

    def _send(self, body: str, title: str = "", status: int = 200, headers: dict | None = None):
        data = PAGE.format(title=title, body=body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to: str, cookie: str | None = None):
        self.send_response(303)
        self.send_header("Location", to)
        if cookie:
            self.send_header("Set-Cookie", f"sid={cookie}; Path=/; HttpOnly")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def _new_session(self, user: str) -> str:
        sid = secrets.token_hex(16)
        self.state.sessions[sid] = user
        return sid

    # --- routes ---
    def do_GET(self):
        path = urlparse(self.path).path
        st = self.state
        if path == "/admin/login":
            return self._send(ADMIN_LOGIN.format(error=""), "Console sign in")
        if path.startswith("/admin/"):
            if self._session() != ADMIN[0]:
                return self._redirect("/admin/login")
            if path == "/admin/console":
                return self._send(CONSOLE, "Console")
            if path == "/admin/nav":
                return self._send(NAV, "Menu")
            if path == "/admin/welcome":
                return self._send("<p>Welcome</p>", "Welcome")
            if path == "/admin/users":
                users = json.dumps(sorted(u for u in st.passwords if u != ADMIN[0]))
                return self._send(USERS.replace("__USERS__", users).replace("__DELAY__", str(st.grid_delay_ms)), "Users")
        if path in ("/portal", "/portal/"):
            return self._redirect("/portal/login")
        if path == "/portal/login":
            if self._session():  # "single sign-on": any live session skips the form
                return self._redirect("/portal/home")
            return self._send(PORTAL_LOGIN.format(error=""), "Sign in")
        if path == "/portal/home":
            user = self._session()
            if not user:
                return self._redirect("/portal/login")
            return self._send(f"<h1>Hello {html.escape(user)}</h1>", "Home")
        self._send("<p>not found</p>", "Not found", 404)

    def do_POST(self):
        path = urlparse(self.path).path
        st = self.state
        if path == "/admin/login":
            f = {k: v[0] for k, v in parse_qs(self._body().decode()).items()}
            if (f.get("user"), f.get("pass")) == ADMIN:
                return self._redirect("/admin/console", self._new_session(ADMIN[0]))
            return self._send(ADMIN_LOGIN.format(error="<div class=login-error>Invalid user or password</div>"),
                              "Console sign in")
        if path == "/admin/api/password":
            if self._session() != ADMIN[0]:
                return self._json({"ok": False, "message": "Session expired"})
            req = json.loads(self._body())
            account, pw = req.get("account"), req.get("password", "")
            if account not in st.passwords:
                return self._json({"ok": False, "message": "No such user"})
            if pw != req.get("confirm"):
                return self._json({"ok": False, "message": "Passwords do not match"})
            if len(pw) < st.min_length:
                return self._json({"ok": False, "message": f"Password must be at least {st.min_length} characters"})
            if not st.silent_no_save:
                st.passwords[account] = pw
            if st.disable_after_change:
                st.disabled.add(account)
            return self._json({"ok": True, "message": "Password updated"})
        if path == "/portal/login":
            f = {k: v[0] for k, v in parse_qs(self._body().decode()).items()}
            user, pw = f.get("username", ""), f.get("password", "")
            ok = user in st.passwords and user not in st.disabled and st.passwords[user] == pw
            st.portal_attempts.append((user, ok))
            if ok:
                return self._redirect("/portal/home", self._new_session(user))
            return self._send(PORTAL_LOGIN.format(error="<div class=login-error>Sign-in failed</div>"), "Sign in")
        self._send("<p>not found</p>", "Not found", 404)


def start() -> tuple[ThreadingHTTPServer, State]:
    state = State()
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


if __name__ == "__main__":  # python tests/fake_console.py -> try it in a normal browser
    srv, _ = start()
    print(f"demo console on http://127.0.0.1:{srv.server_port}/admin/login  (admin / {ADMIN[1]})")
    threading.Event().wait()
