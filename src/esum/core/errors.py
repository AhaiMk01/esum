"""Errors. Two outcomes have a fixed, recognizable message: login needed (D-17) and an anti-bot check
the host agent completes in ESUM's tab (D-54). Everything else keeps its raw message, with a
non-zero exit code and a `help:` line naming the next thing to try (D-52).
(Exit code 4, WRITE NOT APPROVED, is retired with the write dialog, D-34; it is not reused.)"""

from __future__ import annotations

from .text import clip

LOGIN_NEEDED = "LOGIN NEEDED"
CHECK_NEEDED = "CHECK NEEDED"

# Exit codes. Anything other than 0 is a failure; login needed and check needed get their own codes.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_LOGIN_NEEDED = 3
EXIT_CHECK_NEEDED = 5

URL_CAP = 200   # an HTTP error message shows at most this much of its URL; `.url` keeps all of it


class EsumError(Exception):
    """Base class for errors ESUM raises itself."""


class LoginNeeded(EsumError):
    """The site needs a logged-in session. The user logs in themselves; ESUM never handles credentials.

    Raise it from an action (`raise ctx.login_needed()`) or from a transport backend when it detects
    a logged-out session (login redirect, 401/403 on an endpoint that needs an account, …).
    """

    exit_code = EXIT_LOGIN_NEEDED
    help = ("Don't stop a longer task for this: finish the rest, then tell the user at "
            "the end what needed a login. Run this command again once they have logged in")

    def __init__(self, site: str, base_url: str | None = None, where: str | None = None,
                 detail: str | None = None):
        self.site = site
        self.base_url = base_url
        self.where = where or "the browser ESUM uses"
        self.detail = detail
        target = f"{site} ({base_url})" if base_url else site
        msg = (f"esum: {LOGIN_NEEDED}: {target}. The user logs in themselves in {self.where}. "
               f"Never type or handle credentials.")
        if detail:
            msg += f" [{detail}]"
        super().__init__(msg)


class CheckNeeded(EsumError):
    """A browser check (a Cloudflare Turnstile "Verify you are human" box) still shows in ESUM's
    tab after the wait. ESUM solves nothing and calls no solver (D-54): it leaves its tab open and
    names it, so the host agent completes the check there with patchright-cli, then reruns the call.

    Raised by the browser layer only, never by an action.
    """

    exit_code = EXIT_CHECK_NEEDED

    def __init__(self, site: str, url: str, session: str, tab: str,
                 viewport: tuple[int, int] | None = None, zoom: float | None = None):
        self.site = site
        self.url = url
        self.session = session
        self.tab = tab
        self.viewport = viewport  # the page's CSS size; mouse coordinates are in these pixels
        self.zoom = zoom  # the page's visual (pinch) zoom; 1 is unzoomed
        super().__init__(f"esum: {CHECK_NEEDED}: {site} ({url}) shows a browser check in ESUM's tab "
                         f"'{tab}' (patchright-cli session '{session}').")

    @property
    def help(self) -> str:
        pc = f"patchright-cli -s={self.session} --tab={self.tab}"
        if self.zoom is not None and abs(self.zoom - 1) > 0.01:
            steps = (f"ESUM's tab is zoomed in (x{self.zoom:.2f}), so a screenshot doesn't map to "
                     f"click coordinates: don't click. Treat it like login needed: tell the user "
                     f"that ESUM's tab is zoomed and shows a check, then `{pc} close`")
        else:
            if self.viewport:
                scale = (f"the page is {self.viewport[0]}x{self.viewport[1]} CSS pixels: if the "
                         f"screenshot is wider, divide its x and y by (screenshot width / "
                         f"{self.viewport[0]})")
            else:
                scale = ("the page's size couldn't be read: take the screenshot's pixels as CSS "
                         "pixels, and if the click misses, divide x and y by 2 (a HiDPI "
                         "screenshot) for the next try")
            steps = (f"Complete the check in that tab: `{pc} screenshot` (the viewport, not "
                     f"--full-page), find the check box, then `{pc} mousemove <x> <y>`, "
                     f"`{pc} mousedown`, `{pc} mouseup` on it (a snapshot can't see inside the "
                     f"check's frame). Mouse coordinates are CSS pixels, and {scale}. Rerun this "
                     f"command a few seconds later, and `{pc} close` when done. Only a box to "
                     f"tick: anything more is the user's")
        return steps


class HttpError(EsumError):
    """A non-success HTTP status. The message is the raw status, URL and the start of the body,
    which is marked untrusted because the site (or a proxy in front of it) wrote it. An action that
    knows what a status means on its site can set `help`."""

    help: str | None = None
    wait: float | None = None   # for a 429: the seconds the site asked to wait, when ESUM gave up waiting

    def __init__(self, status: int, url: str, body: str = ""):
        self.status = status
        self.url = url
        self.body = body
        snippet = " ".join(body.split())[:300]
        super().__init__(f"HTTP {status} from {clip(url, URL_CAP)}" + (f": body (untrusted): {snippet}" if snippet else ""))


class ArgumentError(EsumError, ValueError):
    """A bad action argument (a malformed date, an unknown category, a range past the end). It exits
    with the usage code (2) and prints `help` as its own `help:` line, so the agent can fix the call."""

    def __init__(self, message: str, help: str | None = None):
        super().__init__(message)
        self.help = help


class UnsupportedURL(EsumError):
    """A request URL that isn't a web URL (core/urls.py). Raised before any transport runs; the
    site module built it, so it is an error (exit 1), not a usage mistake."""

    help = ("The site module built a non-web URL: the esum-site-author skill repairs a site. Pass "
            "http:// or https:// URLs only")


class ModuleFormatError(EsumError):
    """A site or engine module targets a module-format version this ESUM cannot load."""


class NoSuchItem(EsumError):
    """The requested id or name does not exist or has been removed."""


class SiteRefused(EsumError):
    """The site refused the call for a reason a login would not fix."""


class KeystoreError(EsumError):
    """The OS credential store would not change a saved key, or would not say whether it did."""

    def __init__(self, message: str, help: str):
        super().__init__(message)
        self.help = help


class TransportFailure(EsumError):
    """A transport error whose own text quoted a credential: this carries the same text with the
    credential removed, and nothing of the original (no cause, no attributes)."""


class WriteInterrupted(EsumError):
    """A write failed, with the original cause and completed items."""

    def __init__(self, cause: Exception, written: list[str]):
        self.cause = cause
        self.written = list(written)
        super().__init__(str(cause))


class SiteNotLoaded(EsumError):
    """A discovered site module failed to load (an error rather than bad usage)."""

    def __init__(self, message: str, help: str):
        super().__init__(message)
        self.help = help


class WriteLogError(EsumError):
    """A local write-log failure, distinct from the site's response."""

    def __init__(self, path: str, cause: Exception):
        self.path = path
        self.cause = cause
        super().__init__(f"{path}: {cause}")
