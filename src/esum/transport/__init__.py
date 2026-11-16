"""The fetch abstraction site code calls (`ctx.http`), and the backend interface transports implement.

Sites never talk to a transport directly. They call `ctx.http.get_json("/t/1.json")` and friends on an
`HttpClient`, which resolves URLs against the site's base URL, spaces requests per site across
processes, reuses identical GETs within one read action, retries HTTP 429, retries reads after
short-lived failures (502/503/504, timeouts, dropped connections), decodes text by charset and
raises `HttpError` on failure. The client sends requests through the
backend of the site's declared transport:

    Transport.DIRECT        esum.transport.direct        T1, httpx, no browser
    Transport.BROWSER_HTTP  esum.transport.browser_http  T2, curl_cffi Chrome fingerprint (+ browser cookies)
    Transport.IN_PAGE       esum.transport.in_page       T3, generic fetch() in a tab of the origin

Each browser backend exports `make_backend(site, options, *, session) -> Backend`; T1 takes
only `site, options`. Backend modules are imported
lazily on a site's first request, so T1 sites never import (or launch) anything browser-related.
A backend raises `LoginNeeded` when it detects a logged-out session; network errors pass through raw.

429 handling and charset detection are ported from the nitan_plugin userscript (My.user.js
`fetchJson` / `fetchNative`).
"""

from __future__ import annotations

import codecs
import importlib
import json as jsonlib
import re
import sys
import time
from dataclasses import dataclass, field, replace
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Callable, Protocol
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from ..core.throttle import DEFAULT_INTERVAL, Throttle
from ..core.errors import URL_CAP, HttpError, LoginNeeded, TransportFailure, UnsupportedURL
from ..core.text import clip
from ..core.urls import is_absolute, is_web_url
from ..core.model import Transport

if TYPE_CHECKING:
    from ..browser.session import BrowserSession
    from ..core.context import Options
    from ..core.model import Site

BACKEND_MODULES = {
    Transport.DIRECT: "esum.transport.direct",
    Transport.BROWSER_HTTP: "esum.transport.browser_http",
    Transport.IN_PAGE: "esum.transport.in_page",
}

# Site extras read here and by the browser layer, valid on every site (the registry reports any
# other key a site's engine or module doesn't declare in its `EXTRAS`).
SITE_EXTRAS = frozenset({
    "min_interval",     # core/throttle.py: per-site request spacing in seconds
    "challenge_text",   # this file: words only the site's own bot check has, for one a 200 can carry
    "browser_cookies",  # browser_http.py: False sends T2 without the browser's cookies
    "browser_home",     # in_page.py: the page a new T3 tab loads first
    "login_check",      # browser/login.py: what a logged-out response looks like
    "key_entry",        # core/keys_ui.py: the prompt for a key typed into `esum keys ui` (D-79)
})

RETRY_MAX = 3          # retries after HTTP 429
# Short-lived failures retry reads only: a write is never resent after one, since the first attempt
# may have reached the site. HTTP 429 is the exception: it is retried for every method, writes
# included, because a 429 means the server did not process the request.
TRANSIENT_STATUSES = frozenset({502, 503, 504, 522})
# A backend on another HTTP library adds its own classes as `transient_errors`.
TRANSIENT_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
TRANSIENT_WAITS = (1.0, 2.0)  # one wait per retry of a GET after a short-lived failure
RETRY_METHODS = frozenset({"GET", "HEAD"})
CHALLENGE_STATUSES = frozenset({403, 429, 503})
# FORMAT 1 also allowed these credentials in site/default headers (D-46).
PAGE_SECRET_HEADERS = frozenset({"user-api-key", "user-api-client-id"})
MAX_WAIT_SECONDS = 30  # a longer requested wait fails the call instead (the agent decides) …
OPTED_IN_MAX_WAIT = 300  # … unless the caller set --max-retries, which says it will wait
DEFAULT_TIMEOUT = 30.0
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 20
_DEFAULT_PORTS = {"http": 80, "https": 443}


def keeps_credential(origin: str, target: str) -> bool:
    """Whether a redirect from `origin` to `target` may carry the request's credential: only to the
    same host and port, or from http to https on the same host; never to another host or down to
    http. `HttpClient` follows a keyed request's redirects itself and drops the credential otherwise."""
    a, b = urlsplit(origin), urlsplit(target)
    if (a.hostname or "").lower() != (b.hostname or "").lower():
        return False
    if a.scheme == b.scheme:
        return (a.port or _DEFAULT_PORTS.get(a.scheme)) == (b.port or _DEFAULT_PORTS.get(b.scheme))
    return a.scheme == "http" and b.scheme == "https"


def redirected_method(method: str, status: int) -> str:
    """The method a browser uses after a redirect: GET after 303, and after 301/302 from a POST."""
    if status == 303 and method != "HEAD":
        return "GET"
    if status in (301, 302) and method == "POST":
        return "GET"
    return method


def _credential_pieces(credential: "Credential | None") -> list[str]:
    """Every form a credential's header values can take in an error message, longest first."""
    pieces: set[str] = set()
    for v in (credential.headers.values() if credential else ()):
        if not v:
            continue
        pieces |= {v, repr(v)[1:-1], repr(v.encode("utf-8", "replace"))[2:-1]}
        pieces |= {p for p in re.split(r"[\r\n]+", v) if len(p) >= 6}
    return sorted(pieces, key=len, reverse=True)


def _error_texts(error: BaseException) -> list[str]:
    """All the text an error can show: its message, repr and attributes, and the same for every
    cause and context under it (a debug traceback prints those too)."""
    texts, seen, queue = [], set(), [error]
    while queue:
        e = queue.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        texts += [str(e), repr(e)] + [str(a) for a in getattr(e, "args", ())]
        texts += [str(v) for v in vars(e).values() if isinstance(v, (str, bytes))]
        texts += [str(getattr(e, name, "") or "") for name in ("strerror", "filename", "filename2")]
        queue += [e.__cause__, e.__context__, *getattr(e, "exceptions", ())]
    return texts


def without_credential(error: BaseException, credential: "Credential | None") -> BaseException:
    """`error` itself when nothing it could show quotes the credential; otherwise a TransportFailure
    carrying its type name and message with the credential removed. An HTTP library can quote a
    header it refused (`Illegal header value b'...'`), and that text reaches output. The caller
    raises the replacement `from None`, so the original and its chain are never printed."""
    pieces = _credential_pieces(credential)
    if not pieces or not any(p in text for text in _error_texts(error) for p in pieces):
        return error
    text = str(error)
    for p in pieces:
        text = text.replace(p, "[credential]")
    return TransportFailure(f"{type(error).__name__}: {text}")


@dataclass(repr=False)
class Credential:
    """Request authentication, with an optional separately verified page-safe identity.

    Header credentials stay outside untrusted pages unless explicitly declared page-safe.
    Verification belongs to the credential's owner and runs before each actual attempt.
    """

    headers: dict[str, str] = field(default_factory=dict)
    page_safe: bool = False
    fallback: Credential | None = None
    verify: Callable[[], None] | None = None

    def for_transport(self, transport: Transport) -> Credential | None:
        if transport is Transport.IN_PAGE and not self.page_safe:
            return self.fallback.for_transport(transport) if self.fallback else None
        return self


@dataclass
class Request:
    method: str
    url: str                      # absolute, query string included
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes | None = None
    timeout: float = DEFAULT_TIMEOUT
    credential: Credential | None = field(default=None, repr=False)
    follow_redirects: bool = True   # False: the backend returns a 3xx as it is (T1, T2; a page always follows)


@dataclass
class Response:
    status: int
    url: str                      # final URL after redirects
    headers: dict[str, str]       # lower-cased names
    content: bytes
    charset: str | None = None    # the site's declared charset; overrides the response's
    reused: bool = False          # an identical earlier GET of this action answered it
    transport: Transport | None = None
    credential: Credential | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        return decode(self.content, self.charset or charset_of(self.headers, self.content))

    def json(self) -> Any:
        return jsonlib.loads(self.text())

    def http_error(self, body: str | None = None) -> HttpError:
        """The HttpError for this failed response; callers that inspect a check=False response raise
        this rather than build their own. `body` defaults to the start of the response text. A 429
        carries the wait the site asked for (`.wait`), and a Cloudflare page's HTML is left out,
        since it is not the site's error text (D-61, D-71)."""
        if body is None:
            body = self.text()[:2000]
        if self.status == 429 and is_cloudflare_challenge(self.status, self.headers, self.text()):
            body = ""
        error = HttpError(self.status, self.url, body)
        if self.status == 429:
            error.wait = requested_wait(self)
        return error


class Backend(Protocol):
    """What a transport implements. `send` performs one request; it must not retry, throttle or reuse
    responses. It follows redirects only when `request.follow_redirects` says so. It calls `before_send` (the site throttle) after any slow set-up, right before the
    request goes out."""

    def send(self, request: Request, *, before_send: Callable[[], None] = lambda: None) -> Response: ...

    def close(self) -> None: ...


BackendFactory = Callable[["Site", "Options"], Backend]


# --- charset -----------------------------------------------------------------------------------------

# Browsers decode these labels as windows-1252 (WHATWG Encoding); FlyerTalk's "ISO-8859-1" pages use
# its curly quotes, which real Latin-1 would turn into control characters.
_WINDOWS_1252 = {"iso-8859-1", "iso8859-1", "latin1", "latin-1", "l1", "us-ascii", "ascii",
                 "windows-1252", "cp1252"}


def charset_of(headers: dict[str, str], content: bytes) -> str:
    """Charset from Content-Type, else a <meta> tag in the first 4 KB, else UTF-8."""
    m = re.search(r"charset=[\"']?([\w.:-]+)", headers.get("content-type", ""), re.I)
    if not m:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w.:-]+)", content[:4096], re.I)
        return m.group(1).decode("ascii", "replace") if m else "utf-8"
    return m.group(1)


def codec(charset: str | None) -> str:
    """The Python codec for a charset label: windows-1252 for the Latin-1 labels, UTF-8 when the
    label is missing or unknown."""
    label = (charset or "utf-8").strip().lower()
    if label in _WINDOWS_1252:
        return "cp1252"
    try:
        return codecs.lookup(label).name
    except LookupError:
        return "utf-8"


def decode(content: bytes, charset: str) -> str:
    return content.decode(codec(charset), errors="replace")


def form_encode(data: dict[str, Any], charset: str | None) -> str:
    """Query string / form body in the site's charset, as a browser submits the site's own forms
    (vBulletin 3 reads a UTF-8 "ü" as Latin-1 "Ã¼"). Characters the charset lacks become &#N;."""
    return urlencode(data, doseq=True, encoding=codec(charset), errors="xmlcharrefreplace")


# --- 429 ---------------------------------------------------------------------------------------------

def retry_wait(response: Response, attempt: int) -> float:
    """Seconds to wait before retrying a 429: what the site asked for, else exponential backoff 2, 4, 8 s."""
    wait = requested_wait(response)
    return 2.0 * 2 ** attempt if wait is None else wait


def requested_wait(response: Response) -> float | None:
    """The wait a 429 asks for: Retry-After (seconds or HTTP date), else Discourse's
    `{"extras": {"wait_seconds": N}}` + 1, else None."""
    header = response.headers.get("retry-after", "").strip()
    if header:
        if re.fullmatch(r"\d+(\.\d+)?", header):
            return float(header)
        try:
            return max(0.0, parsedate_to_datetime(header).timestamp() - time.time())
        except (TypeError, ValueError):
            pass
    try:
        wait = float(jsonlib.loads(response.content.decode("utf-8", "replace"))["extras"]["wait_seconds"])
        if wait > 0:
            return wait + 1
    except (ValueError, KeyError, TypeError):
        pass
    return None


def is_cloudflare_challenge(status: int, headers: dict[str, str], body: str) -> bool:
    """A Cloudflare interstitial instead of the site's answer (heuristics from nitan-mcp's HTTP
    client, src/http/client.ts, MIT). The request never reached the site, so resending it is safe."""
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if status not in CHALLENGE_STATUSES:
        return False
    low = body[:20000].lower()
    return ("just a moment" in low or "/cdn-cgi/challenge-platform/" in low
            or "cf-challenge" in low or "attention required" in low)


def is_challenged(resp: "Response", site: "Site") -> bool:
    """Whether a response carries a Cloudflare or AWS WAF challenge or the site's declared check text."""
    marker = site.extras.get("challenge_text")
    if marker and marker in resp.text():
        return True
    if resp.headers.get("x-amzn-waf-action", "").lower() == "challenge":    # AWS WAF, even with no body
        return True
    if resp.status not in CHALLENGE_STATUSES and "cf-mitigated" not in resp.headers:
        return False
    try:
        body = resp.text()
    except (LookupError, ValueError):
        body = ""
    return is_cloudflare_challenge(resp.status, resp.headers, body)


# --- backends ----------------------------------------------------------------------------------------

def load_backend(site: "Site", options: "Options", *, session: "BrowserSession | None" = None) -> Backend:
    module_name = BACKEND_MODULES[site.transport]
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as e:
        if e.name != module_name:
            raise
        raise RuntimeError(f"transport {site.transport.label} is not available in this esum build "
                           f"({module_name} is missing)") from None
    if site.transport is Transport.DIRECT:
        return module.make_backend(site, options)
    if session is None:
        from ..browser.session import session_for
        session = session_for(options.browser, options.cdp)
    return module.make_backend(site, options, session=session)


class ChallengeError(HttpError):
    """The last available transport returned a bot challenge rather than usable data."""

    def __init__(self, response: Response, site: "Site", *, where: str):
        super().__init__(response.status, response.url)
        cloudflare = is_cloudflare_challenge(response.status, response.headers, response.text())
        page = "a bot check page" + (" (Cloudflare challenge)" if cloudflare else "")
        self.args = (f"HTTP {response.status} from {clip(response.url, URL_CAP)}: {page} instead of the site's response",)
        challenge = "a Cloudflare challenge" if cloudflare else "a bot check"
        check = "Cloudflare check" if cloudflare else "bot check"
        if site.transport is Transport.DIRECT:
            self.help = (f"{site.name} is behind {challenge}, so plain HTTP (T1) can't read it; "
                         "declare the site with transport=Transport.BROWSER_HTTP (T2) or IN_PAGE (T3)")
        elif site.transport is Transport.BROWSER_HTTP and not site.extras.get("browser_cookies", True):
            self.help = (f"Chrome's network fingerprint (T2) no longer gets past {site.name}'s {check}; "
                         "declare the site with transport=Transport.IN_PAGE (T3)")
        else:
            self.help = (f"wait about 30 s and rerun the same command; if the {check} persists, ask the user "
                         f"to open {site.name} in {where or 'the browser ESUM uses'} and let the check finish, then rerun")


class HttpClient:
    """The fetching interface Context owns and the runner closes after an action and its writes."""

    def __init__(self, site: "Site", options: "Options", *, dedupe: bool,
                 backend_factory: BackendFactory | None = None,
                 session: "BrowserSession | None" = None,
                 throttle: Throttle | None = None, sleep: Callable[[float], None] = time.sleep):
        self.site = site
        self.options = options
        self.dedupe = dedupe            # only tier-0 actions reuse identical GETs
        self.backend_factory = backend_factory
        self._session = session
        self._backend: Backend | None = None
        self._throttle = throttle
        self._sleep = sleep
        self._seen: dict[str, Response] = {}  # in memory, gone when the call ends; nothing on disk

    def url(self, path: str, params: dict[str, Any] | None = None) -> str:
        """`t/1.json` or `/t/1.json` -> base_url + `/t/1.json`; an absolute URL (`x://…`) as is.
        Only a web URL comes back (core/urls.py): anything else raises UnsupportedURL."""
        if is_absolute(path):
            url = path
        else:
            if not self.site.base_url:
                raise ValueError(f"{self.site.name} has no base_url; pass an absolute URL")
            url = f"{self.site.base_url}/{path.lstrip('/')}"
        if not is_web_url(url):
            raise UnsupportedURL(f"{self.site.name}: refusing to request {url!r}: not an http(s) URL")
        parts = urlsplit(url)
        if params:
            query = form_encode({k: v for k, v in params.items() if v is not None}, self.site.charset)
            if query:
                url += ("&" if parts.query else "?") + query
        return url

    def backend(self) -> Backend:
        if self._backend is None:
            if self.backend_factory is not None:
                self._backend = self.backend_factory(self.site, self.options)
            else:
                session = self.browser_session if self.site.transport is not Transport.DIRECT else None
                self._backend = load_backend(self.site, self.options, session=session)
        return self._backend

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                headers: dict[str, str] | None = None, data: dict[str, Any] | None = None,
                json: Any = None, content: bytes | None = None, check: bool = True,
                timeout: float = DEFAULT_TIMEOUT, dedupe: bool | None = None,
                credential: Credential | None = None, follow_redirects: bool = True) -> Response:
        """Send a request. Non-2xx raises HttpError unless `check=False`; challenges always raise.

        `follow_redirects=False` returns a 3xx as it is (with `check=False`), so a site that must
        only ever be asked for fixed paths is never taken anywhere else with the user's cookies
        (T1 and T2; a page on T3 always follows).

        `credential` owns authentication and verification; its page-safe fallback is selected
        before an escalated send, and the response records the identity actually used.

        Within one tier-0 action, a successful GET is sent once and its response reused for
        identical GETs (same URL and headers); `dedupe=False` opts a read out. Nothing is kept
        between calls (D-18): browser transports answer as whoever is logged in, which no key can
        see, and a live answer is what the agent asked for."""
        method = method.upper()
        hdrs = {**self.site.headers, **(headers or {})}
        if json is not None:
            content = jsonlib.dumps(json).encode()
            hdrs.setdefault("Content-Type", "application/json")
        elif data is not None:
            content = form_encode(data, self.site.charset).encode("ascii")
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        req = Request(method=method, url=self.url(path, params), headers=hdrs, content=content,
                      timeout=timeout, credential=credential, follow_redirects=follow_redirects)

        key = None
        selected = credential.for_transport(self.site.transport) if credential else None
        if method == "GET" and self.dedupe and dedupe is not False:
            key = jsonlib.dumps([req.url, sorted(req.headers.items()),
                                 sorted(selected.headers.items()) if selected else None])
            if (seen := self._seen.get(key)) is not None:
                return replace(seen, reused=True)

        resp = self._send_with_retry(req)
        resp.charset = self.site.charset
        if check and not resp.ok:
            raise resp.http_error()
        if key and resp.ok:
            self._seen[key] = resp
        return resp

    @property
    def throttle(self) -> Throttle:
        if self._throttle is None:
            self._throttle = Throttle(sleep=self._sleep)
        return self._throttle

    def _send_once(self, backend: Backend, sent: Request,
                   credential: Credential | None) -> tuple[Response, Credential | None]:
        """One request, and the credential its answer was sent with. A request whose credential adds
        headers has its redirects followed here, hop by hop, because a library would carry a header
        such as `x-api-key` to any host: the credential stays on the request's own origin and is
        dropped, for the rest of the chain, from the first hop that leaves it (`keeps_credential`).
        Page-safe credentials on T3 are the page's own and follow as the page does."""
        def wait() -> None:
            self.throttle.wait(self.site.name, self._interval())

        secret = {k.lower() for k in (credential.headers if credential else {})}
        if not secret or self.site.transport is Transport.IN_PAGE or not sent.follow_redirects:
            return backend.send(sent, before_send=wait), credential
        hop, before = replace(sent, follow_redirects=False), wait
        for _ in range(MAX_REDIRECTS + 1):
            resp = backend.send(hop, before_send=before)
            before = lambda: None     # the site's spacing applies to the request, not to each hop
            location = resp.headers.get("location")
            if resp.status not in REDIRECT_STATUSES or not location:
                return resp, credential
            target = urljoin(resp.url, location)
            method = redirected_method(hop.method, resp.status)
            hop = replace(hop, url=target, method=method, content=hop.content if method == hop.method else None)
            if not keeps_credential(sent.url, target):
                plain = {k: v for k, v in hop.headers.items() if k.lower() not in secret}
                return backend.send(replace(hop, headers=plain, credential=None, follow_redirects=True)), None
        raise httpx.TooManyRedirects(f"Exceeded {MAX_REDIRECTS} redirects.")

    def _send_with_retry(self, req: Request) -> Response:
        host = urlsplit(req.url).hostname or ""
        rate_limited = transient = 0
        while True:
            credential = req.credential.for_transport(self.site.transport) if req.credential else None
            if credential is not None and credential.verify is not None:
                credential.verify()
            # A verification read can escalate too; verify the newly selected identity first.
            if req.credential and req.credential.for_transport(self.site.transport) is not credential:
                continue
            headers = {**req.headers, **(credential.headers if credential else {})}
            if self.site.transport is Transport.IN_PAGE:
                headers = {k: v for k, v in headers.items() if k.lower() not in PAGE_SECRET_HEADERS}
                if credential and any(k.lower() in PAGE_SECRET_HEADERS for k in credential.headers):
                    credential = replace(credential, headers={k: v for k, v in credential.headers.items()
                                                             if k.lower() not in PAGE_SECRET_HEADERS})
            sent = replace(req, headers=headers, credential=credential)
            backend = self.backend()
            try:
                resp, credential = self._send_once(backend, sent, credential)
            except Exception as e:
                transient_errors = TRANSIENT_ERRORS + getattr(backend, "transient_errors", ())
                if not isinstance(e, transient_errors) or not self._may_retry(req, transient):
                    clean = without_credential(e, credential)
                    if clean is e:
                        raise
                    raise clean from None
                self._retry_later(host, type(e).__name__, TRANSIENT_WAITS[transient])
                transient += 1
                continue
            resp = replace(resp, transport=self.site.transport, credential=credential)
            # A 429 on the last transport is rate limiting, even behind a Cloudflare page: it waits
            # and retries below and ends as a plain HTTP 429, never a check (D-71).
            if is_challenged(resp, self.site) and not (resp.status == 429 and self.site.fallback is None):
                # Waiting won't clear a challenge. A site with a fallback resends the request, write or
                # not, on the stronger transport and stays there for the rest of the call (D-40);
                # otherwise no usable data remains, even for check=False callers.
                if self.site.fallback is None:
                    resp.charset = self.site.charset
                    raise ChallengeError(resp, self.site, where=self.login_where if self.uses_browser_login else "")
                self._escalate(host)
                rate_limited = transient = 0
                continue
            retries = RETRY_MAX if self.options.max_retries is None else self.options.max_retries
            if resp.status == 429 and rate_limited < retries:
                wait = retry_wait(resp, rate_limited)
                if wait > (MAX_WAIT_SECONDS if self.options.max_retries is None else OPTED_IN_MAX_WAIT):
                    error = resp.http_error(f"rate limited by {host}; it asks to wait {wait:.0f}s before "
                                            f"retrying (--max-retries waits up to {OPTED_IN_MAX_WAIT}s)")
                    error.wait = wait
                    raise error
                self.throttle.cool_down(self.site.name, wait)   # parallel ESUM calls to the site wait too
                self._retry_later(host, "rate-limited the request (HTTP 429)", wait)
                rate_limited += 1
                continue
            if resp.status in TRANSIENT_STATUSES and self._may_retry(req, transient):
                self._retry_later(host, f"answered HTTP {resp.status}", TRANSIENT_WAITS[transient])
                transient += 1
                continue
            return resp

    def _escalate(self, host: str) -> None:
        to = self.site.fallback
        print(f"esum: {host} answered {self.site.transport.short} with a bot check; "
              f"switching to {to.label} for this call", file=sys.stderr)
        self.close()
        self.site = replace(self.site, transport=to, fallback=None)

    def _interval(self) -> float | None:
        """The site's spacing, raised (never lowered) by `--min-interval`."""
        own = self.site.extras.get("min_interval")
        wanted = self.options.min_interval
        if wanted is None:
            return own
        return max(wanted, DEFAULT_INTERVAL if own is None else own)

    @staticmethod
    def _may_retry(req: Request, transient: int) -> bool:
        return req.method in RETRY_METHODS and transient < len(TRANSIENT_WAITS)

    def _retry_later(self, host: str, what: str, wait: float) -> None:
        print(f"esum: {host} {what}; retrying in {wait:.0f}s", file=sys.stderr)
        self._sleep(wait)

    def get(self, path: str, **kw: Any) -> Response:
        return self.request("GET", path, **kw)

    def get_text(self, path: str, **kw: Any) -> str:
        return self.get(path, **kw).text()

    def get_json(self, path: str, **kw: Any) -> Any:
        headers = {"Accept": "application/json", **(kw.pop("headers", None) or {})}
        return self.get(path, headers=headers, **kw).json()

    def wait_turn(self) -> None:
        """Wait out the site's request spacing, for a caller that reaches the site another way."""
        self.throttle.wait(self.site.name, self._interval())

    @property
    def browser_session(self) -> "BrowserSession":
        """Shared session dependency; getting it never opens a browser."""
        if self._session is None:
            from ..browser.session import session_for
            self._session = session_for(self.options.browser, self.options.cdp)
        return self._session

    @property
    def uses_browser_login(self) -> bool:
        """Whether the active transport sends the browser's login."""
        return (self.site.transport is Transport.IN_PAGE or
                self.site.transport is Transport.BROWSER_HTTP and self.site.extras.get("browser_cookies", True))

    @property
    def login_where(self) -> str:
        """The login place for this client's active transport, including after escalation."""
        return self.browser_session.where

    def login_needed(self, detail: str | None = None, where: str | None = None) -> LoginNeeded:
        return LoginNeeded(self.site.name, self.site.base_url, where=where or self.login_where, detail=detail)

    def close(self) -> None:
        if self._backend is not None:
            self._backend.close()
            self._backend = None
