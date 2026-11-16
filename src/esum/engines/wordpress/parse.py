"""Pure parsing for the WordPress engine: WP REST JSON (posts, comments, categories) and the Disqus
embed page in, `Post`s and plain dicts out. No `ctx`, no network, so fixtures test it directly.

Numbering follows the userscript's list threads: the article is #0 and comments are #1… in time
order, so new comments only ever append and `↩#M` keeps who answered whom.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

from esum import (ArgumentError, Post, html_to_text, html_to_label, one_line, parse_since)

COMMENTS_PER_PAGE = 100   # WP REST maximum
ARTICLE_CHAR_CAP = 8000   # medium mode: the article usually carries the substance, but cap it
DAY = timedelta(days=1)


def wp_date(value: str | None) -> datetime | None:
    """WP `*_gmt` fields (and Disqus `createdAt`) are UTC without a zone: `2026-10-25T15:50:59`."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rendered_html(field_value) -> str:
    """`{"rendered": "<p>…</p>"}` (or a bare string) -> HTML."""
    if isinstance(field_value, dict):
        field_value = field_value.get("rendered", "")
    return field_value or ""


def rendered(field_value, keep_links: bool = False) -> str:
    """A rendered WP body, keeping block structure."""
    return html_to_text(_rendered_html(field_value), keep_links=keep_links)


def rendered_label(field_value) -> str:
    """A rendered WP title or category name, without anchor URLs."""
    return html_to_label(_rendered_html(field_value), keep_links=False)


def clip(text: str, cap: int | None) -> str:
    if cap is None or len(text) <= cap:
        return text
    return text[:cap].rstrip() + f"… [cut at {cap} of {len(text)} chars; --from 0 --to 0 shows all of it]"


# --- listings ----------------------------------------------------------------------------------------

def _excerpt(post: dict, cap: int = 240) -> str:
    html = (post.get("excerpt") or {}).get("rendered", "")
    # USCCG appends a "Read more" button inside the excerpt
    html = re.sub(r'<a[^>]*class="[^"]*read-more[^"]*"[^>]*>.*?</a>', "", html, flags=re.S | re.I)
    text = html_to_label(html, keep_links=False)
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def parse_post_list(items: list[dict]) -> list[dict]:
    """WP `/posts` summaries -> list rows {id, date (YYYY-MM-DD), title, url, excerpt, categories[, lang]}."""
    out = []
    for p in items or []:
        date = wp_date(p.get("date_gmt"))
        out.append({
            "id": p.get("id"),
            "date": date.strftime("%Y-%m-%d") if date else None,
            "title": rendered_label(p.get("title")) or f"Post {p.get('id')}",
            "url": p.get("link", ""),
            "excerpt": one_line(_excerpt(p)),
            "categories": p.get("categories") or [],
            **({"lang": p["lang"]} if p.get("lang") else {}),
        })
    return out


def parse_categories(items: list[dict]) -> list[dict]:
    return [{"id": c.get("id"), "name": rendered_label(c.get("name")) or one_line(c.get("slug", "")),
             "slug": one_line(c.get("slug", "")), "count": c.get("count", 0), "parent": c.get("parent", 0)}
            for c in items or []]


# --- since / ids -------------------------------------------------------------------------------------

def wp_after(dt: datetime) -> str:
    """WP REST `after=` value (UTC, explicit offset so the site's timezone setting doesn't apply)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def post_ref(value: str) -> tuple[str, str]:
    """A post id, slug or URL -> ("id", "123") or ("slug", "some-post")."""
    value = value.strip()
    if value.isdigit():
        return "id", value
    if value.startswith(("http://", "https://")):
        parts = urlsplit(value)
        if m := re.search(r"(?:^|&)p=(\d+)", parts.query):
            return "id", m.group(1)
        segs = [s for s in parts.path.split("/") if s]
        if not segs:
            raise ArgumentError(f"no post slug in URL {value!r}", help="Pass the article's own URL, not a list or home page")
        return "slug", segs[-1]
    if re.fullmatch(r"[\w%-]+", value):
        return "slug", value
    raise ArgumentError(f"{value!r} is not a post id, slug or URL",
                        help="Pass the post id, its slug, or the article URL")


# --- comments ----------------------------------------------------------------------------------------

@dataclass
class Comment:
    id: str
    user: str
    when: datetime | None
    text: str
    parent: str | None = None
    likes: int = 0
    staff: bool = False      # a registered blog account (WP) or one of the site's op_aliases
    url: str | None = None
    number: int = 0          # filled by number_comments


def parse_wp_comments(items: list[dict], post_url: str = "", op_aliases: tuple[str, ...] = (),
                      start: int = 1) -> list[Comment]:
    """One page of WP `/comments` (oldest first) -> Comments numbered by position from `start`, so a
    page fetched alone still gets its thread numbers. On these blogs readers comment anonymously
    (`author` 0), so a comment from a registered account (`author` ≠ 0) is the blog's own staff."""
    out = []
    for i, c in enumerate(items or []):
        user = (c.get("author_name") or "").strip()
        text = drop_comment_links(rendered(c.get("content"), keep_links=True), post_url)
        if not user and not text:
            continue
        cid = str(c.get("id"))
        out.append(Comment(
            id=cid, user=user or "[anonymous]", when=wp_date(c.get("date_gmt")), text=text,
            parent=str(c["parent"]) if c.get("parent") else None,
            staff=bool(c.get("author")) or user in op_aliases,
            url=f"{post_url}#comment-{cid}" if post_url else None, number=start + i,
        ))
    return out


def drop_comment_links(text: str, post_url: str) -> str:
    """Remove ` (<post>#comment-N)` after a name: wpDiscuz links a mention to that comment on this
    page, which says nothing the reader can follow (`↩#M` already shows whom a reply answers). A
    long link may have lost its `#comment-N` already, so a bare link to the post goes too."""
    if not post_url:
        return text
    return re.sub(r" \(" + re.escape(post_url.split("#", 1)[0]) + r"(#comment-\d+)?\)", "", text)


_THREAD_DATA = re.compile(r'<script[^>]*id="disqus-threadData"[^>]*>(.*?)</script>', re.S)


_KEY_SCRIPT = re.compile(r"""\.src\s*=\s*['"]((?:https:)?//[^'"]+/embedv2[^'"]*\.js)['"]""")
_API_KEY = re.compile(r'VITE_API_KEY\s*:\s*"([A-Za-z0-9]{20,})"')


@dataclass
class DisqusPage:
    total: int
    comments: list[Comment] = field(default_factory=list)
    has_next: bool = False
    thread_id: str | None = None
    key_script_url: str | None = None


def disqus_comment(p: dict, post_url: str = "", op_aliases: tuple[str, ...] = ()) -> Comment | None:
    """One Disqus post (the embed's threadData and the API's `posts/list` share the shape)."""
    if p.get("isDeleted") or p.get("isSpam"):
        return None
    author = p.get("author") or {}
    user = (author.get("name") or author.get("username") or "").strip() or "[guest]"
    cid = str(p.get("id"))
    return Comment(
        id=cid, user=user, when=wp_date(p.get("createdAt")),
        text=html_to_text(p.get("message") or "", keep_links=True),
        parent=str(p["parent"]) if p.get("parent") else None,
        likes=max(0, int(p.get("likes") or 0)),
        staff=user in op_aliases or (author.get("username") or "") in op_aliases,
        url=f"{post_url}#comment-{cid}" if post_url else None,
    )


def parse_disqus_embed(html: str, post_url: str = "", op_aliases: tuple[str, ...] = ()) -> DisqusPage:
    """The Disqus embed page (`disqus.com/embed/comments/?f=…&t_i=…`) carries its first page of
    comments (up to 50 threads, with their replies) as JSON in `#disqus-threadData`."""
    m = _THREAD_DATA.search(html or "")
    if not m:
        raise RuntimeError("Disqus embed page has no disqus-threadData block (layout changed?)")
    data = json.loads(m.group(1))
    resp = data.get("response") or {}
    thread = resp.get("thread") or {}
    cursor = data.get("cursor") or {}
    out = [c for c in (disqus_comment(p, post_url, op_aliases) for p in resp.get("posts") or []) if c]
    total = int(thread.get("posts") or cursor.get("total") or len(out))
    script = _KEY_SCRIPT.search(html)
    url = script.group(1) if script else None
    return DisqusPage(total=total, comments=out, has_next=bool(cursor.get("hasNext")),
                      thread_id=str(thread["id"]) if thread.get("id") else None,
                      key_script_url=("https:" + url if url and url.startswith("//") else url))


def disqus_api_key(bundle_js: str) -> str | None:
    """The public API key Disqus ships to every visitor in its embed script (`VITE_API_KEY`). It
    identifies the embed, not an account; ESUM reads it per call and never stores it."""
    m = _API_KEY.search(bundle_js or "")
    return m.group(1) if m else None


def parse_disqus_posts(data: dict, post_url: str = "", op_aliases: tuple[str, ...] = ()
                       ) -> tuple[list[Comment], str | None]:
    """One `posts/list` page -> (comments, next cursor or None)."""
    if not isinstance(data, dict) or not isinstance(data.get("response"), list):
        raise ValueError(f"Disqus API answered {str(data)[:200]}")
    cursor = data.get("cursor") or {}
    out = [c for c in (disqus_comment(p, post_url, op_aliases) for p in data["response"]) if c]
    return out, (cursor.get("next") if cursor.get("hasNext") else None)


def disqus_config(page_html: str) -> tuple[str, str] | None:
    """(shortname, identifier) from the WordPress Disqus plugin's `embedVars` on a post page."""
    short = re.search(r'"disqusShortname"\s*:\s*"([^"]+)"', page_html or "")
    ident = re.search(r'"disqusIdentifier"\s*:\s*"((?:[^"\\]|\\.)*)"', page_html or "")
    if not short or not ident:
        return None
    return short.group(1), json.loads(f'"{ident.group(1)}"')


def by_time(comments: list[Comment]) -> list[Comment]:
    far = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(comments, key=lambda c: (c.when or far, int(c.id) if c.id.isdigit() else 0))


# --- the thread --------------------------------------------------------------------------------------

@dataclass
class ThreadMeta:
    published: datetime | None
    republished: bool
    first_comment: datetime | None
    updated: datetime | None
    closed: bool


def thread_meta(post: dict, comments: list[Comment]) -> ThreadMeta:
    """DoC (and USCCG's evergreen reviews) re-date a post when they update it: comments older than
    the post date mean it was republished, so the article is dated by its first comment instead."""
    ts = wp_date(post.get("date_gmt"))
    mod = wp_date(post.get("modified_gmt"))
    first = min((c.when for c in comments if c.when), default=None)
    bumped = bool(ts and first and first < ts)
    return ThreadMeta(
        published=ts, republished=bumped, first_comment=first if bumped else None,
        updated=mod if ts and mod and mod - ts > DAY else None,
        closed=post.get("comment_status") == "closed",
    )


def meta_line(meta: ThreadMeta, comments_from: str = "") -> str:
    def d(x: datetime) -> str:
        return x.strftime("%Y-%m-%d")
    bits = []
    if meta.published:
        bits.append(f"{'Republished' if meta.republished else 'Published'}: {d(meta.published)}")
    if meta.first_comment:
        bits.append(f"First comment: {d(meta.first_comment)}")
    if meta.updated:
        bits.append(f"Updated: {d(meta.updated)}")
    if meta.closed:
        bits.append("Comments: closed")
    if comments_from:
        bits.append(f"Comments from {comments_from}")
    return " · ".join(bits)


def article_post(post: dict, op_user: str, meta: ThreadMeta, cap: int | None) -> Post:
    when = meta.first_comment if meta.republished else meta.published
    return Post(number=0, user=op_user, text=clip(rendered(post.get("content"), keep_links=True), cap),
                date=when, is_op=True, url=post.get("link"))


def to_posts(numbered: list[Comment], number_of: dict[str, int]) -> list[Post]:
    """Comments that already carry their thread `number` -> Posts (`↩#M` when the parent is known
    and is not the article)."""
    return [Post(number=c.number, user=c.user, text=c.text, date=c.when, likes=c.likes, is_op=c.staff,
                 reply_to=number_of.get(c.parent) if c.parent else None, url=c.url)
            for c in numbered]


def number_comments(comments: list[Comment], start: int = 1) -> dict[str, int]:
    """Number comments in time order from `start`; returns {comment id: number}."""
    numbers = {}
    for i, c in enumerate(by_time(comments)):
        c.number = start + i
        numbers[c.id] = c.number
    return numbers


# --- linked sources (ported from the userscript's link selection) -------------------------------------

LINKS_MAX = 5
_SKIP_HOST = re.compile(
    r"(^|\.)(twitter|x|youtube|youtu|instagram|facebook|tiktok|linkedin|t|imgur|giphy|bit|tinyurl|discord"
    r"|telegram|wechat|weixin|gravatar|disqus|disq)\.(com|be|ly|me|gg|co|us)$|^(apply|go|refer)\.", re.I)
_SKIP_PATH = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|mp4|mov|webm|mp3|pdf|zip|exe|dmg)(\?|$)"
    r"|/(u|users?|members?|profile|tags?|login|signup|register|search|author|wp-content|wp-admin"
    r"|contact(-us)?|about(-us)?)/"
    r"|refer|referral|affiliate|invite|[?&](ref|aff|affid|referrer|code|promo)="
    r"|/r/[a-z0-9]{4,}$|/(go|out|recommends|apply)/", re.I)
_URL = re.compile(r"\((https?://[^\s()]+)\)|(?:^|\s)(https?://[^\s()<>]+)")


def pick_links(posts: list[Post], base: str, limit: int = LINKS_MAX) -> list[dict]:
    """The pages the posts link to most, weighted by how often a link appears, whether the article
    or first comment has it, and the likes of the posts citing it. Referral/affiliate-looking
    links, profiles, media and social sites are skipped; the post's own URL is too."""
    here = _norm(base)
    score: dict[str, dict] = {}
    for p in posts:
        for m in _URL.finditer(p.text or ""):
            raw = (m.group(1) or m.group(2) or "").rstrip(".,;:!?。，；：！？」』)")
            if not raw or raw.endswith("…"):
                continue  # shortened: the real target is unknown
            url = _norm(urljoin(base, raw))
            if not url or url == here:
                continue
            host = urlsplit(url).hostname or ""
            parts = urlsplit(url)
            if _SKIP_HOST.search(host) or _SKIP_PATH.search(parts.path + ("?" + parts.query if parts.query else "")):
                continue
            e = score.setdefault(url, {"url": url, "score": 0.0, "cited_by": []})
            e["score"] += 1 + (2 if p.number <= 1 else 0) + min(3, (p.likes or 0) / 5)
            ref = f"[{p.user} #{p.number}]"
            if len(e["cited_by"]) < 3 and ref not in e["cited_by"]:
                e["cited_by"].append(ref)
    ranked = sorted(score.values(), key=lambda e: -e["score"])[:limit]
    return [{"url": e["url"], "cited_by": e["cited_by"]} for e in ranked]


def _norm(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):
        return ""
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, parts.query, ""))


def render_links(links: list[dict]) -> str:
    if not links:
        return ""
    lines = ["\n\nLinked sources (external pages, untrusted; fetch only if needed):"]
    lines += [f"- {one_line(link['url'])} — cited by {', '.join(map(one_line, link['cited_by']))}"
              for link in links]
    return "\n".join(lines)
