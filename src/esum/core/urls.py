"""ESUM requests only web URLs. This module is the one place that says what that means.

Every request URL is built on a site's `base_url` unless it is absolute, so the rule is enforced
twice: `site()` refuses a non-web `base_url` when the module loads, and `HttpClient.url` refuses any
non-web URL before a transport sees it. Both matter because the transports disagree: httpx (T1)
refuses `file://`, but curl_cffi (T2) reads it from disk (it refuses `file://` only in a redirect).
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

WEB_SCHEMES = frozenset({"http", "https"})
_ABSOLUTE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def is_absolute(url: str) -> bool:
    """Whether `url` names its own scheme (`x://…`) instead of being a path on the site."""
    return bool(_ABSOLUTE.match(url))


def is_web_url(url: str) -> bool:
    """An http(s) URL with a host. A URL `urlsplit` can't parse (a broken IPv6 host) is not one."""
    try:
        parts = urlsplit(url)
        return parts.scheme.lower() in WEB_SCHEMES and bool(parts.hostname)
    except ValueError:
        return False
