"""LINUX DO (linux.do): a Chinese Discourse community on Linux, programming, AI tools and self-hosting.

Cloudflare lets Chrome's network fingerprint (T2) through most of the time but sometimes answers it
with a 429 challenge; a call that meets one switches to a generic fetch() in the ESUM browser
window (T3), which clears it (D-40). Reads carry the browser's cookies: search and some
categories need a login, and after the user logs in there once, LOGIN NEEDED answers turn into
results. Categories come from `/site.json`.
"""

from esum import Transport, site

FORMAT = 1
PORTED_FROM = "original site config on the Discourse engine (no external source)"

linuxdo = site(
    name="linuxdo",
    engine="discourse",
    base_url="https://linux.do",
    transport=Transport.BROWSER_HTTP,  # T2 with the browser's cookies (search is login-only)
    fallback=Transport.IN_PAGE,        # T3 when Cloudflare challenges T2
    label={
        "en": "LINUX DO (linux.do): Chinese community on Linux, programming, AI tools, self-hosting "
              "and deals",
        "zh": "LINUX DO：中文技术社区，Linux、编程、AI 工具、自建服务与羊毛",
    },
    extras={"min_interval": 1.0, "guest_search": False},
)
