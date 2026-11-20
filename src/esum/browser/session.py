"""Named patchright-cli sessions and the per-call tabs ESUM opens in them.

Each ESUM call works in its own *named tab* (`patchright-cli --tab=<name>`, patchright-cli 0.8+):
its own page in the session's shared browser, cookies and fingerprint. Every command names the tab
it acts on, so parallel `esum` calls never run code in each other's tabs and their requests
overlap. The only cross-process lock left guards opening (or attaching) the session itself.

In-page code runs through a small guard that checks the tab is still on the origin ESUM opened it
on; a tab the user closed or navigated elsewhere raises `TabLost`. Only a loss detected before
the function ran proves that retrying a write is safe.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Callable, NamedTuple
from urllib.parse import urlsplit

from filelock import FileLock

from ..core.errors import CheckNeeded, LoginNeeded
from ..core.paths import esum_home
from .daemon import DEFAULT_PORT, SESSION_NOT_OPEN, TAB_NOT_OPEN, TARGET_CLOSED, BrowserError, DaemonClient

MANAGED_SESSION = "esum"
NATIVE_SESSION = "esum-native"
DEFAULT_CDP = "http://127.0.0.1:9222"
LOCK_TIMEOUT = 300.0     # a stuck holder fails the waiting call instead of hanging it forever
CHALLENGE_WAIT = 20.0    # seconds to let the browser clear an anti-bot interstitial on tab open
CHALLENGE_ASK_EVERY = 3.0  # seconds between `cleared` asks: each is one more request to a wary site
SNAPSHOT_MAX_AGE = 3600  # patchright-cli writes a snapshot file per page it opens

# Interstitial titles (Cloudflare and similar). Generic: no site's logic, only the vendor pages.
CHALLENGE_TITLE = re.compile(r"just a moment|attention required|checking your browser|"
                             r"verify you are human|prove your humanity|请稍候", re.I)

_PAGE_STATE = "async () => ({title: document.title, href: location.href})"
# The page's size in CSS pixels, which mouse commands use, and its visual (pinch) zoom. A
# screenshot can be larger (device pixels: patchright 1.58 ignores scale="css" on a HiDPI screen),
# so the agent scales by the width ratio; a zoomed page has no such simple mapping (D-54).
_VIEWPORT = ("async () => ({width: innerWidth, height: innerHeight, "
             "zoom: window.visualViewport ? visualViewport.scale : 1})")

# An empty cookie jar is a success reply, so it has no code: "0 cookies in this session".
NO_COOKIES = "0 cookies"
# what T2 copies so its requests read as the browser's own (clearance cookies are bound to the UA)
IDENTITY_JS = "({userAgent: navigator.userAgent, languages: navigator.languages})"


class BrowserIdentity(NamedTuple):
    user_agent: str
    languages: tuple[str, ...]


class TabLost(BrowserError):
    """ESUM's tab was closed, or navigated to another origin, by someone else."""

    def __init__(self, message: str, *, not_run: bool = False):
        super().__init__(message)
        self.not_run = not_run


def is_closed(error: BrowserError) -> bool:
    """The browser, context or page behind the session or tab was closed."""
    return error.code == TARGET_CLOSED


def is_not_open(error: BrowserError) -> bool:
    """The session isn't open, or the named tab is gone."""
    return error.code in (SESSION_NOT_OPEN, TAB_NOT_OPEN)


def is_tab_gone(error: BrowserError) -> bool:
    return error.code == TAB_NOT_OPEN


def cookie_jar(output: str) -> list[dict[str, Any]]:
    """`cookie-list` output as cookie dicts: JSON, or the empty-jar message."""
    return [] if output.startswith(NO_COOKIES) else json.loads(output)


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


class BrowserSession:
    """One patchright-cli named session: the managed profile, an attached Chrome, or the user's own
    everyday Chrome (native: attached through Chrome's consented remote debugging).

    Nothing touches the browser until a method needs it, and the first such call opens (or
    attaches) the session if the daemon doesn't have it yet. ESUM never runs page code in a tab it
    didn't open: in attach and native mode the session's default tab is one of the user's.
    """

    def __init__(self, mode: str = "managed", cdp: str | None = None, *,
                 client: DaemonClient | None = None, port: int = DEFAULT_PORT,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        if mode not in ("managed", "attach", "native"):
            raise ValueError(f"unknown browser mode {mode!r}; use managed, attach or native")
        self.mode = mode
        self.cdp = (cdp or DEFAULT_CDP) if mode == "attach" else None  # native: read when attaching
        self.name = session_name(mode, cdp)
        self.dir = esum_home() / "browser"
        self.client = client or DaemonClient(port, cwd=str(self.dir))
        self._lock = FileLock(str(self.dir / f"{self.name}.lock"), timeout=LOCK_TIMEOUT)
        self._sleep = sleep
        self._clock = clock
        self._open = False

    @property
    def where(self) -> str:
        """The window the user logs in to, for the LOGIN NEEDED message."""
        return login_place(self.mode, self.cdp)

    # --- daemon access ---------------------------------------------------------------------------

    def run(self, command: str, *args: str, **options: Any) -> str:
        if self.mode == "managed" and command == "open":
            profile = self.dir / "profile"
            profile.mkdir(parents=True, exist_ok=True)
            command = "background-open"
            options.update(persistent=True, profile=str(profile), background=True)
        return self.client.command(command, args, session=self.name, **options)

    def lock(self) -> FileLock:
        self.dir.mkdir(parents=True, exist_ok=True)
        return self._lock

    def ensure_open(self) -> None:
        """Open or attach the session once; the lock keeps parallel calls from doing it twice."""
        if self._open:
            return
        with self.lock():
            if self.mode == "managed":
                self.run("open")
            else:
                try:
                    self.run("tab-list")
                except BrowserError as e:
                    if not is_not_open(e):
                        raise
                    # host context: the attached Chrome's own cookies and logins
                    cdp = native_cdp() if self.mode == "native" else self.cdp
                    self.run("attach", cdp=cdp, context="host")
            self._prune_snapshots()
        self._open = True

    def reopen_if_closed(self, error: BrowserError) -> None:
        """Re-raise `error` unless the browser behind the session was closed (the user closed the
        ESUM window or quit the attached Chrome). Then drop the stale session and open or attach
        it again, so the caller can retry its step once."""
        if not is_closed(error):
            raise error
        with self.lock():
            try:
                self.run("cookie-list")  # another esum call may have reopened it meanwhile
            except BrowserError as e:
                if not (is_closed(e) or is_not_open(e)):
                    raise
                try:
                    # An attached Chrome belongs to the user: detach, never close it.
                    self.run("close" if self.mode == "managed" else "detach")
                except BrowserError:
                    pass
            self._open = False
            self.ensure_open()

    def _retrying(self, step: Callable[[], Any]) -> Any:
        self.ensure_open()
        try:
            return step()
        except TabLost:
            raise
        except BrowserError as e:
            self.reopen_if_closed(e)
            return step()

    def cookies(self) -> list[dict[str, Any]]:
        """The session's cookie jar (Playwright cookie dicts). Kept in memory, never written."""
        return self._retrying(lambda: cookie_jar(self.run("cookie-list")))

    def identity(self) -> BrowserIdentity:
        """The browser's user agent and languages, read in a short-lived tab of ESUM's own."""
        def step() -> BrowserIdentity:
            name = _tab_name()
            try:
                self.run("open", "about:blank", tab=name)
                value = json.loads(self.run("eval", IDENTITY_JS, tab=name))
                return BrowserIdentity(str(value["userAgent"]), tuple(str(x) for x in value["languages"]))
            finally:
                _close_quietly(self, name)
        return self._retrying(step)

    # --- tabs ------------------------------------------------------------------------------------

    def open_tab(self, url: str, cleared: Callable[["Tab"], bool] | None = None, *,
                 site: str = "") -> "Tab":
        """Open a named tab on `url` and wait out an anti-bot interstitial; see `wait_for_challenge`
        for `cleared`. A check that outlasts the wait raises `CheckNeeded` and leaves the tab open
        for the host agent (D-54); in native mode, where the tab is in the user's own Chrome, it is
        LOGIN NEEDED instead."""
        tab = self._retrying(lambda: self._new_tab(url))
        try:
            tab.wait_for_challenge(cleared)
        except BaseException:
            tab.close()
            raise
        if tab.challenged:
            if self.mode == "native":
                tab.close()
                raise LoginNeeded(site or tab.origin, tab.origin, where=self.where,
                                  detail="a browser check wants a person")
            try:
                size = tab.viewport()
            except TabLost:  # gone or elsewhere since the wait: there is no tab to hand over
                tab.close()
                raise
            raise CheckNeeded(site or tab.origin, url, self.name, tab.name,
                              viewport=size[:2] if size else None, zoom=size[2] if size else None)
        return tab

    def _new_tab(self, url: str) -> "Tab":
        name = _tab_name()
        try:
            self.run("open", url, tab=name)
            href = json.loads(self.run("eval", "location.href", tab=name))
        except BaseException:
            _close_quietly(self, name)  # a failed navigation leaves the tab on an error page
            raise
        return Tab(self, name, origin_of(href or url))

    def _prune_snapshots(self) -> None:
        cutoff = time.time() - SNAPSHOT_MAX_AGE
        for path in (self.dir / ".patchright-cli").glob("page-*.yml"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass


class Tab:
    """A named tab ESUM opened for one call. Code runs in it only through `evaluate`."""

    def __init__(self, session: BrowserSession, name: str, origin: str):
        self.session = session
        self.name = name
        self.origin = origin
        self.closed = False
        self.challenged = False  # the check still showed when the wait ran out

    def evaluate(self, fn: str, arg: Any = None) -> Any:
        """Run the JS function source `fn` with `arg` in this tab and return its JSON result."""
        try:
            reply = self.session.client.evaluate(fn, arg, origin=self.origin,
                                                 session=self.session.name, tab=self.name)
        except BrowserError as e:
            if not (is_closed(e) or is_tab_gone(e)):
                raise
            raise TabLost(f"ESUM's tab for {self.origin} was closed or left the site; retry the call",
                          not_run=is_tab_gone(e)) from e
        if not reply.get("esum_tab"):
            raise TabLost(f"ESUM's tab for {self.origin} was closed or left the site; retry the call",
                          not_run=True)
        return reply.get("value")

    def wait_for_challenge(self, cleared: Callable[["Tab"], bool] | None = None) -> None:
        """Wait until the title stops naming a check. A check page's title can outlive the check
        (Reddit's stays while requests from the tab already get real pages), so `cleared`, when
        given, is asked too: it says whether a request from this tab gets the site's answer. Some
        checks' pages have no telling title (AWS WAF's), so with `cleared` a plain title is not
        enough: the tab is clear when `cleared` says so."""
        deadline = self.session._clock() + CHALLENGE_WAIT
        ask_at = 0.0
        warned = False
        while True:
            try:
                state = self.evaluate(_PAGE_STATE) or {}
            except TabLost:
                raise
            except BrowserError:  # the interstitial reloaded the page mid-evaluation
                state = {"title": "Just a moment"}
            if not cleared and not CHALLENGE_TITLE.search(state.get("title") or ""):
                return
            if cleared and self.session._clock() >= ask_at:
                ask_at = self.session._clock() + CHALLENGE_ASK_EVERY
                try:
                    if cleared(self):
                        return
                except TabLost:
                    raise
                except BrowserError:  # the interstitial reloaded the page mid-evaluation
                    pass
            if self.session._clock() > deadline:
                self.challenged = True
                return
            if not warned:
                print(f"esum: waiting for {self.origin}'s anti-bot check to clear", file=sys.stderr)
                warned = True
            self.session._sleep(1.0)

    def viewport(self) -> tuple[int, int, float] | None:
        """The page's width and height in CSS pixels and its visual zoom, or None if the tab can't
        say (a reload mid-read, a nonsensical answer). A lost tab raises `TabLost`."""
        try:
            size = self.evaluate(_VIEWPORT) or {}
        except TabLost:
            raise
        except BrowserError:
            return None
        try:
            width, height, zoom = int(size["width"]), int(size["height"]), float(size.get("zoom", 1))
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        return (width, height, zoom) if width > 0 and height > 0 and zoom > 0 else None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            _close_quietly(self.session, self.name)


def _tab_name() -> str:
    return f"esum-{secrets.token_hex(4)}"


def _close_quietly(session: BrowserSession, name: str) -> None:
    """Close one named tab. Never a plain `close`, which would close the session's default tab:
    one of the user's own in attach and native mode."""
    try:
        session.run("close", tab=name)
    except BrowserError:
        pass  # already gone, or the browser or daemon is gone


def login_place(mode: str = "managed", cdp: str | None = None) -> str:
    """The browser window named by login and challenge outcomes, without opening it."""
    if mode == "managed":
        return f"the ESUM browser window (managed Chrome profile, patchright-cli session '{session_name(mode, cdp)}')"
    if mode == "native":
        return "your everyday Chrome (native mode)"
    return f"the Chrome attached at {cdp or DEFAULT_CDP} (patchright-cli session '{session_name(mode, cdp)}')"


def session_name(mode: str, cdp: str | None) -> str:
    """The patchright-cli session a browser mode uses: `esum`, `esum-native`, or one per attached
    CDP endpoint."""
    if mode == "native":
        return NATIVE_SESSION
    return MANAGED_SESSION if mode == "managed" else _attach_name(cdp or DEFAULT_CDP)


def chrome_user_data_dir() -> Path:
    """Where the user's everyday Google Chrome keeps its profiles."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Google" / "Chrome" / "User Data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "google-chrome"


def native_cdp() -> str:
    """The WebSocket address of the user's running Chrome, from the `DevToolsActivePort` file Chrome
    writes when the user turns on remote debugging at chrome://inspect/#remote-debugging (Chrome
    144+). Chrome serves no HTTP discovery endpoint in that mode, so only this address works. Read
    at every attach: the port and id change when Chrome restarts."""
    port_file = chrome_user_data_dir() / "DevToolsActivePort"
    try:
        port, path = port_file.read_text(encoding="ascii").split()[:2]
    except (OSError, ValueError):
        raise BrowserError("native mode attaches to your everyday Chrome, which needs remote "
                           "debugging turned on: open chrome://inspect/#remote-debugging in Chrome, "
                           "turn it on, then retry (Chrome will ask you to Allow ESUM's connection)") from None
    return f"ws://127.0.0.1:{port}{path}"


def _attach_name(cdp: str) -> str:
    parts = urlsplit(cdp if "://" in cdp else f"http://{cdp}")
    return "esum-attach-" + re.sub(r"[^a-z0-9]+", "-", (parts.netloc or cdp).lower()).strip("-")


_sessions: dict[tuple[str, str | None], BrowserSession] = {}


def session_for(mode: str, cdp: str | None) -> BrowserSession:
    """The process-wide session object for a browser mode (cheap: nothing starts until used)."""
    key = (mode, cdp)
    if key not in _sessions:
        _sessions[key] = BrowserSession(mode, cdp)
    return _sessions[key]


def _reset_for_tests() -> None:
    _sessions.clear()


__all__ = ["BrowserSession", "Tab", "TabLost", "session_for", "session_name", "login_place", "cookie_jar", "origin_of",
           "MANAGED_SESSION", "NATIVE_SESSION", "DEFAULT_CDP"]
