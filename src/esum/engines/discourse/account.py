"""One call's Discourse identity, credentials, login outcomes and write authentication."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from esum import ArgumentError, Context, Credential, HttpError, LoginNeeded, Transport
from esum.transport import HttpClient
from . import auth, parse


def account_for(ctx: Context) -> Account:
    """Keep the account alive with this action's context, including its planned writes."""
    if "_discourse_account" not in vars(ctx):
        ctx._discourse_account = Account(ctx)
    return ctx._discourse_account


class Account:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.adopt(auth.load_key(ctx.site.name))

    def adopt(self, saved: dict | None) -> None:
        self.saved = saved
        self.credential = Credential(auth.headers(saved)) if saved else None
        self.user_credential = None

    def is_key(self, credential: Credential | None) -> bool:
        return bool(self.credential and credential and credential.headers == self.credential.headers)

    def _key_for(self, http: HttpClient) -> bool:
        return bool(self.credential and self.credential.for_transport(http.site.transport) is self.credential)

    @property
    def is_guest(self) -> bool:
        return not self._key_for(self.ctx.http)

    def _can_write(self, http: HttpClient) -> bool:
        return bool(self.saved and "write" in self.saved.get("scopes", []) and self._key_for(http))

    def for_write(self) -> WriteAccount:
        """Plan and send as one account, using a writable key or the browser's identity."""
        http = self._write_http()
        chosen = self.credential if self._can_write(http) else Credential(page_safe=True)
        user = self.current_user(http, credential=chosen)
        if not user:
            raise http.login_needed(where=self.login_place(http), detail="the site shows no logged-in user")
        username = str(user.get("username") or "?")
        return WriteAccount(self, http, username, chosen, self._write_credential(http, username, chosen))

    def login_place(self, http: HttpClient, *, key: bool = False) -> str:
        if key or (not http.uses_browser_login and self.ctx.site.transport is Transport.BROWSER_HTTP
                   and not self.ctx.site.extras.get("browser_cookies", True)):
            return f"their own terminal, by running `esum {self.ctx.site.name} login`"
        return http.login_where

    def invalid_login(self, http: HttpClient, browser_detail: str) -> LoginNeeded:
        key_sent = self.is_key(self.user_credential)
        return http.login_needed(where=self.login_place(http, key=key_sent),
                                 detail="the saved key no longer works" if key_sent else browser_detail)

    def _write_credential(self, http: HttpClient, username: str, chosen: Credential) -> Credential:
        verified = False
        browser_read = Credential(page_safe=True)

        def verify() -> None:
            nonlocal verified
            if verified:
                return
            actual = str((self.current_user(http, credential=browser_read) or {}).get("username") or "(logged out)")
            if actual.casefold() != username.casefold():
                raise http.login_needed(detail=f"bookmark planned for @{username}, but the browser is @{actual}; no browser write was sent")
            verified = True

        browser = Credential(page_safe=True, verify=verify)
        return Credential(chosen.headers, fallback=browser) if self.is_key(chosen) else browser

    def fetch_json(self, path: str, params: dict[str, Any] | None = None, *, http: HttpClient | None = None,
                   method: str = "GET", headers: dict[str, str] | None = None, data: Any = None,
                   dedupe: bool | None = None, credential: Credential | None = None,
                   failure_help: str | None = None) -> Any:
        """One Discourse API call. Translate `not_logged_in` to LOGIN NEEDED;
        other API failures pass through raw as HttpError, while transport handles bot checks."""
        ctx = self.ctx
        http = http or ctx.http
        selected = credential or self.credential
        key = self.is_key(selected.for_transport(http.site.transport) if selected else None)
        resp = http.request(method, path, params=params, check=False, dedupe=dedupe,
                            headers={"Accept": "application/json", **(headers or {})}, credential=credential or self.credential,
                            **({"json": data} if data is not None else {}))
        # A challenge can move this very request to T3, which strips the key (D-46).
        # Only Discourse's explicit CSRF rejection proves the write was not applied.
        if (method not in ("GET", "HEAD") and key and not self.is_key(resp.credential)
                and resp.status == 403):
            if csrf_rejected(resp):
                csrf = self.fetch_json("/session/csrf.json", http=http, dedupe=False).get("csrf")
                if not csrf:
                    raise http.login_needed(detail="the browser session supplied no CSRF token; write was rejected")
                resp = http.request(method, path, params=params, check=False, dedupe=False, credential=credential or self.credential,
                                    headers={"Accept": "application/json", **(headers or {}),
                                             "X-CSRF-Token": csrf, "X-Requested-With": "XMLHttpRequest"},
                                    **({"json": data} if data is not None else {}))
            if not resp.ok:
                error = resp.http_error()
                error.help = ("The request switched to browser authentication; this write was not replayed again. "
                              + (failure_help or f"Run `esum {ctx.site.name} --help` to find a read action and check the item's state before retrying"))
                raise error
        body = resp.text()
        try:
            payload = json.loads(body) if body.strip() else None
        except ValueError:
            payload = None
        if not resp.ok:
            if self.is_key(resp.credential) and key and resp.status in (401, 403) and self.current_user(http) is None:
                raise self.invalid_login(http,
                                     f"HTTP {resp.status} from {resp.url}; browser login check found no logged-in user")
            if parse.error_type(payload) == "not_logged_in":
                raise http.login_needed(where=self.login_place(http), detail=f"HTTP {resp.status} not_logged_in from {resp.url}")
            raise resp.http_error(body[:2000])
        if payload is None:
            raise HttpError(resp.status, resp.url, "expected JSON, got: " + body[:300])
        return payload

    def guest_scope(self) -> str | None:
        """Said on forum-wide reads: a guest's results quietly leave out what only members can see."""
        ctx = self.ctx
        if self._key_for(ctx.http):
            return None
        if any(c.get("restricted") for c in ctx.site.extras.get("categories") or []):
            return "guest view: member-only categories are not included"
        return "no saved key: categories only members see may be missing"   # a guest's /site.json hides them

    def member_only(self, cat: dict) -> Exception:
        """A saved key that still can't see a category needs a higher account level, not another login."""
        ctx = self.ctx
        if self._key_for(ctx.http):
            return ArgumentError(f"{cat['path']} is not visible to this account on {ctx.site.name}",
                                 help="it is a member-only category, and logging in again won't change that")
        return ctx.login_needed(where=self.login_place(ctx.http), detail=f"{cat['path']} is a member-only category")

    def _write_http(self) -> HttpClient:
        """The client for login-only calls: the site's own transport when a saved key may write, else
        `extras["write_transport"]`, else T2 for a T1 site, else the site's transport; T2 here always
        carries the browser's cookies."""
        ctx = self.ctx
        if self._can_write(ctx.http):
            return ctx.http
        wanted = ctx.site.extras.get("write_transport")
        transport = Transport(wanted) if wanted else (
            Transport.BROWSER_HTTP if ctx.site.transport is Transport.DIRECT else ctx.site.transport)
        return ctx.http_for(transport, browser_cookies=True)

    def current_user(self, http: HttpClient, *, credential: Credential | None = None) -> dict | None:
        """The logged-in user, or None (Discourse answers 404 to anonymous `/session/current.json`)."""
        resp = http.request("GET", "/session/current.json", check=False, dedupe=False,
                            headers={"Accept": "application/json"}, credential=credential or self.credential)
        self.user_credential = resp.credential
        body = resp.text()
        if resp.status in (401, 403, 404):
            return None
        if not resp.ok:
            raise resp.http_error(body[:2000])
        return (json.loads(body) or {}).get("current_user")


@dataclass
class WriteAccount:
    """A planned writer keeps its account and verification across all bookmark items."""

    account: Account
    http: HttpClient
    username: str
    read_credential: Credential
    credential: Credential

    def fetch_json(self, path: str) -> Any:
        return self.account.fetch_json(path, http=self.http, credential=self.read_credential, dedupe=False)

    def post(self, path: str, data: Any, *, failure_help: str | None = None) -> Any:
        headers = {}
        if not self.account.is_key(self.credential.for_transport(self.http.site.transport)):
            csrf = self.fetch_json("/session/csrf.json").get("csrf")
            headers = {"X-CSRF-Token": csrf or "", "X-Requested-With": "XMLHttpRequest"}
        return self.account.fetch_json(path, http=self.http, method="POST", data=data, headers=headers,
                                       credential=self.credential, dedupe=False, failure_help=failure_help)


def csrf_rejected(response) -> bool:
    """Discourse rejects CSRF before applying the action; accept only its exact marker."""
    if response.status != 403:
        return False
    body = response.text().strip()
    try:
        value = json.loads(body)
    except ValueError:
        return body.casefold() == "bad csrf"
    return (isinstance(value, list) and len(value) == 1 and isinstance(value[0], str)
            and value[0].casefold() == "bad csrf")

