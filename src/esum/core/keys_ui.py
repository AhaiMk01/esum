"""`esum keys ui`: a small local page to see and manage saved keys (D-79).

It serves on 127.0.0.1 only, behind a one-time token in the URL that becomes a SameSite cookie, and
checks Host and Origin on every request, so other pages and other machines can't reach it. It shows
each key's site, store and date, never a key. Changing anything needs the OS to confirm the user
first (`osauth`); a confirmation lasts five minutes. Removing a key deletes ESUM's copy only (the
user revokes it on the site), and adding one runs the site's `login`, whose
`ctx.ask_secret` gets the key typed here, so the same checks apply. The page closes after ten idle minutes.
"""

from __future__ import annotations

import html
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, TextIO
from urllib.parse import parse_qs, urlsplit

from . import keystore, osauth

IDLE_SECONDS = 600
UNLOCK_SECONDS = 300
COOKIE = "esum_keys"


class KeysPage:
    """The page's state: who may talk to it, whether the OS confirmed the user, the last message."""

    def __init__(self, sites: dict[str, dict], run: Callable[[str, str, str | None], tuple[int, str]],
                 auth=osauth, clock: Callable[[], float] = time.monotonic) -> None:
        self.sites = sites            # site name -> {"label", "entry": key prompt or None}
        self.run = run                # (site, action, secret) -> (exit code, output), in this process
        self.auth = auth
        self.clock = clock
        self.token = secrets.token_urlsafe(32)
        self.session = secrets.token_urlsafe(32)
        self.origin = ""
        self.unlocked_until = 0.0
        self.last_seen = clock()
        self.message = ""
        self.method, self.why_not = auth.available()
        self.busy = threading.Lock()  # one change at a time
        self._token_lock = threading.Lock()

    def unlocked(self) -> bool:
        return self.clock() < self.unlocked_until

    def take_token(self, token: str) -> bool:
        """The link's token works once: the first browser to open it gets the session cookie."""
        with self._token_lock:
            if self.token and secrets.compare_digest(token, self.token):
                self.token = ""
                return True
            return False

    # --- actions -----------------------------------------------------------------------------

    def unlock(self) -> None:
        if not self.method:
            self.message = f"can't confirm you here: {self.why_not}"
        elif self.auth.verify("ESUM wants to change your saved site keys"):
            self.unlocked_until = self.clock() + UNLOCK_SECONDS
            self.message = "unlocked for 5 minutes"
        else:
            self.message = "not confirmed; nothing changed"

    def remove(self, site: str) -> None:
        if not any(e["site"] == site for e in keystore.entries()):
            self.message = f"no saved key for {site}"
            return
        keystore.delete(site)
        self.message = (f"deleted the saved {site} key here; it still works on the site until you revoke "
                        f"it there (a Discourse forum lists it under Preferences, Apps)")

    def add(self, site: str, key: str) -> None:
        if not (self.sites.get(site) or {}).get("entry"):
            self.message = f"{site} keys are added with `esum {site} login` in your terminal"
            return
        code, out = self.run(site, "login", key)
        self.message = out.strip() or f"login exited {code}"

    def post(self, path: str, form: dict[str, str]) -> None:
        if path == "/unlock":
            self.unlock()
            return
        if not self.unlocked():
            self.message = "locked: confirm it's you first"
            return
        site = form.get("site", "")
        if path == "/remove":
            self.remove(site)
        elif path == "/add":
            self.add(site, form.get("key", ""))

    # --- the page ------------------------------------------------------------------------------

    def render(self) -> str:
        e = html.escape
        unlocked = self.unlocked()
        off = "" if unlocked else " disabled"
        rows = "".join(
            f"<tr><td>{e(r['site'])}</td><td>{e((self.sites.get(r['site']) or {}).get('label', ''))}</td>"
            f"<td>{e(r['store'])}</td><td>{e(str(r.get('created', '')))}</td>"
            f"<td>{e(','.join(r.get('scopes') or []))}</td>"
            f"<td><form method=post action=/remove><input type=hidden name=site value=\"{e(r['site'])}\">"
            f"<button{off}>Remove</button></form></td></tr>"
            for r in keystore.entries())
        table = (f"<table><tr><th>site</th><th>name</th><th>stored in</th><th>saved</th><th>scopes</th><th></th></tr>"
                 f"{rows}</table>") if rows else "<p>No saved keys.</p>"
        options = "".join(f"<option value=\"{e(name)}\">{e(name)}: {e(info['entry'])}</option>"
                          for name, info in sorted(self.sites.items()) if info.get("entry"))
        add = (f"<h2>Add or replace a key</h2><form method=post action=/add>"
               f"<select name=site{off}>{options}</select> "
               f"<input type=password name=key autocomplete=off placeholder=key{off}> "
               f"<button{off}>Save</button></form>") if options else ""
        if unlocked:
            left = int(self.unlocked_until - self.clock())
            lock = f"<p class=ok>Unlocked for {left // 60}:{left % 60:02d}.</p>"
        elif self.method:
            lock = (f"<form method=post action=/unlock><button>Unlock with {e(self.method)}</button></form>"
                    "<p class=dim>Viewing needs nothing; changing a key needs the system to confirm it's you.</p>")
        else:
            lock = (f"<p class=dim>Read-only: {e(self.why_not)}. Use <code>esum &lt;site&gt; login</code> in your terminal.</p>")
        note = f"<p class=msg>{e(self.message)}</p>" if self.message else ""
        self.message = ""
        return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>ESUM keys</title><style>
:root{{color-scheme:light dark;--fg:#1d1d1f;--bg:#fff;--dim:#6e6e73;--line:#d2d2d7;--ok:#1a7f37}}
@media (prefers-color-scheme:dark){{:root{{--fg:#f5f5f7;--bg:#1c1c1e;--dim:#98989d;--line:#3a3a3c;--ok:#3fb950}}}}
body{{font:15px/1.5 system-ui,sans-serif;color:var(--fg);background:var(--bg);max-width:760px;margin:2rem auto;padding:0 16px}}
table{{border-collapse:collapse;width:100%}}td,th{{text-align:left;padding:.4rem;border-bottom:1px solid var(--line)}}
th{{color:var(--dim);font-weight:500}}.dim{{color:var(--dim)}}.ok{{color:var(--ok)}}
.msg{{padding:.5rem .75rem;border:1px solid var(--line);border-radius:6px}}form{{margin:0}}
button,input,select{{font:inherit}}h2{{font-size:1rem;margin-top:2rem}}</style></head><body>
<h1>Saved site keys</h1>{note}{lock}{table}{add}
<p class=dim>Discourse forums add a key with <code>esum &lt;site&gt; login</code> in your terminal (it asks you
to approve ESUM on the forum). Keys are never shown here. This page closes after 10 idle minutes.</p>
</body></html>"""


def _handler(page: KeysPage):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:   # no request log: a URL can carry the token
            pass

        def _send(self, status: int, body: str = "", headers: dict[str, str] | None = None) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            for name, value in {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(data)),
                                # same-origin, not no-referrer: with no-referrer a form post sends `Origin: null`
                                "Cache-Control": "no-store", "Referrer-Policy": "same-origin",
                                "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                                           "form-action 'self'; frame-ancestors 'none'",
                                **(headers or {})}.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self) -> bool:
            if self.headers.get("Host") != page.origin.removeprefix("http://"):
                return False          # a rebinding DNS name pointing at 127.0.0.1
            cookies = dict(c.strip().partition("=")[::2] for c in self.headers.get("Cookie", "").split(";") if c)
            return secrets.compare_digest(cookies.get(COOKIE, ""), page.session)

        def do_GET(self) -> None:
            page.last_seen = page.clock()
            url = urlsplit(self.path)
            token = parse_qs(url.query).get("t", [""])[0]
            if url.path == "/" and token and self.headers.get("Host") == page.origin.removeprefix("http://") \
                    and page.take_token(token):
                self._send(303, headers={"Location": "/", "Set-Cookie":
                                         f"{COOKIE}={page.session}; HttpOnly; SameSite=Strict; Path=/"})
                return
            if url.path != "/" or not self._allowed():
                self._send(403, "This link was already used or isn't this page's: run `esum keys ui` again "
                                "for a new one.")
                return
            self._send(200, page.render())

        def do_POST(self) -> None:
            page.last_seen = page.clock()
            if not self._allowed() or self.headers.get("Origin") != page.origin:
                self._send(403, "forbidden")
                return
            length = min(int(self.headers.get("Content-Length") or 0), 8192)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8", "replace")).items()}
            with page.busy:
                page.post(urlsplit(self.path).path, form)
            self._send(303, headers={"Location": "/"})

    return Handler


def serve(page: KeysPage, out: TextIO, *, open_browser: bool = True,
          idle: float = IDLE_SECONDS, port: int = 0) -> ThreadingHTTPServer:
    """Start the page on 127.0.0.1 and print its link; blocks until it has been idle too long."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(page))
    page.origin = f"http://127.0.0.1:{server.server_address[1]}"
    url = f"{page.origin}/?t={page.token}"
    out.write(f"ESUM keys: {url}\n(closes after {int(idle // 60)} idle minutes; Ctrl+C stops it)\n")
    out.flush()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if open_browser:
        webbrowser.open(url)
    try:
        while page.clock() - page.last_seen < idle:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    out.write("ESUM keys page closed\n")
    return server

