"""T3 in-page fetch: a generic `fetch()` evaluated in a tab of the site's origin.

For sites that block plain HTTP clients (Cloudflare, fingerprint checks), the request runs inside
a real Chrome tab, so it carries that tab's cookies, TLS fingerprint and clearance. The first
request to an origin opens a tab on the site's `base_url` (or `extras["browser_home"]`, a URL or
path on the site, when the base URL is a poor landing page) and waits out an anti-bot
interstitial: while the tab's title names one or, on a site that declares
`extras["challenge_text"]`, until a request from the tab gets a real page (some checks' pages,
such as AWS WAF's, have no telling title). A check that
outlasts the wait (a Turnstile box to tick) raises CHECK NEEDED with the tab left open, for the
host agent to complete with patchright-cli (D-54). Later requests of the same call reuse the tab;
`close()` closes it.

`FETCH_JS` below is the only code ESUM runs in a page. It knows nothing about any site: it sends
the request it is given and returns the status, final URL, headers and the raw body (base64), which
`HttpClient` decodes by the site's declared charset (FlyerTalk: ISO-8859-1).
"""

from __future__ import annotations

import base64
import re
from typing import TYPE_CHECKING, Any, Callable

from ..browser.daemon import BrowserError
from ..browser.login import check_login
from ..browser.session import CHALLENGE_TITLE, BrowserSession, Tab, TabLost, origin_of
from ..core.urls import is_absolute
from . import RETRY_METHODS, Request, Response, is_challenged

if TYPE_CHECKING:
    from ..core.context import Options
    from ..core.model import Site

FETCH_JS = """async (a) => {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), a.timeout_ms);
  try {
    const init = {method: a.method, headers: a.headers, credentials: "include",
                  redirect: "follow", signal: ctl.signal};
    if (a.body !== null) init.body = Uint8Array.from(atob(a.body), (c) => c.charCodeAt(0));
    const r = await fetch(a.url, init);
    const bytes = new Uint8Array(await r.arrayBuffer());
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    const headers = {};
    r.headers.forEach((v, k) => { headers[k] = v; });
    return {ok: true, status: r.status, url: r.url, headers: headers, body: btoa(bin)};
  } catch (e) {
    const message = String((e && e.message) || e);
    const kind = e && e.name === "AbortError" ? "timeout" :
                 e && e.name === "TypeError" && /failed to fetch|networkerror|network request failed/i.test(message)
                 ? "network" : "other";
    return {ok: false, kind: kind, error: message};
  } finally {
    clearTimeout(timer);
  }
}"""


CLEARED_TIMEOUT = 10.0  # one ask must not outlast the wait it is meant to shorten
TITLE = re.compile(r"<title[^>]*>([^<]*)", re.I)


class FetchTransientError(BrowserError):
    """The page's generic fetch timed out or lost its network connection."""


def _payload(request: Request) -> dict[str, Any]:
    return {
        "method": request.method,
        "url": request.url,
        "headers": request.headers,
        "body": base64.b64encode(request.content).decode() if request.content is not None else None,
        "timeout_ms": int(request.timeout * 1000),
    }


def _response(result: dict[str, Any], url: str) -> Response:
    return Response(status=int(result["status"]), url=result["url"] or url,
                    headers={k.lower(): v for k, v in result["headers"].items()},
                    content=base64.b64decode(result["body"]))


class InPageBackend:
    transient_errors = (FetchTransientError,)

    def __init__(self, site: "Site", session: BrowserSession):
        self.site = site
        self.session = session
        self._tabs: dict[str, Tab] = {}

    def send(self, request: Request, *, before_send: Callable[[], None] = lambda: None) -> Response:
        payload = _payload(request)
        origin = origin_of(request.url)
        tab = self._tab(origin, before_send)
        before_send()
        try:
            result = tab.evaluate(FETCH_JS, payload)
        except TabLost as e:  # someone closed our tab or navigated it away: open a fresh one, once
            self._tabs.pop(origin, None)
            if not e.not_run and request.method not in RETRY_METHODS:
                # it closed mid-fetch: a write may have reached the site, so never resend it
                raise TabLost(f"ESUM's tab for {origin} closed while {request.method} {request.url} was "
                              f"in flight; it may already have been applied. Check the site before "
                              f"retrying.") from e
            tab = self._tab(origin, before_send)
            before_send()
            result = tab.evaluate(FETCH_JS, payload)
        if not result or not result.get("ok"):
            error = (result or {}).get("error", "no result")
            transient = (result or {}).get("kind") in ("timeout", "network")
            raise (FetchTransientError if transient else BrowserError)(
                f"in-page fetch of {request.url} failed: {error}")
        response = _response(result, request.url)
        check_login(self.site, response, self.session.where)
        return response

    def _tab(self, origin: str, before_send: Callable[[], None]) -> Tab:
        tab = self._tabs.get(origin)
        if tab is None:
            home = self._home(origin)
            tab = self._tabs[origin] = self.session.open_tab(home, self._cleared(home, before_send),
                                                               site=self.site.name)
        return tab

    def _cleared(self, home: str, before_send: Callable[[], None]) -> Callable[[Tab], bool] | None:
        """For a site that declares its check's text: whether a request from the tab gets a page
        with neither that text nor a check's title. Other sites are judged by the tab's title."""
        if not self.site.extras.get("challenge_text"):
            return None

        def cleared(tab: Tab) -> bool:
            before_send()
            request = Request("GET", home, headers={"Accept": "text/html"}, timeout=CLEARED_TIMEOUT)
            result = tab.evaluate(FETCH_JS, _payload(request))
            if not result or not result.get("ok"):
                return False
            response = _response(result, home)
            title = TITLE.search(response.text())
            return (response.ok and not is_challenged(response, self.site)
                    and not (title and CHALLENGE_TITLE.search(title.group(1))))
        return cleared

    def _home(self, origin: str) -> str:
        """The page a new tab loads: a page of the request's origin, preferably the site's own."""
        home = self.site.extras.get("browser_home")
        if home and not is_absolute(home):
            home = f"{origin_of(self.site.base_url or origin)}/{home.lstrip('/')}"
        for candidate in (home, self.site.base_url):
            if candidate and origin_of(candidate) == origin:
                return candidate
        return origin + "/"

    def close(self) -> None:
        tabs, self._tabs = list(self._tabs.values()), {}
        for tab in tabs:
            try:
                tab.close()
            except BrowserError:
                pass  # the browser or daemon is gone; nothing left to close


def make_backend(site: "Site", options: "Options", *, session: BrowserSession) -> InPageBackend:
    return InPageBackend(site, session)
