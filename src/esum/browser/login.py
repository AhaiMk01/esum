"""The login-detection hook the browser transports (T2, T3) run on every response.

A site declares how a logged-out session looks in `extras["login_check"]`, either as a dict:

    site(..., transport=Transport.IN_PAGE, extras={"login_check": {
        "status": [401, 403],                     # any of these statuses
        "url": [r"/login\\b", r"/session/new"],   # regex searched in the final URL (after redirects)
        "body": [r'<form[^>]+id="loginform"'],    # regex searched in the decoded body
    }})

or as a callable `check(response) -> str | bool | None` that returns a truthy value (a string is
used as the detail) when the response shows a logged-out session. Any one match raises
`LoginNeeded`, which prints the fixed LOGIN NEEDED message naming the site and the browser window
the user should log in to. Without a `login_check` nothing is detected here; actions can still
`raise ctx.login_needed()` themselves after parsing a response.

Keep checks narrow: a body pattern that also appears on logged-in pages (a "Log in" link in a
thread) would stop every read. Prefer statuses and login-page URLs.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..core.errors import LoginNeeded
from ..transport import Response, charset_of, decode

if TYPE_CHECKING:
    from ..core.model import Site

KEYS = frozenset({"status", "url", "body"})


def login_problem(site: "Site", response: Response) -> str | None:
    """Why `response` looks logged out according to the site's `login_check`, or None."""
    check: Any = site.extras.get("login_check")
    if check is None:
        return None
    if callable(check):
        verdict = check(response)
        if not verdict:
            return None
        return verdict if isinstance(verdict, str) else "the site's login check matched"
    if not isinstance(check, dict) or not set(check) <= KEYS:
        raise ValueError(f"{site.name}: extras['login_check'] must be a callable or a dict with "
                         f"keys {sorted(KEYS)}; got {check!r}")
    if response.status in set(check.get("status", ())):
        return f"HTTP {response.status} from {response.url}"
    for pattern in check.get("url", ()):
        if re.search(pattern, response.url):
            return f"redirected to {response.url}"
    patterns = check.get("body", ())
    if patterns:
        text = decode(response.content, site.charset or charset_of(response.headers, response.content))
        for pattern in patterns:
            if re.search(pattern, text):
                return f"{response.url} shows a logged-out page"
    return None


def check_login(site: "Site", response: Response, where: str | None) -> None:
    """Raise LoginNeeded when `response` matches the site's login check."""
    detail = login_problem(site, response)
    if detail:
        raise LoginNeeded(site.name, site.base_url, where=where, detail=detail)
