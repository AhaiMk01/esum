"""T2 Chrome-fingerprint HTTP: plain HTTP requests sent through curl_cffi.

curl_cffi sends Chrome's real TLS and HTTP/2 fingerprint, so sites that block non-browser clients
by fingerprint answer without a browser tab. It runs no JavaScript: a JS challenge (Cloudflare's
"Just a moment…" page) still needs T3.

By default the backend also borrows the browser session's login. On the first request it reads
the session's cookies (`patchright-cli cookie-list`) and the browser's user agent (clearance
cookies are bound to it). The cookies live only in this process's memory; they are never logged or
written to disk. Cookies the server sets during the call stay in that in-memory jar and are not
copied back to the browser.

A site that needs no login sets `extras={"browser_cookies": False}`. Then no browser is involved at
all, and curl_cffi's own Chrome user agent matches the fingerprint.
"""

from __future__ import annotations

from http.cookiejar import Cookie
from typing import TYPE_CHECKING, Any, Callable

from curl_cffi import requests as cffi
from curl_cffi.requests import exceptions as cffi_errors

from ..browser.login import check_login
from ..browser.session import BrowserSession
from . import Request, Response

if TYPE_CHECKING:
    from ..core.context import Options
    from ..core.model import Site

IMPERSONATE = "chrome"  # curl_cffi's newest Chrome fingerprint


class BrowserHttpBackend:
    # curl_cffi's network failures; HttpClient retries reads after these as it does after httpx's.
    transient_errors = (cffi_errors.Timeout, cffi_errors.ConnectionError)

    def __init__(self, site: "Site", session: BrowserSession,
                 session_factory: Callable[[], Any] | None = None):
        self.site = site
        self.session = session
        self._factory = session_factory or (lambda: cffi.Session(impersonate=IMPERSONATE))
        self._http: Any = None

    def send(self, request: Request, *, before_send: Callable[[], None] = lambda: None) -> Response:
        client = self._client()
        before_send()
        r = client.request(request.method, request.url, headers=request.headers, content=request.content,
                           timeout=request.timeout, allow_redirects=request.follow_redirects)
        headers: dict[str, str] = {}
        for name, value in r.headers.multi_items():
            name = name.lower()
            headers[name] = f"{headers[name]}, {value}" if name in headers else value
        response = Response(status=r.status_code, url=str(r.url), headers=headers, content=r.content)
        check_login(self.site, response, self.session.where)
        return response

    def _client(self) -> Any:
        if self._http is None:
            http = self._factory()
            if self.site.extras.get("browser_cookies", True):
                for c in self.session.cookies():
                    http.cookies.jar.set_cookie(_cookie(c))
                http.headers["User-Agent"] = self.session.user_agent()
            self._http = http
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None


def _cookie(c: dict[str, Any]) -> Cookie:
    """A Playwright cookie dict as a cookiejar Cookie. A leading dot means "and subdomains"."""
    domain = str(c.get("domain", ""))
    expires = c.get("expires")
    expires = int(expires) if isinstance(expires, (int, float)) and expires > 0 else None
    return Cookie(version=0, name=str(c["name"]), value=str(c.get("value", "")), port=None,
                  port_specified=False, domain=domain, domain_specified=domain.startswith("."),
                  domain_initial_dot=domain.startswith("."), path=str(c.get("path") or "/"),
                  path_specified=True, secure=bool(c.get("secure")), expires=expires,
                  discard=expires is None, comment=None, comment_url=None,
                  rest={"HttpOnly": ""} if c.get("httpOnly") else {})


def make_backend(site: "Site", options: "Options", *, session: BrowserSession) -> BrowserHttpBackend:
    return BrowserHttpBackend(site, session)
