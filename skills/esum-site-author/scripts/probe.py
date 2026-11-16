# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "curl-cffi>=0.17"]
# ///
"""First look at a website for an ESUM site: which transport answers, the charset, the platform,
feeds, sitemaps, embedded page data and API paths the HTML mentions.

    uv run skills/esum-site-author/scripts/probe.py https://forum.example [--page URL …] [--save DIR]

Read-only: a handful of GETs, spaced 1 s apart, no cookies, no browser. T1 sends ESUM's own
user agent, so a T1 verdict predicts what ESUM's direct transport gets. It can't test T3 or T2 with
the user's cookies: those sources are checked in the browser (references/discovery.md).
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from curl_cffi import requests as cffi

ESUM_UA = "Mozilla/5.0 (compatible; esum/0.1.0)"   # esum.transport.direct.USER_AGENT
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/131.0.0.0 Safari/537.36")
SPACING = 1.0
CHALLENGE_STATUSES = {403, 429, 503}
CHALLENGE_TITLES = re.compile(r"<title[^>]*>\s*(just a moment|attention required|ddos-guard|"
                              r"checking your browser|security check|请稍候|安全验证|访问验证)", re.I)
LOGIN_URL = re.compile(r"/(login|signin|sign_in|sign-in|session/new|passport|account/login)\b|"
                       r"member\.php\?mod=logging|[?&](redirect_to|return_to|next)=", re.I)

# platform -> (structural markers searched in the home page, endpoint confirming it, ESUM engine)
# A platform counts only on its generator meta or a structural marker, never on a mention.
PLATFORMS = {
    "discourse": ([r'name="generator" content="Discourse', r"data-discourse-setup"], "latest.json", "discourse"),
    "wordpress": ([r'name="generator" content="WordPress', r'<link[^>]+rel="https://api\.w\.org/"'],
                  "wp-json/", "wordpress"),
    "vbulletin": ([r'name="generator" content="vBulletin', r"<script[^>]+vbulletin_global\.js"], None, "vbulletin"),
    "discuz": ([r'name="generator" content="Discuz!', r"discuz_uid\s*="], None, "discuz"),
    "xenforo": ([r'<html[^>]+data-xf="', r'id="XF"'], None, None),
    "phpbb": ([r'<body[^>]+id="phpbb"', r'name="generator" content="phpBB'], None, None),
    "flarum": ([r'<div id="flarum-loading"', r"flarum\.core\.app"], "api/discussions?page[limit]=1", None),
    "nodebb": ([r'name="generator" content="NodeBB', r"<script[^>]+nodebb\.min\.js"], "api/recent", None),
    "lemmy": ([r'name="generator" content="Lemmy', r"window\.isoData\s*="], "api/v3/site", None),
    "mediawiki": ([r'name="generator" content="MediaWiki'], "api.php?action=query&meta=siteinfo&format=json", None),
    "ghost": ([r'name="generator" content="Ghost'], None, None),
    "substack": ([r"window\._preloads\s*="], "api/v1/archive?sort=new&limit=1", None),
}
EMBEDDED = {
    "__NEXT_DATA__": r'<script id="__NEXT_DATA__"',
    "__NUXT__": r"window\.__NUXT__",
    "initial state": r"window\.__(INITIAL|PRELOADED)_STATE__",
    "JSON-LD": r'<script type="application/ld\+json"',
}


@dataclass
class Fetch:
    via: str
    url: str                  # requested
    status: int = 0
    final: str = ""
    headers: dict | None = None
    body: bytes = b""
    error: str = ""

    @property
    def charset(self) -> str:
        return charset_of(self.headers or {}, self.body)

    @property
    def text(self) -> str:
        return decode(self.body, self.charset)

    @property
    def kind(self) -> str:
        """error | challenge | login | refused | empty | ok"""
        if self.error:
            return "error"
        if is_challenge(self.status, self.headers or {}, self.text):
            return "challenge"
        if LOGIN_URL.search(self.final) and not LOGIN_URL.search(self.url):
            return "login"
        if not 200 <= self.status < 300:
            return "refused"
        return "ok" if self.body.strip() else "empty"


def is_challenge(status: int, headers: dict[str, str], body: str) -> bool:
    """esum.transport.is_cloudflare_challenge, plus interstitial titles on any status (other WAFs
    answer 200)."""
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    low = body[:20000].lower()
    if status in CHALLENGE_STATUSES and any(s in low for s in (
            "just a moment", "/cdn-cgi/challenge-platform/", "cf-challenge", "attention required")):
        return True
    return bool(CHALLENGE_TITLES.search(body[:20000]))


def charset_of(headers: dict[str, str], body: bytes) -> str:
    m = re.search(r"charset=[\"']?([\w.:-]+)", headers.get("content-type", ""), re.I)
    if m:
        return f"{m.group(1).lower()} (header)"
    m = re.search(rb"<meta[^>]+charset=[\"']?([\w.:-]+)", body[:4096], re.I)
    return f"{m.group(1).decode().lower()} (meta)" if m else "none declared (utf-8 assumed)"


def decode(body: bytes, charset: str) -> str:
    """As ESUM decodes (module-format §7): Latin-1 and ASCII labels mean windows-1252."""
    name = charset.split()[0]
    if name == "none":
        name = "utf-8"
    elif name in ("iso-8859-1", "latin-1", "latin1", "us-ascii", "ascii"):
        name = "windows-1252"
    try:
        return body.decode(name, errors="replace")
    except (LookupError, UnicodeError, ValueError):
        return body.decode("utf-8", errors="replace")


class Prober:
    def __init__(self) -> None:
        self.t1 = httpx.Client(follow_redirects=True, timeout=20)
        self.t2 = cffi.Session(impersonate="chrome")
        self.last = 0.0

    def get(self, url: str, via: str) -> Fetch:
        delay = self.last + SPACING - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.last = time.monotonic()
        try:
            if via == "t2":
                r = self.t2.get(url, timeout=20, allow_redirects=True)
                final = r.url
            else:
                r = self.t1.get(url, headers={"User-Agent": CHROME_UA if via == "t1-chrome-ua" else ESUM_UA})
                final = str(r.url)
            return Fetch(via, url, r.status_code, final, {k.lower(): v for k, v in r.headers.items()}, r.content)
        except Exception as e:  # noqa: BLE001 - report any network failure as text
            return Fetch(via, url, error=f"{type(e).__name__}: {e}")


def line(label: str, value: str) -> None:
    print(f"{label:<14}{value}")


def show(label: str, f: Fetch) -> None:
    if f.error:
        line(label, f.error)
        return
    note = {"challenge": "  CHALLENGE PAGE", "login": "  LOGIN WALL", "empty": "  EMPTY BODY"}.get(f.kind, "")
    line(label, f"{f.status}  {(f.headers or {}).get('content-type', '?')}  {len(f.body) // 1024} KB  "
                f"server={(f.headers or {}).get('server', '?')}{note}")
    if f.final.rstrip("/") != f.url.rstrip("/"):
        line("", f"redirected to {f.final}")


def verdict(t1: Fetch, t1c: Fetch | None, t2: Fetch) -> str:
    answered = ("ok", "empty")
    if t1.kind in answered:
        empty = "" if t1.kind == "ok" else " (the page is empty; probe a list with --page)"
        return "T1 works: Transport.DIRECT" + empty
    if t1c and t1c.kind in answered:
        return ("T1 works only with a browser user agent: Transport.DIRECT with "
                "headers={'User-Agent': <a Chrome UA>}, or Transport.BROWSER_HTTP")
    if t2.kind in answered:
        return "T1 refused, T2 works: Transport.BROWSER_HTTP (extras browser_cookies False if no login)"
    fetches = [f for f in (t1, t1c, t2) if f]
    if any(f.kind == "login" or (f.kind == "refused" and f.status in (401, 403)) for f in fetches):
        return ("login wall: guests are shut out. Open it in a browser (discovery.md); if a logged-in "
                "browser reads it, use Transport.BROWSER_HTTP with the browser's cookies and a login_check")
    return ("neither answered (challenge or error): check in the browser that a real one gets "
            "through, then Transport.IN_PAGE (T3); or a Markdown site")


def fixture_name(f: Fetch) -> str:
    """Readable path + query, plus a short digest of the full URL so distinct URLs never collide."""
    u = urlparse(f.url)
    stem = re.sub(r"[^\w.-]", "_", (u.path.strip("/") + ("_" + u.query if u.query else "")) or "home")[:60]
    ext = ".json" if "json" in (f.headers or {}).get("content-type", "") else ".html"
    return f"{stem.removesuffix(ext)}-{hashlib.sha1(f.url.encode()).hexdigest()[:8]}{ext}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("url", help="the site's home page (or its base URL, sub-path included)")
    ap.add_argument("--page", action="append", default=[],
                    help="also fetch this page (a thread, a list, a .json URL) over T1 and T2; a full URL "
                         "(Git Bash rewrites a bare /path); repeatable")
    ap.add_argument("--save", metavar="DIR",
                    help="save each --page body a transport got cleanly into DIR, as a raw fixture to scrub")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    base = args.url if "://" in args.url else f"https://{args.url}"
    p = Prober()

    print(f"probe {base}  (read-only GETs, {SPACING:.0f} s apart, no cookies)\n")
    print("== transport")
    t1 = p.get(base, "t1")
    show("T1 direct", t1)
    t1c = None
    if t1.kind not in ("ok", "empty"):
        t1c = p.get(base, "t1-chrome-ua")
        show("T1 chrome UA", t1c)
    t2 = p.get(base, "t2")
    show("T2 chrome-tls", t2)
    line("verdict", verdict(t1, t1c, t2))

    good = next((f for f in (t1, t1c, t2) if f and f.kind == "ok"), None)
    via = good.via if good else "t1"
    for page in args.page:
        url = urljoin(base, page)
        print(f"\n{url}")
        got = [p.get(url, "t1"), p.get(url, "t2")]
        show("  T1", got[0])
        show("  T2", got[1])
        ok = next((f for f in got if f.kind == "ok"), None)
        if args.save and ok:
            out = Path(args.save).expanduser() / fixture_name(ok)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(ok.body)
            line("  saved", f"{out}  (from {ok.via.upper()}; raw: scrub before keeping)")
        elif args.save:
            line("  not saved", "no transport got it cleanly; capture it in the browser (discovery.md)")
    if not good:
        return 0

    html = good.text
    # Resolve from where the page really is: a redirect moves the origin, a sub-path install keeps its prefix.
    home = good.final if good.final.endswith("/") else good.final.rsplit("/", 1)[0] + "/"
    origin = f"{urlparse(good.final).scheme}://{urlparse(good.final).netloc}"
    print()
    line("charset", good.charset)
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    line("title", " ".join(title.group(1).split())[:100] if title else "?")
    gen = re.search(r'<meta[^>]+name="generator"[^>]+content="([^"]+)"', html, re.I)
    line("generator", gen.group(1) if gen else "none")

    print("\n== platform")
    found = False
    for name, (markers, endpoint, engine) in PLATFORMS.items():
        hits = [m for m in markers if re.search(m, html, re.I)]
        if not hits:
            continue
        found = True
        note = f"ESUM engine `{engine}`: a site config" if engine else "no ESUM engine: standalone module"
        line(name, f"{len(hits)}/{len(markers)} markers; {note}")
        for url in dict.fromkeys([urljoin(home, endpoint), urljoin(origin + "/", endpoint)] if endpoint else []):
            r = p.get(url, via)
            ok = r.kind == "ok" and "json" in (r.headers or {}).get("content-type", "")
            line("", f"{url} -> {r.error or r.status}{'  JSON: confirmed' if ok else ''}")
            if ok:
                break
    if not found:
        line("unknown", "no known platform markers: standalone module or Markdown site")

    print("\n== data sources")
    feeds = re.findall(r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]*href="([^"]+)"', html, re.I)
    line("feeds", ", ".join(dict.fromkeys(urljoin(good.final, f) for f in feeds)) or "none linked")
    alts = re.findall(r'<link[^>]+type="application/json"[^>]*href="([^"]+)"', html, re.I)
    if alts:
        line("json links", ", ".join(dict.fromkeys(urljoin(good.final, a) for a in alts))[:300])
    embedded = [k for k, pat in EMBEDDED.items() if re.search(pat, html)]
    line("embedded", ", ".join(embedded) or "none (data may come from XHR: check the browser's network log)")
    apis = sorted(set(re.findall(r"""["'`](/(?:api|ajax|graphql|v\d|wp-json|_next/data)[\w/.\-?=&]*)""", html)))
    line("api paths", ", ".join(apis[:15]) + (" …" if len(apis) > 15 else "") if apis else "none in the HTML")
    robots = p.get(origin + "/robots.txt", via)
    if robots.kind != "ok":
        line("robots.txt", robots.error or str(robots.status))
    else:
        text = robots.body.decode("utf-8", errors="replace")
        maps = re.findall(r"(?im)^sitemap:\s*(\S+)", text)
        dis = list(dict.fromkeys(re.findall(r"(?im)^disallow:\s*(\S+)", text)))
        line("sitemaps", ", ".join(maps[:5]) or "none")
        line("disallow", ", ".join(dis[:12]) + (" …" if len(dis) > 12 else "") if dis else "none")
    print("\nNext: explore in the browser (references/discovery.md) to confirm each source.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
