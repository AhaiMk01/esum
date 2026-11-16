"""Pure parsers for NodeSeek (www.nodeseek.com).

Every page carries its data as base64-encoded JSON in `<script id="temp-script">` (the front end
decodes it into `window.__config__`): the category list on every page, and on a post page
`postData` with the post's title, category, page count and that page's comments in Markdown.
There is no JSON API for content, so a post is read from that embedded JSON. List pages carry only
the category list there; their rows are server-rendered HTML (`<li class="post-list-item">`).

A post page holds 10 floors: page 1 is the opening post (floor 0) and floors 1-10, page n holds
floors 10(n-1)+1 .. 10n. Page 1 also repeats the post's "hot" comments from any page; they carry
their real floor numbers and are kept once. ESUM keeps the site's floor numbers: the opening post
is #0 and comment floor N is #N, so the `#N` links people write in comments match the output.
"""

from __future__ import annotations

import base64
import binascii
import html as htmllib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from esum import (ArgumentError, Post, one_line, parse_date)

BASE = "https://www.nodeseek.com"
PER_PAGE = 10
MEMBERS_ONLY = "本帖需要注册用户才能查看"

_CONFIG = re.compile(r'<script id="temp-script" type="application/json">\s*([^<]*?)\s*</script>')
_THREAD_REF = re.compile(r"^\s*(\d+)\s*$|/post-(\d+)(?:-\d+)?")
# "@user [#19](https://www.nodeseek.com/post-1012229-2#19)" at the start of a comment
_REPLY = re.compile(r"^\s*@(\S+?)\s*\[#(\d+)\]\((?:https?://(?:www\.)?nodeseek\.com)?/post-(\d+)-\d+#\d+\)[ \t]*\n*")


# --- the embedded config ------------------------------------------------------------------------------

def page_config(html: str) -> dict | None:
    """The page's `__config__` (base64 JSON in `#temp-script`), or None when the page has none."""
    m = _CONFIG.search(html)
    if not m:
        return None
    try:
        data = json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_guest(config: dict | None) -> bool:
    return not (config or {}).get("user")


def page_message(html: str) -> str | None:
    """The text of the site's message page ("本帖需要注册用户才能查看😭"), or None."""
    m = re.search(r'<div style="min-height:300px;[^"]*"><div[^>]*>(.*?)</div>', html, re.S)
    return one_line(htmllib.unescape(re.sub(r"<[^>]+>", "", m.group(1)))) or None if m else None


def categories(config: dict | None) -> list[dict]:
    """The category list: key (for `listing --category`), name, and where the site shows it."""
    out = []
    for c in (config or {}).get("allCategory") or []:
        if not isinstance(c, dict) or not c.get("key"):
            continue
        out.append({"key": one_line(c["key"]), "name": one_line(c.get("cn_text") or c["key"]),
                    "in_site_list": bool(c.get("showInIndex")), "in_nav": bool(c.get("showInNav")),
                    "admin_only": bool(c.get("adminOnly"))})
    return out


# --- list pages ---------------------------------------------------------------------------------------

def _num(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _text(fragment: str | None) -> str:
    return one_line(htmllib.unescape(re.sub(r"<[^>]+>", "", fragment or "")))


def _first(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text, re.S)
    return m.group(1) if m else None


def parse_row(item: str) -> dict | None:
    """One `<li class="post-list-item">` of a list page."""
    m = re.search(r'class="post-title">\s*<a href="/post-(\d+)-1"[^>]*>(.*?)</a>', item, re.S)
    if not m:
        return None
    pid = int(m.group(1))
    title_html = _first(r'class="post-title">(.*?)</div>', item) or ""
    author = re.search(r'class="info-item info-author">.*?<a href="/space/(\d+)">(.*?)</a>', item, re.S)
    last_by = _first(r'class="info-item info-last-commenter">.*?<a href="/space/\d+">(.*?)</a>', item)
    last_at = _first(r'class="info-item info-last-comment-time"[^>]*>\s*<time[^>]*datetime="([^"]+)"', item)
    category = re.search(r'<a href="/categories/([^"]+)" class="info-item post-category">(.*?)</a>', item, re.S)
    lock = _first(r'href="#lock"></use></svg>\s*(\d+)', title_html)
    pinned, level, read_only = 'title="置顶"' in title_html, _num(lock) if lock else 0, "nsk-badge read-only" in title_html
    return {
        "id": pid,
        "title": _text(m.group(2)),
        "author": _text(author.group(2)) if author else None,
        "author_uid": int(author.group(1)) if author else None,
        "category": category.group(1) if category else None,
        "category_name": _text(category.group(2)) if category else None,
        "replies": _num(_first(r'class="info-item info-comments-count">.*?<span[^>]*>(\d+)</span>', item)),
        "views": _num(_first(r'class="info-item info-views">.*?<span[^>]*>(\d+)</span>', item)),
        "last_reply": last_at,
        "last_reply_by": _text(last_by) if last_by else None,
        "pinned": pinned,
        "read_level": level,
        "read_only": read_only,
        "flags": [f for f, on in (("pinned", pinned), (f"read level {level}", level), ("read-only", read_only)) if on],
        "url": f"{BASE}/post-{pid}-1",
    }


def parse_list_page(html: str) -> dict:
    """A list page (site-wide or one category): its rows, current page and whether a next page exists."""
    rows = []
    for chunk in html.split('<li class="post-list-item">')[1:]:
        if (row := parse_row(chunk)) is not None:
            rows.append(row)
    pager = _first(r'<div class="nsk-pager pager-top">(.*?)</div></div>', html) or ""
    current = _num(_first(r'class="pager-pos pager-cur">(\d+)<', pager), 1)
    last = max([_num(n) for n in re.findall(r'page-(\d+)"', pager)] + [current])
    return {"page": current, "last_page": last, "has_next": 'rel="next"' in pager, "threads": rows}


# --- post pages ---------------------------------------------------------------------------------------

def thread_ref(ref: str) -> int:
    """A post id or post URL (`/post-<id>-<page>`, with or without `#floor`) -> id."""
    m = _THREAD_REF.search(str(ref))
    if not m:
        raise ArgumentError(f"{ref!r} is not a nodeseek post id or URL",
                            help="Pass the post number, or a nodeseek.com/post-<id>-<page> URL")
    return int(m.group(1) or m.group(2))


def page_of(floor: int) -> int:
    """The post page holding a floor (floor 0, the opening post, is on page 1)."""
    return 1 if floor <= PER_PAGE else (floor - 1) // PER_PAGE + 1


def floor_url(pid: int, floor: int) -> str:
    return f"{BASE}/post-{pid}-{page_of(floor)}" + (f"#{floor}" if floor else "")


@dataclass
class Floor:
    floor: int
    user: str
    is_op: bool
    markdown: str
    date: datetime | None
    upvotes: int
    chicken_legs: int
    edited: str | None = None


@dataclass
class PostPage:
    id: int
    title: str
    category: str | None
    category_name: str | None
    page: int
    pages: int
    views: int
    locked: bool
    op: str | None
    floors: list[Floor] = field(default_factory=list)


def _stamp(value: str | None) -> str | None:
    when = parse_date(value)
    return f"{when:%Y-%m-%d %H:%M}" if when else None


def parse_post_page(config: dict, pid: int) -> PostPage:
    """`postData` of a post page's config -> the page's floors (hot repeats included)."""
    pd = config.get("postData")
    if not isinstance(pd, dict):
        raise RuntimeError(f"nodeseek post {pid}: the page carries no post data (the layout may have changed)")
    floors = []
    for c in pd.get("comments") or []:
        if not isinstance(c, dict) or not isinstance(c.get("floorIndex"), int):
            continue
        poster = c.get("poster") or {}
        floors.append(Floor(floor=c["floorIndex"], user=poster.get("name") or "[unknown]",
                            is_op=bool(poster.get("isOp")), markdown=c.get("markdown") or "",
                            date=parse_date((c.get("time") or {}).get("createdDate")),
                            upvotes=_num(c.get("upvoteCount")), chicken_legs=_num(c.get("likeCount")),
                            edited=_stamp((c.get("time") or {}).get("editedDate"))))
    return PostPage(id=_num(pd.get("postId"), pid), title=pd.get("title") or f"Post {pid}",
                    category=pd.get("category"), category_name=pd.get("categoryWord"),
                    page=_num(pd.get("postPage"), 1), pages=max(1, _num(pd.get("postPageCount"), 1)),
                    views=_num(pd.get("views")), locked=bool(pd.get("locked")),
                    op=(pd.get("op") or {}).get("name"), floors=floors)


def last_floor(page: PostPage) -> int:
    """The highest floor number that belongs on this page (hot repeats from other pages don't count;
    0 when the page holds only the opening post)."""
    return max((f.floor for f in page.floors if page_of(f.floor) == page.page), default=0)


def reply_target(markdown: str, pid: int, floor: int) -> tuple[int | None, str]:
    """(floor replied to, text) from a leading `@user [#N](…/post-<pid>-<page>#N)`. The link is
    replaced by `@user`; a reply to the opening post (#0) or to a later floor gets no marker."""
    m = _REPLY.match(markdown)
    if not m or int(m.group(3)) != pid:
        return None, markdown
    target = int(m.group(2))
    text = f"@{m.group(1)} " + markdown[m.end():]
    return (target if 0 < target < floor else None), text


def to_post(f: Floor, pid: int) -> Post:
    reply_to, text = reply_target(f.markdown, pid, f.floor) if f.floor else (None, f.markdown)
    text = text.strip() or "(no text)"
    if f.chicken_legs:
        text += f"\n[🍗{f.chicken_legs} chicken legs (加鸡腿, paid likes)]"
    return Post(f.floor, f.user, text, date=f.date, likes=f.upvotes, is_op=f.is_op, reply_to=reply_to,
                url=floor_url(pid, f.floor), edited=f.edited)
