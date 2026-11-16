"""The browser session recovers from what the user closes, and cleans up the tabs it opened.

These tests drive `BrowserSession` against `FakeDaemon` (test_browser_transports) extended with
what patchright-cli 0.11.1 does when the user closes things: a named tab the user closed fails
`eval` with code `target_closed`, and so does every command of a
session whose window or attached Chrome was closed; a named tab whose navigation failed stays open
on an error page.
"""

from __future__ import annotations

import httpx
import pytest

from esum import Transport
from esum.browser import daemon as daemon_mod
from esum.browser import session as session_mod
from esum.browser.daemon import BrowserError
from esum.browser.session import BrowserSession, TabLost
from esum.core.context import Options
from esum.transport import Request, browser_http, in_page

from test_browser_transports import FakeDaemon, FakeTab, make_site

CLOSED_MSG = "Error: Target page, context or browser has been closed"


class ClosingDaemon(FakeDaemon):
    """FakeDaemon plus patchright-cli's behaviour for closed tabs, closed browsers and failed
    navigations of a named tab's `open`."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.dead: set[str] = set()                  # sessions whose browser was closed
        self.zombies: set[tuple[str, str]] = set()   # (session, named tab) the user closed
        self.bad_urls: set[str] = set()              # a named tab's open fails to navigate

    def close_tab_by_user(self, tab: str, session: str = "esum") -> None:
        self.zombies.add((session, tab))

    def _run(self, command, args, name, options):
        fail = BrowserError(f"patchright-cli {command} (session {name!r}): {CLOSED_MSG}",
                            code="target_closed")
        tab = options.get("tab")
        if command in ("close", "detach") and tab is None:
            self.sessions.pop(name, None)
            self.dead.discard(name)
            return f"Session '{name}' {command}ed."
        s = self.sessions.get(name)
        if s is not None and name in self.dead and (command in ("cookie-list", "eval")
                                                     or (command in ("open", "background-open", "close") and tab)):
            raise fail
        if s is not None and command == "eval" and (name, tab) in self.zombies:
            raise fail
        if s is not None and command in ("open", "background-open") and tab and args[0] in self.bad_urls:
            s["tabs"][tab] = FakeTab("chrome-error://chromewebdata/", "")
            raise BrowserError(f"patchright-cli open (session {name!r}): Error: Page.goto: "
                               f"net::ERR_NAME_NOT_RESOLVED at {args[0]}")
        return super()._run(command, args, name, options)


@pytest.fixture
def fake():
    return ClosingDaemon()


def session(fake, mode="managed", cdp=None) -> BrowserSession:
    return fake.session(Options(browser=mode, cdp=cdp))


# --- 3. a closed ESUM window / attached Chrome is reopened once (review-r2 #2) ---------------------

def test_closed_browser_is_closed_reopened_and_the_tab_retried(fake):
    backend = in_page.make_backend(make_site(), Options(), session=fake.session(Options()))
    try:
        backend.send(Request("GET", "https://forum.example/a"))
    finally:
        backend.close()
    fake.dead.add("esum")  # the user closed the ESUM window
    backend = in_page.make_backend(make_site(), Options(), session=fake.session(Options()))
    try:
        resp = backend.send(Request("GET", "https://forum.example/b"))
    finally:
        backend.close()
    assert resp.status == 200
    session_closes = [c for c in fake.calls if c[0] == "close" and "tab" not in c[3]]
    assert len(session_closes) == 1 and len(fake.session_opens()) == 2


def test_closed_attached_chrome_is_reattached_for_cookies(fake):
    s = session(fake, "attach", "http://127.0.0.1:9222")
    assert s.cookies() == []
    fake.dead.add(s.name)
    assert s.cookies() == []
    # an attached Chrome is the user's: the stale session is detached, never closed
    assert fake.names().count("attach") == 2 and fake.names().count("detach") == 1
    assert "close" not in fake.names()


# --- native mode: the user's everyday Chrome via consented remote debugging -------------------------

def test_native_attaches_to_the_address_chrome_wrote(fake, native_profile):
    (native_profile / "DevToolsActivePort").write_text("9222\n/devtools/browser/abc\n", encoding="ascii")
    s = session(fake, "native")
    assert s.cookies() == []
    attach = next(c for c in fake.calls if c[0] == "attach")
    assert attach[2] == "esum-native"
    assert attach[3] == {"cdp": "ws://127.0.0.1:9222/devtools/browser/abc", "context": "host"}
    assert s.where == "your everyday Chrome (native mode)"
    # Chrome restarted with a new port and id: the stale session is detached and re-attached there
    (native_profile / "DevToolsActivePort").write_text("9333\n/devtools/browser/def\n", encoding="ascii")
    fake.dead.add("esum-native")
    s.cookies()
    attaches = [c[3]["cdp"] for c in fake.calls if c[0] == "attach"]
    assert attaches[-1] == "ws://127.0.0.1:9333/devtools/browser/def"
    assert "detach" in fake.names() and "close" not in fake.names()


def test_native_without_remote_debugging_says_how_to_turn_it_on(fake, native_profile):
    with pytest.raises(BrowserError, match="chrome://inspect/#remote-debugging"):
        session(fake, "native").cookies()


def test_native_rejects_cdp_and_is_never_the_default():
    from esum.cli import UsageError, extract_globals
    assert extract_globals(["demo", "thread"])[0].browser == "managed"
    assert extract_globals(["--browser=native", "demo", "thread"])[0].browser == "native"
    with pytest.raises(UsageError, match="doesn't apply to --browser=native"):
        extract_globals(["--browser=native", "--cdp=http://127.0.0.1:9222", "demo", "thread"])


def test_other_session_errors_are_not_retried(fake):
    s = session(fake)
    s.cookies()
    fake.bad_urls.add("https://down.example/")
    with pytest.raises(BrowserError, match="ERR_NAME_NOT_RESOLVED"):
        s.open_tab("https://down.example/")
    assert not [c for c in fake.calls if c[0] == "close" and "tab" not in c[3]]
    assert len([c for c in fake.calls if c[0] in ("open", "background-open") and "tab" in c[3]]) == 1


# --- 4. a failed tab open leaves no tab behind (review-r2 #3) --------------------------------------

def test_failed_navigation_closes_the_new_tab(fake):
    s = session(fake)
    fake.bad_urls.add("https://down.example/")
    for _ in range(3):
        with pytest.raises(BrowserError):
            s.open_tab("https://down.example/")
    assert fake.named() == []  # patchright-cli leaves the tab on an error page; ESUM closes it


def test_failed_challenge_wait_closes_the_new_tab(fake):
    s = session(fake)
    checked = []

    def lost(tab):
        checked.append(tab)
        raise BrowserError("tab disappeared during the wait", code="tab_not_open")

    fake.on_state = lost
    with pytest.raises(TabLost):
        s.open_tab("https://forum.example")
    assert checked and fake.named() == []


# --- 5. a tab the user closed is replaced, never searched for (review-r2 #4) ------------------------

def test_our_tab_closed_by_user_raises_tab_lost(fake):
    s = session(fake)
    tab = s.open_tab("https://forum.example")
    fake.close_tab_by_user(tab.name)
    with pytest.raises(TabLost):
        tab.evaluate(session_mod._PAGE_STATE)


def test_t3_retries_in_a_fresh_tab_when_the_user_closed_ours(fake):
    backend = in_page.make_backend(make_site(), Options(), session=fake.session(Options()))
    try:
        backend.send(Request("GET", "https://forum.example/a"))
        fake.close_tab_by_user(fake.named()[0])
        assert backend.send(Request("GET", "https://forum.example/b")).status == 200
    finally:
        backend.close()
    assert len([c for c in fake.calls if c[0] in ("open", "background-open") and "tab" in c[3]]) == 2


def test_other_eval_errors_in_our_tab_are_not_retried_elsewhere(fake):
    s = session(fake)
    tab = s.open_tab("https://forum.example")

    def fail(state):
        raise BrowserError("patchright-cli eval (session 'esum'): Error: Execution context was destroyed")

    fake.before_eval = fail
    with pytest.raises(BrowserError, match="Execution context was destroyed") as info:
        tab.evaluate(session_mod._PAGE_STATE)
    assert not isinstance(info.value, TabLost)


def test_t2_user_agent_is_read_in_a_tab_of_esums_own(fake):
    s = session(fake, "attach", "http://127.0.0.1:9222")
    assert s.user_agent() == "Mozilla/5.0 FakeChrome/140"
    ua = [c for c in fake.calls if c[0] == "eval"]
    assert ua and all(c[3].get("tab", "").startswith("esum-") for c in ua)  # not the user's tab
    assert fake.named("esum-attach-127-0-0-1-9222") == []


def test_t2_backend_builds_its_client_from_the_session(fake):
    s = session(fake)
    built = []

    class FakeCurl:  # a curl_cffi Session stand-in: building the client is what this tests
        def __init__(self):
            self.cookies = httpx.Cookies()
            self.headers = {}
            built.append(self)

        def close(self):
            pass

    backend = browser_http.BrowserHttpBackend(make_site(Transport.BROWSER_HTTP), s,
                                              session_factory=FakeCurl)
    backend._client()
    backend.close()
    assert built[0].headers["User-Agent"] == "Mozilla/5.0 FakeChrome/140"


# --- 7. the missing-Chrome hint names a command that exists (review-r4 #3) --------------------------

def test_missing_patchright_cli_message_gives_a_working_chrome_install(tmp_path):
    class OfflineDaemon(daemon_mod.DaemonClient):
        def _listening(self):
            return False

    client = OfflineDaemon(port=1, executable=str(tmp_path / "missing-patchright-cli"))
    with pytest.raises(BrowserError) as info:
        client.ensure_running()
    # `patchright` is the patchright package's script; `uv tool install patchright-cli` links only
    # `patchright-cli`, so the hint has to run it from the tool's environment.
    assert "uv tool run --from patchright-cli patchright install chrome" in str(info.value)
