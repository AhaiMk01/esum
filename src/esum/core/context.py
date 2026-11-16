"""What an action receives as `ctx`."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from . import terminal
from .errors import ArgumentError, LoginNeeded
from .model import Transport

if TYPE_CHECKING:
    from ..transport import HttpClient
    from .model import Action, Site


@dataclass
class Options:
    """Global flags. Transport backends read `browser` / `cdp`; `extra` is room for backend settings."""

    json: bool = False
    browser: Literal["managed", "attach", "native"] = "managed"
    cdp: str | None = None
    fields: str | None = None    # extra list columns (`--fields a,b` or `all`)
    no_spill: bool = False       # print a large result instead of saving it (`--no-spill`)
    max_retries: int | None = None      # retries after HTTP 429 (`--max-retries`, ESUM_MAX_RETRIES)
    min_interval: float | None = None   # seconds between requests to a site (`--min-interval`, ESUM_MIN_INTERVAL)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Context:
    site: "Site"
    action: "Action"
    options: Options
    http: "HttpClient"
    secret: str | None = field(default=None, repr=False)   # what `ask_secret` answers, when the caller has it
    _clients: dict[tuple["Transport", bool | None], "HttpClient"] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        cookies = self.site.extras.get("browser_cookies", True) if self.site.transport is Transport.BROWSER_HTTP else None
        self._clients[(self.site.transport, cookies)] = self.http

    def http_for(self, transport: "Transport", *, browser_cookies: bool | None = None) -> "HttpClient":
        """A client cached for this transport and cookie policy, alive through all planned writes."""
        from ..transport import HttpClient

        cookies = self.site.extras.get("browser_cookies", True) if browser_cookies is None else browser_cookies
        key = (transport, cookies if transport is Transport.BROWSER_HTTP else None)
        if key not in self._clients:
            fallback = self.site.fallback
            if fallback is not None and fallback.rank <= transport.rank:
                fallback = None
            target = replace(self.site, transport=transport, fallback=fallback,
                             extras={**self.site.extras, "browser_cookies": cookies})
            self._clients[key] = HttpClient(target, self.options, dedupe=self.http.dedupe,
                                            throttle=self.http.throttle, session=self.http.browser_session,
                                            backend_factory=self.http.backend_factory)
        return self._clients[key]

    def page_script(self, url: str, script: str, arg: Any = None) -> Any:
        """Run a site's page script (D-95) in a tab ESUM opens on `url`, a page of this site, and
        return what it returns; the tab is closed after. `script` is a static file of the site
        package that reads: a built-in site's is reviewed and tested, a custom site's never runs."""
        from ..browser.session import origin_of

        if not self.site.builtin:
            raise ArgumentError(f"{self.site.name} is a custom site: page scripts run for built-in sites only")
        if origin_of(url) != origin_of(self.site.base_url):
            raise ValueError(f"a page script runs on its own site: {url} is not on {self.site.base_url}")
        self.http.wait_turn()
        tab = self.http.browser_session.open_tab(url, site=self.site.name)
        try:
            return tab.evaluate(script, arg)
        finally:
            tab.close()

    def close(self) -> None:
        """Close every client after planning and writes, also on failure."""
        with ExitStack() as cleanup:
            for client in self._clients.values():
                cleanup.callback(client.close)

    def require_terminal(self) -> None:
        """Refuse unless a person is at the keyboard: an interactive action is the user's to run, in
        their own terminal, never an agent's."""
        if not terminal.interactive():
            command = f"esum {self.site.name} {self.action.name}"
            raise ArgumentError(f"`{command}` is interactive, so the user runs it in their own terminal",
                                help=f"Tell the user to run `{command}` themselves")

    def ask_secret(self, prompt: str) -> str:
        """A secret only the user has, such as an API key. It comes from the user's own terminal with
        hidden input, or from `esum keys ui`, where they typed it after the OS confirmed them; an
        action never learns which. Without either it refuses (`require_terminal`). Never print it."""
        if self.secret is not None:
            return self.secret
        self.require_terminal()
        import getpass
        return getpass.getpass(prompt)

    def login_needed(self, detail: str | None = None, where: str | None = None) -> LoginNeeded:
        """`raise ctx.login_needed()` when the site shows a logged-out session."""
        return self.http.login_needed(detail=detail, where=where)
