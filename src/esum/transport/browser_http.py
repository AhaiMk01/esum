"""T2 Chrome-fingerprint HTTP: plain HTTP requests sent through curl_cffi.

curl_cffi sends Chrome's real TLS and HTTP/2 fingerprint, so sites that block non-browser clients
by fingerprint answer without a browser tab. It runs no JavaScript: a JS challenge (Cloudflare's
"Just a moment…" page) still needs T3.

By default the backend also borrows the browser session's login. On the first request it reads
the session's cookies (`patchright-cli cookie-list`) and the browser's user agent (clearance
cookies are bound to it) and languages, and sends the User-Agent, Client Hints (`Sec-Ch-Ua*`) and
Accept-Language the browser would, rather than curl_cffi's own Chrome version, Windows platform and
English. The cookies live only in this process's memory; they are never logged or written to disk. Cookies the server sets during the call stay in that in-memory jar and are not
copied back to the browser.

A site that needs no login sets `extras={"browser_cookies": False}`. Then no browser is involved at
all, and curl_cffi's own Chrome user agent matches the fingerprint.
"""

from __future__ import annotations

import re
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
                identity = self.session.identity()
                http.headers["User-Agent"] = identity.user_agent
                http.headers.update(client_hints(identity.user_agent))
                if identity.languages:
                    http.headers["Accept-Language"] = accept_language(identity.languages)
            self._http = http
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None


_GREASE_CHARS = " (:-./);=?_"
_GREASE_VERSIONS = ("8", "99", "24")
_ORDERINGS = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))
_PLATFORMS = (("Windows NT", "Windows"), ("CrOS", "Chrome OS"), ("Android", "Android"),
              ("Macintosh", "macOS"), ("Linux", "Linux"))


def client_hints(user_agent: str) -> dict[str, str]:
    """The `Sec-Ch-Ua*` headers Chrome sends with `user_agent`, so they agree with the borrowed UA
    instead of curl_cffi's own Chrome version and Windows platform. The brand list follows
    Chromium's `GenerateBrandVersionList`, seeded by the major version (checked against Chrome 152
    and 157). A UA that isn't Chrome's gets no hints from here."""
    m = re.search(r"\bChrome/(\d+)", user_agent)
    if not m or re.search(r"\b(Edg|OPR|YaBrowser|SamsungBrowser)/", user_agent):
        return {}
    major = m.group(1)
    seed = int(major)
    grease = (f"Not{_GREASE_CHARS[seed % 11]}A{_GREASE_CHARS[(seed + 1) % 11]}Brand",
              _GREASE_VERSIONS[seed % 3])
    brands: list[tuple[str, str]] = [("", "")] * 3
    order = _ORDERINGS[seed % 6]
    brands[order[0]], brands[order[1]], brands[order[2]] = grease, ("Chromium", major), ("Google Chrome", major)
    platform = next((name for token, name in _PLATFORMS if token in user_agent), "")
    return {"Sec-Ch-Ua": ", ".join(f'"{b}";v="{v}"' for b, v in brands),
            "Sec-Ch-Ua-Mobile": "?1" if "Mobile" in user_agent else "?0",
            "Sec-Ch-Ua-Platform": f'"{platform}"'}


def accept_language(languages: tuple[str, ...] | list[str]) -> str:
    """Chrome's Accept-Language for `navigator.languages`: the first bare, then q falling by 0.1
    down to 0.1 (`["zh-CN", "zh"]` is "zh-CN,zh;q=0.9", as Chrome sends it)."""
    return ",".join(lang if i == 0 else f"{lang};q={max(10 - i, 1) / 10:g}"
                    for i, lang in enumerate(languages))


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
