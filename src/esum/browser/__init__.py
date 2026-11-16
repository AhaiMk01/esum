"""The browser layer: Chrome driven through patchright-cli's daemon and named sessions (§7).

ESUM never launches Chrome itself and never duplicates patchright-cli. It sends commands to the
patchright-cli daemon (started on demand) in one named session per browser mode:

    managed (default)  session `esum`: a persistent profile in ~/.esum/browser/profile, in its own
                       window. The user logs in there once; the profile keeps the login.
    attach             session `esum-attach-<host>-<port>`: a Chrome the user started on a
                       dedicated profile with a debugging port (`--cdp=<url>`, default
                       http://127.0.0.1:9222), using its own cookies (host context).
    native             session `esum-native`: the user's everyday Chrome, after they turn on
                       chrome://inspect/#remote-debugging (Chrome 144+); its address comes from
                       Chrome's DevToolsActivePort file. Detached, never closed.

Nothing here is imported for T1 sites, and nothing starts until a T2/T3 request is sent.
Each call (one `esum` action) works in its own named tab (`--tab=<name>`, patchright-cli 0.8+) and
closes it at the end, so parallel calls never touch each other's pages (see `session.py`). When an action
falls short, the agent can drive the same session with plain patchright-cli commands, e.g.
`patchright-cli -s=esum snapshot` (never `esum-native`: that is the user's own browser).

Modules:
    daemon.py   socket client for the patchright-cli daemon; starts it via `patchright-cli`
    session.py  BrowserSession (open/attach, cookie jar, user agent) and per-call Tab
    login.py    the login-detection hook (`extras["login_check"]`) T2/T3 run on every response
"""

from __future__ import annotations

from .daemon import BrowserError, DaemonClient
from .login import check_login, login_problem
from .session import BrowserSession, Tab, TabLost, origin_of, session_for

__all__ = ["BrowserError", "DaemonClient", "BrowserSession", "Tab", "TabLost", "session_for",
           "origin_of", "check_login", "login_problem"]
