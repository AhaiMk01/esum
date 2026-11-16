"""T2 / T3 transports and the browser session layer, against a fake patchright-cli daemon.

`FakeDaemon` stands in for `DaemonClient`: it keeps named sessions with a default tab and named
tabs the way patchright-cli 0.11 does, and "evaluates" only the generic JS ESUM sends (it recognizes
each function by its source, so any new in-page code fails these tests until it is added here).
"""

from __future__ import annotations

import base64
import contextlib
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
import pytest

from esum.browser import session as session_mod
from esum.browser.daemon import BrowserError
from esum.browser.session import _PAGE_STATE, _VIEWPORT, BrowserSession, TabLost
from esum.core.context import Options
from esum.core.errors import CHECK_NEEDED, CheckNeeded, LoginNeeded
from esum.core.model import Site, Transport
from curl_cffi.requests import exceptions as cffi_errors

from esum.transport import HttpClient, Request, Response, browser_http, in_page
from esum.transport.in_page import FETCH_JS

KNOWN_FNS = {FETCH_JS: "fetch", _PAGE_STATE: "state", _VIEWPORT: "viewport"}


class FakeTab:
    def __init__(self, url: str, title: str = "Page"):
        self.url = url
        self.titles = [title]

    @property
    def origin(self) -> str:
        p = urlsplit(self.url)
        return f"{p.scheme}://{p.netloc}"

    def title(self) -> str:
        return self.titles.pop(0) if len(self.titles) > 1 else self.titles[0]


def page_response(tab, req):
    return {"ok": True, "status": 200, "url": req["url"], "headers": {"Content-Type": "text/html"},
            "body": __import__("base64").b64encode(f"<p>{req['method']} {req['url']}</p>".encode()).decode()}


class FakeDaemon:
    """patchright-cli 0.11.1 as ESUM uses it: sessions with a default tab plus named tabs (`tab=`
    option). Commands name their tab; a missing named tab fails with code `tab_not_open`, a missing
    session with `session_not_open`."""

    def __init__(self, responder=page_response, titles: dict[str, list[str]] | None = None,
                 cookies: list[dict] | None = None, delay: float = 0.0):
        self.sessions: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fns: list[str] = []
        self.responder = responder
        self.titles = titles or {}
        self.cookies = cookies or []
        self.delay = delay
        self.eval_delay = 0.0   # time an eval spends "in the page", outside the daemon's mutex
        self.before_eval = None
        self.viewport = {"width": 1037, "height": 739, "zoom": 1}  # the _VIEWPORT reply
        self.on_state = None
        self.on_viewport = None  # called with the tab before the _VIEWPORT read; may raise or move it
        self._mutex = threading.Lock()
        self._sessions = {}
        self.sleep = lambda seconds: None
        self.clock = time.monotonic

    def session(self, options=None):
        options = options or Options()
        key = (options.browser, options.cdp)
        if key not in self._sessions:
            self._sessions[key] = BrowserSession(*key, client=self, sleep=self.sleep, clock=self.clock)
        return self._sessions[key]

    def evaluate(self, fn, arg=None, *, origin, session, tab):
        return json.loads(self.command("eval", ((origin, fn, arg),), session=session, tab=tab))

    def tabs(self, session="esum") -> dict[str, FakeTab]:
        return self.sessions[session]["tabs"]

    def named(self, session="esum") -> list[str]:
        return [n for n in self.tabs(session) if n != "default"]

    def command(self, command, args=(), *, session, **options):
        with self._mutex:
            self.calls.append((command, tuple(args), session, options))
        if self.delay:
            time.sleep(self.delay)  # let parallel callers interleave between commands
        if command == "eval" and self.eval_delay:
            time.sleep(self.eval_delay)  # the page works; other tabs' commands go on meanwhile
        with self._mutex:
            return self._run(command, list(args), session, options)

    def _not_open(self, name, tab):
        return BrowserError(f"patchright-cli (session {name!r}): Tab '{tab}' is not open in session "
                            f"'{name}'. Run `patchright-cli --tab {tab} open <url>` first.", code="tab_not_open")

    def _run(self, command, args, name, options):
        tab = options.get("tab")
        if command == "background-open":
            self.sessions.setdefault(name, {"tabs": {"default": FakeTab("about:blank", "")},
                                            "options": options})
            self.sessions[name]["options"]["background"] = True
            command = "open"
        if command in ("open", "attach") and tab is None:
            self.sessions.setdefault(name, {"tabs": {"default": FakeTab("about:blank", "")},
                                            "options": options})
            return "### Page"
        s = self.sessions.get(name)
        if s is None:
            raise BrowserError(f"patchright-cli {command} (session {name!r}): "
                               f"Session '{name}' is not open. Run 'open' first.", code="session_not_open")
        tabs = s["tabs"]
        if command == "open":
            tabs[tab] = FakeTab(args[0], "Page")
            tabs[tab].titles = list(self.titles.get(args[0], ["Page"]))
            return "### Page"
        if command == "tab-list":
            return "### Tabs\n" + "\n".join(f"  [{i}] ({n}) {t.url} — t" for i, (n, t) in enumerate(tabs.items()))
        if command == "cookie-list":
            return json.dumps(self.cookies, indent=2)
        if command == "close" and tab is not None:
            if tab not in tabs:
                raise self._not_open(name, tab)
            del tabs[tab]
            return f"Tab '{tab}' closed."
        if command in ("close", "detach"):
            del self.sessions[name]
            return f"Session '{name}' {command}ed."
        if command == "eval":
            if tab not in tabs:
                raise self._not_open(name, tab)
            if self.before_eval:
                self.before_eval(s)
            return json.dumps(self._eval(tabs[tab], args[0]), indent=2)
        raise AssertionError(f"unexpected command {command}")

    def _eval(self, tab: FakeTab, expr: str):
        if expr == "navigator.userAgent":
            return "Mozilla/5.0 FakeChrome/140"
        if expr == "location.href":
            return tab.url
        origin, fn, arg = expr
        assert fn in KNOWN_FNS, f"unknown in-page function: {fn[:80]}"
        self.fns.append(KNOWN_FNS[fn])
        if fn == _PAGE_STATE and self.on_state:
            self.on_state(tab)
        if fn == _VIEWPORT and self.on_viewport:
            self.on_viewport(tab)
        if tab.origin.lower() != origin:
            return {"esum_tab": False}
        if fn == FETCH_JS:
            value = self.responder(tab, arg)
        elif fn == _VIEWPORT:
            value = self.viewport
        else:
            value = {"title": tab.title(), "href": tab.url}
        return {"esum_tab": True, "value": value}

    def names(self):
        return [c[0] for c in self.calls]

    def session_opens(self):
        """`open`/`attach` calls that open a session (not a named tab)."""
        return [c for c in self.calls if c[0] in ("open", "background-open", "attach") and "tab" not in c[3]]


def make_site(transport=Transport.IN_PAGE, **kw) -> Site:
    kw.setdefault("base_url", "https://forum.example")
    return Site(name="ex", engine=None, transport=transport, label={"en": "Example"}, **kw)


@pytest.fixture
def fake():
    return FakeDaemon()


def client_for(site, options=None, *, fake) -> HttpClient:
    return HttpClient(site, options or Options(), dedupe=False, session=fake.session())


# --- laziness ----------------------------------------------------------------------------------------

def test_t1_never_imports_the_browser_layer():
    code = ("import sys\n"
            "from esum.core.model import Site, Transport\n"
            "from esum.core.context import Options\n"
            "from esum.transport import ChallengeError, HttpClient, Response, load_backend\n"
            "from types import SimpleNamespace\n"
            "s = Site(name='x', engine=None, base_url='https://x.invalid', transport=Transport.DIRECT, label={'en': 'x'})\n"
            "load_backend(s, Options()).close()\n"
            "backend = SimpleNamespace(send=lambda req, **kw: Response(403, req.url, {'cf-mitigated': 'challenge'}, b'check'), close=lambda: None)\n"
            "client = HttpClient(s, Options(), dedupe=False, backend_factory=lambda s, o: backend)\n"
            "try: client.get('/x')\n"
            "except ChallengeError: pass\n"
            "bad = [m for m in sys.modules if m.startswith(('esum.browser', 'esum.transport.in_page', 'esum.transport.browser_http'))]\n"
            "print(bad)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_making_a_backend_starts_nothing(fake):
    in_page.make_backend(make_site(), Options(), session=fake.session(Options())).close()
    browser_http.make_backend(make_site(Transport.BROWSER_HTTP), Options(), session=fake.session(Options())).close()
    assert fake.calls == []


# --- T3 ----------------------------------------------------------------------------------------------

def test_t3_opens_managed_session_and_fetches_in_a_named_tab(fake, isolated):
    http = client_for(make_site(), fake=fake)
    try:
        assert http.get_text("/t/1") == "<p>GET https://forum.example/t/1</p>"
    finally:
        http.close()
    (opened,) = fake.session_opens()
    assert opened[2] == "esum"
    assert opened[3] == {"persistent": True, "profile": str(isolated / "browser" / "profile"),
                         "background": True}
    tab_open = next(c for c in fake.calls if c[0] in ("open", "background-open") and "tab" in c[3])
    assert tab_open[1] == ("https://forum.example",) and tab_open[3]["tab"].startswith("esum-")
    assert "fetch" in fake.fns
    assert all(c[3].get("tab") for c in fake.calls if c[0] == "eval")  # never the default tab
    assert fake.named() == []  # our tab was closed


def test_t3_returns_raw_bytes_and_charset_decoding_happens_in_the_client(fake):
    import base64
    fake.responder = lambda tab, req: {"ok": True, "status": 200, "url": req["url"],
                                       "headers": {"Content-Type": "text/html; charset=ISO-8859-1"},
                                       "body": base64.b64encode(b"\x93caf\xe9\x94").decode()}
    http = client_for(make_site(charset="iso-8859-1"), fake=fake)
    try:
        resp = http.get("/")
    finally:
        http.close()
    assert resp.content == b"\x93caf\xe9\x94"
    assert resp.headers["content-type"] == "text/html; charset=ISO-8859-1"
    assert resp.text() == "“café”"


def test_t3_sends_method_headers_body_and_timeout(fake):
    seen = []
    fake.responder = lambda tab, req: (seen.append(req), page_response(tab, req))[1]
    backend = in_page.make_backend(make_site(), Options(), session=fake.session(Options()))
    try:
        backend.send(Request("POST", "https://forum.example/api", {"X-A": "1"}, b"\x00\xffbody", 12.5))
    finally:
        backend.close()
    import base64
    assert seen[0]["method"] == "POST" and seen[0]["headers"] == {"X-A": "1"}
    assert base64.b64decode(seen[0]["body"]) == b"\x00\xffbody"
    assert seen[0]["timeout_ms"] == 12500


def test_t3_reuses_one_tab_per_origin(fake):
    backend = in_page.make_backend(make_site(), Options(), session=fake.session(Options()))
    try:
        backend.send(Request("GET", "https://forum.example/a"))
        backend.send(Request("GET", "https://forum.example/b"))
        assert len(fake.named()) == 1
        backend.send(Request("GET", "https://cdn.example/x.json"))  # another origin, another tab
        assert len(fake.named()) == 2
        assert any(c[0] in ("open", "background-open") and c[1] == ("https://cdn.example/",) for c in fake.calls)
    finally:
        backend.close()
    assert fake.named() == [] and list(fake.tabs()) == ["default"]
    assert "tab-select" not in fake.names()


def test_t3_home_page_override(fake):
    backend = in_page.make_backend(make_site(extras={"browser_home": "/forum/index.php"}), Options(), session=fake.session(Options()))
    try:
        backend.send(Request("GET", "https://forum.example/x"))
    finally:
        backend.close()
    assert any(c[0] in ("open", "background-open") and c[1] == ("https://forum.example/forum/index.php",) for c in fake.calls)


def test_t3_reopens_a_tab_that_was_closed_under_it(fake):
    backend = in_page.make_backend(make_site(), Options(), session=fake.session(Options()))
    try:
        backend.send(Request("GET", "https://forum.example/a"))
        del fake.tabs()[fake.named()[0]]  # someone closed our tab
        assert backend.send(Request("GET", "https://forum.example/b")).status == 200
        assert len([c for c in fake.calls if c[0] in ("open", "background-open") and "tab" in c[3]]) == 2
    finally:
        backend.close()
    assert fake.named() == []


def test_tab_lost_is_raised_when_our_tab_left_the_origin(fake):
    s = BrowserSession(client=fake)
    tab = s.open_tab("https://forum.example/")
    fake.tabs()[tab.name].url = "https://elsewhere.example/"
    with pytest.raises(TabLost):
        tab.evaluate(_PAGE_STATE)


def test_t3_waits_for_an_anti_bot_interstitial(fake, capsys):
    fake.titles["https://forum.example"] = ["Just a moment...", "Just a moment...", "Forum home"]
    backend = in_page.make_backend(make_site(), Options(), session=fake.session(Options()))
    try:
        assert backend.send(Request("GET", "https://forum.example/a")).status == 200
    finally:
        backend.close()
    assert fake.fns.count("state") == 3
    assert "anti-bot check to clear" in capsys.readouterr().err


def test_t3_interstitial_reload_during_evaluation_is_waited_out(fake):
    fails = iter([True])

    def flaky(s):
        if next(fails, False):
            raise BrowserError("patchright-cli eval: Error: Execution context was destroyed")

    tab = BrowserSession(client=fake, sleep=lambda x: None).open_tab("https://forum.example/")
    fake.before_eval = flaky
    tab.wait_for_challenge()
    assert fake.fns.count("state") == 2


CHECK_MARK = 'name="js_challenge"'
CHECK_TITLE = "Example - Prove your humanity"
CHECK_SITE = make_site(extras={"challenge_text": CHECK_MARK})


def _body(text, status=200):
    return {"ok": True, "status": status, "url": "https://forum.example/", "headers": {"Content-Type": "text/html"},
            "body": base64.b64encode(text.encode()).decode()}


@pytest.fixture
def clock(fake):
    """Time that moves only when the wait sleeps, so a 20 s wait runs at once. Returns the sleeps."""
    now, sleeps = [0.0], []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    fake.sleep = sleep
    fake.clock = lambda: now[0]
    fake._sessions.clear()
    return sleeps


def _first_request(site, options=None, *, fake):
    """One call's first request on `site`; an error in it doesn't matter to these tests. Returns
    the CHECK NEEDED it raised, if any."""
    backend = in_page.make_backend(site, options or Options(), session=fake.session(options or Options()))
    try:
        with contextlib.suppress(BrowserError):
            backend.send(Request("GET", "https://forum.example/a"))
    except CheckNeeded as e:
        return e
    finally:
        backend.close()
    return None


def test_t3_a_check_title_that_outlives_the_check_ends_the_wait_at_once(fake, clock, capsys):
    fake.titles["https://forum.example"] = [CHECK_TITLE]
    fake.responder = lambda tab, req: _body("<title>Forum home</title><p>the page</p>")
    _first_request(CHECK_SITE, fake=fake)
    assert clock == []
    err = capsys.readouterr().err
    assert "may need to complete it" not in err and "anti-bot check" not in err


def test_t3_a_check_that_clears_during_the_wait_ends_it_there(fake, clock, capsys):
    pages = iter([f"<title>{CHECK_TITLE}</title><input {CHECK_MARK}>"] * 2)
    fake.titles["https://forum.example"] = [CHECK_TITLE]
    fake.responder = lambda tab, req: _body(next(pages, "<title>Forum home</title>"))
    _first_request(CHECK_SITE, fake=fake)
    assert 0 < sum(clock) < session_mod.CHALLENGE_WAIT
    assert "may need to complete it" not in capsys.readouterr().err


def test_t3_a_check_with_a_plain_title_waits_until_requests_get_pages(fake, clock):
    """AWS WAF's check page has no telling title, and its answer to a fetch is an empty 202 with
    `x-amzn-waf-action: challenge`: the wait asks the site, not the title (D-91)."""
    waf = {"ok": True, "status": 202, "url": "https://forum.example/", "body": "",
           "headers": {"x-amzn-waf-action": "challenge"}}
    answers = iter([waf, waf])
    fake.titles["https://forum.example"] = [""]
    fake.responder = lambda tab, req: next(answers, _body("<title>Forum home</title>"))
    assert _first_request(CHECK_SITE, fake=fake) is None
    assert 0 < sum(clock) < session_mod.CHALLENGE_WAIT


@pytest.mark.parametrize("answer", [
    _body(f"<p>the check</p><input {CHECK_MARK}>"),
    _body(f"<TITLE>{CHECK_TITLE}</TITLE><p>a picture to click</p>"),
    _body("<title>Forbidden</title>", status=403),
    {"ok": False, "kind": "network", "error": "Failed to fetch"},
], ids=["check text", "check title", "refused", "no answer"])
def test_t3_a_check_that_stays_waits_to_the_deadline_and_hands_over_its_tab(fake, clock, answer):
    fake.titles["https://forum.example"] = [CHECK_TITLE]
    fake.responder = lambda tab, req: answer
    check = _first_request(CHECK_SITE, fake=fake)
    assert sum(clock) > session_mod.CHALLENGE_WAIT
    assert check is not None and fake.named() == [check.tab]  # left open for the agent (D-54)
    assert check.session == "esum" and CHECK_NEEDED in str(check)
    assert f"patchright-cli -s=esum --tab={check.tab} mousemove <x> <y>" in check.help
    assert check.viewport == (1037, 739)  # mouse coordinates are CSS pixels; a screenshot may not be
    assert "the page is 1037x739 CSS pixels" in check.help


@pytest.mark.parametrize("reply", [None, {"width": 0, "height": 739}, {"width": "wide"}, {}],
                         ids=["no answer", "zero width", "not a number", "empty"])
def test_t3_a_check_whose_size_cant_be_read_is_still_handed_over(fake, clock, reply):
    fake.titles["https://forum.example"] = ["Just a moment..."]
    fake.viewport = reply
    check = _first_request(make_site(), fake=fake)
    assert check is not None and check.viewport is None and fake.named() == [check.tab]
    assert "size couldn't be read" in check.help and "divide x and y by 2" in check.help


def test_t3_a_reload_while_reading_the_size_still_hands_the_tab_over(fake, clock):
    fake.titles["https://forum.example"] = ["Just a moment..."]

    def reload(tab):
        raise BrowserError("patchright-cli eval: Error: Execution context was destroyed")

    fake.on_viewport = reload
    check = _first_request(make_site(), fake=fake)
    assert check is not None and check.viewport is None and fake.named() == [check.tab]


@pytest.mark.parametrize("how", ["closed", "navigated"])
def test_t3_a_tab_lost_while_reading_the_size_is_not_handed_over(fake, clock, how):
    """Found in review: TabLost is a BrowserError, and swallowing it named a tab that was gone."""
    fake.titles["https://forum.example"] = ["Just a moment..."]

    def lose(tab):
        if how == "closed":
            raise BrowserError("patchright-cli eval: Tab 'esum-x' is not open", code="tab_not_open")
        tab.url = "https://elsewhere.invalid/"

    fake.on_viewport = lose
    backend = in_page.make_backend(make_site(), Options(), session=fake.session(Options()))
    try:
        with pytest.raises(TabLost):
            backend.send(Request("GET", "https://forum.example/a"))
    finally:
        backend.close()
    assert fake.named() == []


def test_t3_a_zoomed_tab_is_not_clicked(fake, clock):
    """Found in review: at a 1.5 pinch zoom the width ratio is 1 and a click by it misses."""
    fake.titles["https://forum.example"] = ["Just a moment..."]
    fake.viewport = {"width": 800, "height": 600, "zoom": 1.5}
    check = _first_request(make_site(), fake=fake)
    assert check.zoom == 1.5 and fake.named() == [check.tab]
    assert "zoomed in (x1.50)" in check.help and "don't click" in check.help
    assert "mousemove" not in check.help and f"--tab={check.tab} close" in check.help


def test_t3_a_check_in_native_mode_is_login_needed_and_closes_the_tab(fake, clock, native_profile):
    (native_profile / "DevToolsActivePort").write_text("9222\n/devtools/browser/abc\n", encoding="ascii")
    fake.titles["https://forum.example"] = ["Just a moment..."]
    with pytest.raises(LoginNeeded, match="browser check"):
        _first_request(make_site(), Options(browser="native"), fake=fake)
    assert fake.named("esum-native") == []


def test_t3_asks_a_wary_site_every_few_seconds_not_every_second(fake, clock):
    fake.titles["https://forum.example"] = [CHECK_TITLE]
    fake.responder = lambda tab, req: _body(f"<input {CHECK_MARK}>")
    _first_request(CHECK_SITE, fake=fake)
    asks = fake.fns.count("fetch")  # the call's own request is never sent: the check stayed
    assert asks <= session_mod.CHALLENGE_WAIT / session_mod.CHALLENGE_ASK_EVERY + 2


def test_t3_a_site_without_check_text_sends_nothing_during_the_wait(fake, clock, capsys):
    fake.titles["https://forum.example"] = ["Just a moment..."]
    assert _first_request(make_site(), fake=fake) is not None
    assert fake.fns.count("fetch") == 0
    assert sum(clock) > session_mod.CHALLENGE_WAIT


def test_t3_a_reload_during_an_ask_is_waited_out(fake, clock):
    fake.titles["https://forum.example"] = [CHECK_TITLE]
    reloads = iter([BrowserError("patchright-cli eval: Error: Execution context was destroyed")])

    def responder(tab, req):
        if reload := next(reloads, None):
            raise reload
        return _body("<title>Forum home</title>")

    fake.responder = responder
    _first_request(CHECK_SITE, fake=fake)
    assert 0 < sum(clock) < session_mod.CHALLENGE_WAIT


def test_t3_fetch_failure_passes_through_raw(fake):
    fake.responder = lambda tab, req: {"ok": False, "error": "Failed to fetch"}
    backend = in_page.make_backend(make_site(), Options(), session=fake.session(Options()))
    try:
        with pytest.raises(BrowserError, match="in-page fetch of https://forum.example/a failed: Failed to fetch"):
            backend.send(Request("GET", "https://forum.example/a"))
    finally:
        backend.close()


def _closes_mid_fetch(fake, sent):
    """The request reaches the site, then the tab closes before the result comes back (once)."""
    def responder(tab, req):
        sent.append(req["method"])
        if len(sent) == 1:
            raise BrowserError("patchright-cli eval: Target page, context or browser has been closed",
                               code="target_closed")
        return page_response(tab, req)
    fake.responder = responder


def test_t3_never_resends_a_write_whose_tab_closed_mid_fetch(fake):
    sent = []
    _closes_mid_fetch(fake, sent)
    backend = in_page.make_backend(make_site(), Options(), session=fake.session(Options()))
    try:
        with pytest.raises(TabLost, match="may already have been applied. Check the site before retrying") as e:
            backend.send(Request("POST", "https://forum.example/bookmarks.json", {}, b"x"))
        assert "retry the call" not in str(e.value)
    finally:
        backend.close()
    assert sent == ["POST"]


def test_t3_resends_a_read_whose_tab_closed_mid_fetch(fake):
    sent = []
    _closes_mid_fetch(fake, sent)
    backend = in_page.make_backend(make_site(), Options(), session=fake.session(Options()))
    try:
        assert backend.send(Request("GET", "https://forum.example/a")).status == 200
    finally:
        backend.close()
    assert sent == ["GET", "GET"]


def test_t3_resends_a_write_whose_tab_was_gone_before_the_fetch(fake):
    sent = []
    fake.responder = lambda tab, req: (sent.append(req["method"]), page_response(tab, req))[1]
    backend = in_page.make_backend(make_site(), Options(), session=fake.session(Options()))
    try:
        backend.send(Request("GET", "https://forum.example/a"))
        del fake.tabs()[fake.named()[0]]  # closed between calls: the POST never ran in it
        assert backend.send(Request("POST", "https://forum.example/b", {}, b"x")).status == 200
    finally:
        backend.close()
    assert sent == ["GET", "POST"]


@pytest.mark.parametrize("kind", ["timeout", "network"])
def test_t3_transient_fetch_failures_are_retried_for_reads(fake, kind):
    sent = []

    def responder(tab, req):
        sent.append(req["method"])
        if len(sent) == 1:
            return {"ok": False, "kind": kind, "error": "Failed to fetch"}
        return page_response(tab, req)
    fake.responder = responder
    http = HttpClient(make_site(), Options(), dedupe=False, sleep=lambda s: None, session=fake.session())
    try:
        assert http.get_text("/a") == "<p>GET https://forum.example/a</p>"
    finally:
        http.close()
    assert sent == ["GET", "GET"]


def test_t3_transient_fetch_failures_give_up_and_never_resend_writes(fake):
    sent = []
    fake.responder = lambda tab, req: (sent.append(req["method"]),
                                       {"ok": False, "kind": "network", "error": "Failed to fetch"})[1]
    http = HttpClient(make_site(), Options(), dedupe=False, sleep=lambda s: None, session=fake.session())
    try:
        with pytest.raises(in_page.FetchTransientError):
            http.get("/a")
        assert sent == ["GET"] * 3
        with pytest.raises(in_page.FetchTransientError):
            http.request("POST", "/b", data={"x": "1"})
        assert sent[3:] == ["POST"]
    finally:
        http.close()


def test_t3_other_fetch_failures_are_not_transient(fake):
    fake.responder = lambda tab, req: {"ok": False, "kind": "other", "error": "TypeError: bad header"}
    backend = in_page.make_backend(make_site(), Options(), session=fake.session(Options()))
    try:
        with pytest.raises(BrowserError) as e:
            backend.send(Request("GET", "https://forum.example/a"))
        assert not isinstance(e.value, in_page.FetchTransientError)
    finally:
        backend.close()


def test_t3_throttle_runs_after_the_tab_is_ready(fake):
    seen = []
    backend = in_page.make_backend(make_site(), Options(), session=fake.session(Options()))
    try:
        backend.send(Request("GET", "https://forum.example/a"),
                     before_send=lambda: seen.append((len(fake.named()), "fetch" in fake.fns)))
    finally:
        backend.close()
    assert seen == [(1, False)]  # the tab was open, and nothing fetched yet, when the slot was taken
    assert "fetch" in fake.fns


def test_attach_mode_uses_its_own_session_and_never_touches_the_users_tabs(fake):
    backend = in_page.make_backend(make_site(), Options(browser="attach", cdp="http://localhost:9333"), session=fake.session(Options(browser="attach", cdp="http://localhost:9333")))
    try:
        backend.send(Request("GET", "https://forum.example/a"))
        assert backend.session.user_agent() == "Mozilla/5.0 FakeChrome/140"
    finally:
        backend.close()
    attach = next(c for c in fake.calls if c[0] == "attach")
    assert attach[2] == "esum-attach-localhost-9333"
    # host context: the attached Chrome's own cookies and logins, not a fresh empty context
    assert attach[3] == {"cdp": "http://localhost:9333", "context": "host"}
    # the default tab is the user's: no eval or close ever goes to it
    assert all(c[3].get("tab") for c in fake.calls if c[0] in ("eval", "close"))
    assert list(fake.tabs("esum-attach-localhost-9333")) == ["default"]
    assert BrowserSession("attach", None, client=fake).cdp == "http://127.0.0.1:9222"
    assert "localhost:9333" in backend.session.where


def test_existing_managed_session_is_promoted_in_place(fake):
    fake.sessions["esum"] = {"tabs": {"default": FakeTab("about:blank", "")}, "options": {}}
    backend = in_page.make_backend(make_site(), Options(), session=fake.session(Options()))
    try:
        backend.send(Request("GET", "https://forum.example/a"))
    finally:
        backend.close()
    assert [call[0] for call in fake.session_opens()] == ["background-open"]


def _worker(fake, host, errors, n=1):
    # each worker is like a separate esum process: its own session object, same named session
    backend = in_page.InPageBackend(make_site(base_url=f"https://{host}"),
                                    BrowserSession(client=fake, sleep=lambda s: None))
    try:
        for i in range(n):
            backend.send(Request("GET", f"https://{host}/p{i}"))
    except Exception as e:  # pragma: no cover - reported by the caller
        errors.append(e)
    finally:
        backend.close()


def test_parallel_calls_each_use_their_own_tab(fake):
    fake.delay = 0.002
    seen: list[tuple[str, str]] = []
    fake.responder = lambda tab, req: (seen.append((tab.origin, req["url"])), page_response(tab, req))[1]
    errors: list = []
    fake.sessions["esum"] = {"tabs": {"default": FakeTab("about:blank", "")}, "options": {}}
    threads = [threading.Thread(target=_worker, args=(fake, h, errors, 5))
               for h in ("a.example", "b.example", "c.example")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(seen) == 15
    assert all(url.startswith(origin + "/") for origin, url in seen)
    assert list(fake.tabs()) == ["default"]


def test_parallel_calls_fetch_concurrently(fake):
    """No ESUM lock is held while a page fetches (review limitation #4): three calls whose fetch
    takes 0.4 s each finish in about 0.4 s, not 1.2 s."""
    fake.eval_delay = 0.4
    fake.sessions["esum"] = {"tabs": {"default": FakeTab("about:blank", "")}, "options": {}}
    errors: list = []
    started = time.monotonic()
    threads = [threading.Thread(target=_worker, args=(fake, h, errors))
               for h in ("a.example", "b.example", "c.example")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - started
    assert errors == []
    # each call: one eval for location.href, one for the challenge check, one fetch = 3 evals
    assert elapsed < 3 * 0.4 * 2, f"calls were serialized ({elapsed:.2f}s)"


# --- login check -------------------------------------------------------------------------------------

def _login_site(check):
    return make_site(extras={"login_check": check})


@pytest.mark.parametrize("check,responder", [
    ({"status": [403]}, lambda tab, req: {**page_response(tab, req), "status": 403}),
    ({"url": [r"/login\b"]}, lambda tab, req: {**page_response(tab, req), "url": "https://forum.example/login?r=1"}),
    ({"body": [r"<p>GET .*/account"]}, page_response),
    (lambda resp: "custom says logged out" if "/account" in resp.url else None, page_response),
])
def test_t3_login_check_raises_login_needed(fake, check, responder):
    fake.responder = responder
    http = client_for(_login_site(check), fake=fake)
    try:
        with pytest.raises(LoginNeeded) as e:
            http.get("/account")
    finally:
        http.close()
    msg = str(e.value)
    assert "LOGIN NEEDED" in msg and "ex (https://forum.example)" in msg
    assert "the ESUM browser window" in msg and "session 'esum'" in msg


def test_login_check_passes_logged_in_pages(fake):
    http = client_for(_login_site({"status": [401], "url": ["/login"], "body": ["Please log in"]}), fake=fake)
    try:
        assert http.get("/home").status == 200
    finally:
        http.close()


def test_login_check_rejects_unknown_keys(fake):
    http = client_for(_login_site({"stauts": [401]}), fake=fake)
    try:
        with pytest.raises(ValueError, match="login_check"):
            http.get("/home")
    finally:
        http.close()


# --- T2 ----------------------------------------------------------------------------------------------
# Real curl_cffi against a local HTTP server: libcurl, not ESUM, picks which cookies each request
# carries, so only an end-to-end request shows the jar was loaded correctly. (Secure cookies can't be
# checked here: libcurl treats 127.0.0.1 as a secure origin and sends them over plain HTTP.)

COOKIES = [
    {"name": "sid", "value": "s1", "domain": "127.0.0.1", "path": "/", "expires": -1,
     "httpOnly": True, "secure": False, "sameSite": "Lax"},
    {"name": "old", "value": "o", "domain": "127.0.0.1", "path": "/", "expires": 1000,
     "httpOnly": False, "secure": False, "sameSite": "Lax"},
    {"name": "api", "value": "a", "domain": "127.0.0.1", "path": "/api", "expires": -1,
     "httpOnly": False, "secure": False, "sameSite": "Lax"},
    {"name": "other", "value": "x", "domain": "elsewhere.example", "path": "/", "expires": -1,
     "httpOnly": False, "secure": False, "sameSite": "Lax"},
]


@pytest.fixture
def local_server():
    """A plain-HTTP server on 127.0.0.1 that records each request's path and headers."""
    seen: list[tuple[str, dict[str, str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
            status = 401 if self.path.startswith("/session") else 200
            self.send_response(status)
            if self.path == "/first":
                self.send_header("Set-Cookie", "fresh=1; Path=/")
            self.send_header("X-Multi", "a")
            self.send_header("X-Multi", "b")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.seen = seen
    server.base = f"http://127.0.0.1:{server.server_address[1]}"
    yield server
    server.shutdown()
    server.server_close()


def _cookie_names(headers: dict[str, str]) -> set[str]:
    return {part.split("=", 1)[0].strip() for part in headers.get("cookie", "").split(";") if part.strip()}


def test_t2_sends_the_browser_cookie_jar_and_user_agent(fake, local_server):
    fake.cookies = COOKIES
    backend = browser_http.make_backend(make_site(Transport.BROWSER_HTTP), Options(), session=fake.session(Options()))
    try:
        first = backend.send(Request("GET", f"{local_server.base}/first", {"Accept": "application/json"}))
        backend.send(Request("GET", f"{local_server.base}/api/x"))
    finally:
        backend.close()
    (_, h0), (_, h1) = local_server.seen
    assert _cookie_names(h0) == {"sid"}                  # no expired, path-scoped or foreign
    assert _cookie_names(h1) == {"sid", "api", "fresh"}  # path match + the cookie the server set
    assert h0["user-agent"] == "Mozilla/5.0 FakeChrome/140"  # clearance cookies are bound to it
    assert fake.names().count("cookie-list") == 1        # the jar is read once per call
    assert first.status == 200 and first.json() == {"ok": True}
    assert first.headers["x-multi"] == "a, b"


def test_t2_throttle_runs_after_the_browser_cookies_are_read(fake, local_server):
    fake.cookies = COOKIES
    seen = []
    backend = browser_http.make_backend(make_site(Transport.BROWSER_HTTP), Options(), session=fake.session(Options()))
    try:
        backend.send(Request("GET", f"{local_server.base}/first"),
                     before_send=lambda: seen.append((fake.names().count("cookie-list"), len(local_server.seen))))
    finally:
        backend.close()
    assert seen == [(1, 0)] and len(local_server.seen) == 1  # jar read, nothing sent yet


def test_t2_without_browser_cookies_never_touches_the_browser(fake, local_server):
    site = make_site(Transport.BROWSER_HTTP, extras={"browser_cookies": False})
    backend = browser_http.make_backend(site, Options(), session=fake.session(Options()))
    try:
        backend.send(Request("GET", f"{local_server.base}/first"))
    finally:
        backend.close()
    (_, headers), = local_server.seen
    assert fake.calls == [] and "cookie" not in headers
    assert "Chrome/" in headers["user-agent"]  # curl_cffi's own UA, matching its fingerprint


def test_t2_login_check(fake, local_server):
    fake.cookies = COOKIES
    site = make_site(Transport.BROWSER_HTTP, extras={"login_check": {"status": [401]}})
    backend = browser_http.make_backend(site, Options(), session=fake.session(Options()))
    try:
        with pytest.raises(LoginNeeded, match="LOGIN NEEDED.*HTTP 401"):
            backend.send(Request("GET", f"{local_server.base}/session/current.json"))
    finally:
        backend.close()


def test_t2_network_failures_are_retried_like_httpx_ones():
    class Flaky:
        transient_errors = browser_http.BrowserHttpBackend.transient_errors

        def __init__(self):
            self.calls = 0

        def send(self, request, *, before_send=lambda: None):
            before_send()
            self.calls += 1
            if self.calls == 1:
                raise cffi_errors.Timeout("Operation timed out")
            return Response(200, request.url, {}, b"ok")

        def close(self):
            pass

    flaky = Flaky()
    http = HttpClient(make_site(Transport.BROWSER_HTTP), Options(), dedupe=False,
                      backend_factory=lambda s, o: flaky, sleep=lambda s: None)
    assert http.get_text("/x") == "ok" and flaky.calls == 2


@pytest.mark.parametrize("failure", [None, "planning", "write"])
def test_bookmarks_share_one_write_tab_until_runner_finishes(fake, isolated, failure):
    import base64
    import io

    from esum.core.runner import run_action
    from esum.engines.discourse import discourse
    from support.discourse import ForumFake, TOPIC

    forum = ForumFake("https://forum.example")
    writes = []
    write_tabs = []
    secret = "scrubbed-test-key"
    (isolated / "auth").mkdir()
    (isolated / "auth" / "ex.json").write_text(json.dumps({
        "key": secret, "client_id": "scrubbed", "scopes": ["read"]}), encoding="utf-8")

    def respond(tab, payload):
        assert secret not in json.dumps(payload)
        assert not any(k.lower() in {"user-api-key", "user-api-client-id"} for k in payload["headers"])
        if failure == "planning" and payload["url"].endswith("/session/current.json"):
            return {"ok": True, "status": 500, "url": payload["url"], "headers": {},
                    "body": base64.b64encode(b"planning failed").decode()}
        if payload["method"] == "POST":
            writes.append(payload)
            write_tabs.append(tuple(fake.named()))
            assert len(fake.named()) == 1
            assert not any(c[0] == "close" for c in fake.calls)
            if failure == "write":
                return {"ok": True, "status": 500, "url": payload["url"], "headers": {},
                        "body": base64.b64encode(b"write failed").decode()}
        response = forum.send(Request(payload["method"], payload["url"], payload["headers"],
                                      base64.b64decode(payload["body"]) if payload["body"] else None))
        return {"ok": True, "status": response.status, "url": response.url, "headers": response.headers,
                "body": base64.b64encode(response.content).decode()}

    fake.responder = respond
    site = make_site(Transport.DIRECT, extras={"write_transport": "in_page"})
    stream = io.StringIO()
    def run():
        run_action(site, discourse.actions["bookmark"], {"topic": TOPIC, "posts": [3, 5, 7]},
                   Options(), stream=stream, session=fake.session())
    if failure:
        from esum import HttpError
        from esum.core.errors import WriteInterrupted
        with pytest.raises(WriteInterrupted if failure == "write" else HttpError, match=f"{failure} failed"):
            run()
        assert len(writes) == (1 if failure == "write" else 0)
    else:
        run()
        assert len(writes) == 3
        assert len(set(write_tabs)) == 1
        assert "done 3." in stream.getvalue()
    opened = [c for c in fake.calls if c[0] in ("open", "background-open") and "tab" in c[3]]
    closed = [c for c in fake.calls if c[0] == "close"]
    assert len(opened) == len(closed) == 1
    assert fake.named() == []


def test_context_in_page_client_strips_even_explicit_and_site_key_headers(fake, action_runner):
    import io
    from esum import Tier, Write, action, site
    from esum.transport import load_backend

    secret = "scrubbed-key-must-not-enter-page"
    target = site(name="safe", base_url="https://forum.example", label="Safe",
                  headers={"uSeR-aPi-kEy": secret, "User-Api-Client-Id": "scrubbed"})
    seen = []
    def respond(tab, payload):
        assert secret not in json.dumps(payload)
        assert payload["headers"] == {"X-Test": "ok"}
        seen.append(payload)
        return page_response(tab, payload)
    fake.responder = respond

    @action(target, tier=Tier.REVERSIBLE)
    def write(ctx):
        """Exercise managed clients."""
        http = ctx.http_for(Transport.IN_PAGE)
        assert ctx.http_for(Transport.IN_PAGE) is http
        return [Write("test", lambda: http.request("POST", "/safe", headers={
            "USER-API-KEY": secret, "user-api-client-id": "scrubbed", "X-Test": "ok"}).status)]

    session = fake.session()
    action_runner(target, target.actions["write"], {}, Options(), stream=io.StringIO(), session=session,
                  backend_factory=lambda st, opts: load_backend(st, opts, session=session))
    assert len(seen) == 1 and fake.named() == []



def test_context_in_page_client_drops_explicit_credential_headers(fake, action_runner):
    import io
    from esum import Tier, Write, action, site
    from esum.transport import Credential

    secret = "scrubbed-key-must-not-enter-page"
    target = site(name="safe", base_url="https://forum.example", label="Safe")
    seen = []
    def respond(tab, payload):
        assert secret not in json.dumps(payload)
        assert payload["headers"] == {"X-Test": "ok"}
        seen.append(payload)
        return page_response(tab, payload)
    fake.responder = respond

    @action(target, tier=Tier.REVERSIBLE)
    def write(ctx):
        """Exercise managed clients."""
        http = ctx.http_for(Transport.IN_PAGE)
        assert ctx.http_for(Transport.IN_PAGE) is http
        return [Write("test", lambda: http.request("POST", "/safe", headers={"X-Test": "ok"},
            credential=Credential({"USER-API-KEY": secret, "user-api-client-id": "scrubbed"})).status)]

    from esum.transport import load_backend
    action_runner(target, target.actions["write"], {}, Options(), stream=io.StringIO(), session=fake.session(),
                  backend_factory=lambda st, opts: load_backend(st, opts, session=fake.session()))
    assert len(seen) == 1 and fake.named() == []


@pytest.mark.parametrize("source", ["context", "client", "discourse", "discourse-key", "backend"])
def test_escalated_login_names_the_active_clients_session(fake, isolated, source, action_runner):
    from esum import Tier, action
    from esum.engines.discourse.account import account_for
    from esum.transport import load_backend
    from conftest import FakeBackend

    target = make_site(Transport.BROWSER_HTTP, fallback=Transport.IN_PAGE,
                       extras={"browser_cookies": False,
                               **({"login_check": {"status": [401]}} if source == "backend" else {})})
    if source == "discourse-key":
        (isolated / "auth").mkdir()
        (isolated / "auth" / "ex.json").write_text(json.dumps({
            "key": "scrubbed-key", "client_id": "scrubbed", "scopes": ["read"]}), encoding="utf-8")
    url = target.base_url + "/private.json"
    first = FakeBackend({url: Response(403, url, {"cf-mitigated": "challenge"}, b"challenge")})
    options = Options(browser="attach", cdp="http://localhost:9333")
    session = fake.session(options)
    fake.responder = lambda tab, req: {
        "ok": True, "status": 401 if source in ("backend", "discourse", "discourse-key") else 200,
        "url": req["url"], "headers": {},
        "body": base64.b64encode(b'{"error_type":"not_logged_in"}').decode(),
    }

    def factory(site, options):
        if site.transport is Transport.BROWSER_HTTP:
            return first
        return load_backend(site, options, session=session)

    @action(target, tier=Tier.READ)
    def private(ctx):
        """Read a member page."""
        assert ctx.site.transport is Transport.BROWSER_HTTP
        assert ctx.http_for(Transport.IN_PAGE).browser_session is session
        if source.startswith("discourse"):
            return account_for(ctx).fetch_json("/private.json")
        ctx.http.get("/private.json")
        raise (ctx.login_needed() if source == "context" else ctx.http.login_needed())

    with pytest.raises(LoginNeeded) as caught:
        action_runner(target, target.actions["private"], {}, options, session=session, backend_factory=factory)
    assert caught.value.where == session.where
    assert "the Chrome attached at http://localhost:9333" in str(caught.value)
    assert "their own terminal" not in str(caught.value)
    assert first.closed == 1
    assert fake.named(session.name) == []


def test_login_error_owns_the_fixed_message_help_and_exit_code():
    error = LoginNeeded("ex", "https://forum.example", where="your everyday Chrome (native mode)")
    assert str(error) == ("esum: LOGIN NEEDED: ex (https://forum.example). The user logs in themselves in "
                          "your everyday Chrome (native mode). Never type or handle credentials.")
    assert error.exit_code == 3
    assert error.help == ("Don't stop a longer task for this: finish the rest, then tell the user at "
                          "the end what needed a login. Run this command again once they have logged in")


@pytest.mark.parametrize("mode", ["managed", "attach", "native"])
def test_cookieless_login_check_names_the_selected_session_without_opening_it(fake, local_server, mode):
    options = Options(browser=mode)
    session = fake.session(options)
    target = make_site(Transport.BROWSER_HTTP,
                       extras={"browser_cookies": False, "login_check": {"status": [401]}})
    backend = browser_http.make_backend(target, options, session=session)
    try:
        with pytest.raises(LoginNeeded) as caught:
            backend.send(Request("GET", local_server.base + "/session/current.json"))
        assert caught.value.where == session.where
    finally:
        backend.close()
    assert fake.calls == []


def test_bare_login_error_has_a_mode_neutral_destination():
    error = LoginNeeded("ex")
    assert error.where == "the browser ESUM uses"
    assert str(error) == ("esum: LOGIN NEEDED: ex. The user logs in themselves in "
                          "the browser ESUM uses. Never type or handle credentials.")


@pytest.mark.parametrize("call", ["whoami", "fetch_json"])
@pytest.mark.parametrize("escalates", [True, False])
def test_key_user_check_uses_the_login_actually_sent(fake, isolated, call, escalates, action_runner):
    from esum import Tier, action
    from esum.engines.discourse import discourse
    from esum.engines.discourse.account import account_for
    from esum.transport import load_backend
    from conftest import FakeBackend, json_response

    (isolated / "auth").mkdir()
    (isolated / "auth" / "ex.json").write_text(json.dumps({
        "key": "scrubbed-key", "client_id": "scrubbed", "scopes": ["read"]}), encoding="utf-8")
    target = make_site(Transport.BROWSER_HTTP, fallback=Transport.IN_PAGE,
                       extras={"browser_cookies": False})
    current = target.base_url + "/session/current.json"
    private = target.base_url + "/private.json"
    first = FakeBackend({
        private: json_response(private, {"error_type": "not_logged_in"}, status=401),
        current: (Response(403, current, {"cf-mitigated": "challenge"}, b"challenge")
                  if escalates else json_response(current, {}, status=404)),
    })
    options = Options(browser="attach", cdp="http://localhost:9333")
    session = fake.session(options)
    sent = []

    def respond(tab, request):
        sent.append(request)
        assert not any(k.lower() == "user-api-key" for k in request["headers"])
        return {"ok": True, "status": 404, "url": request["url"], "headers": {},
                "body": base64.b64encode(b'{}').decode()}

    fake.responder = respond

    def factory(site, options):
        return first if site.transport is Transport.BROWSER_HTTP else load_backend(site, options, session=session)

    @action(target, tier=Tier.READ)
    def read(ctx):
        """Check the key's user."""
        return account_for(ctx).fetch_json("/private.json")

    act = discourse.actions["whoami"] if call == "whoami" else target.actions["read"]
    with pytest.raises(LoginNeeded) as caught:
        action_runner(target, act, {}, options, session=session, backend_factory=factory)
    assert first.requests[-1].url == current
    assert first.requests[-1].headers["User-Api-Key"] == "scrubbed-key"
    if escalates:
        assert caught.value.where == session.where
        assert "HTTP" in caught.value.detail
        assert "saved key no longer works" not in str(caught.value)
        assert len(sent) == 1 and sent[0]["url"] == current
    else:
        assert caught.value.where == "their own terminal, by running `esum ex login`"
        assert caught.value.detail == "the saved key no longer works"
        assert sent == []
