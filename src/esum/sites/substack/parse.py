"""Pure parsers for Substack's publication API (`/api/v1/archive`, `/posts/<slug>`, `/post/<id>/comments`).

Every publication is its own origin: `<name>.substack.com` or a custom domain. A thread is the post
(post #1) followed by its comments in time order (#2, #3, …), like Hacker News, so numbers stay
stable as comments arrive; `↩#M` names the comment a reply answers. Paid posts come back as a
preview unless the session has access, and are marked as such.
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from esum import (ArgumentError, Post, html_to_text, one_line, parse_date)

DELETED = "[deleted]"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
IMAGE_LINK = re.compile(r'<a\b[^>]*class="image-link[^"]*"[^>]*>.*?</a>', re.S)
EMBED = re.compile(r'<div\b[^>]*class="(twitter-embed|youtube-wrap)"[^>]*>', re.S)


def _image(m: re.Match) -> str:
    link = m.group(0)
    href = re.search(r'href="([^"]+)"', link)
    alt = re.search(r'<img\b[^>]*\balt="([^"]*)"', link)
    shown = " ".join(part for part in (alt.group(1) if alt else "", href.group(1) if href else "") if part)
    return f"<p>[image: {shown}]</p>" if shown else ""


def _embed(m: re.Match) -> str:
    """The text of an embed is only in `data-attrs`: the div itself is empty."""
    found = re.search(r'data-attrs="([^"]*)"', m.group(0))
    try:
        data = json.loads(html.unescape(found.group(1))) if found else {}
    except ValueError:
        data = {}
    if m.group(1) == "youtube-wrap":
        return f'{m.group(0)}<p>[video: https://youtu.be/{html.escape(str(data.get("videoId") or ""))}]</p>'
    quote = f'@{data.get("username") or "?"}: {" ".join(str(data.get("full_text") or "").split())} ({data.get("url") or ""})'
    return f"{m.group(0)}<blockquote>{html.escape(quote)}</blockquote>"


def embeds(body: str) -> str:
    """Images, tweets and videos as text an agent can follow."""
    return EMBED.sub(_embed, IMAGE_LINK.sub(_image, body))


def _stamp(value: str | None) -> str | None:
    when = parse_date(value)
    return f"{when:%Y-%m-%d %H:%M}" if when else None


def num(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


READER_HOSTS = {"substack.com", "www.substack.com"}   # the reader app and profiles, not a publication
SHARE = re.compile(r"/p-(\d+)(?:[/?#]|$)")              # app share links: /home/post/p-<id>, /@<handle>/p-<id>
NOT_A_PUBLICATION = ("Pass the publication's name (noahpinion), its domain, a URL on it, or an author's "
                     "substack.com/@handle")


@dataclass(frozen=True)
class Ref:
    """What a publication or post argument names, before any request: a publication's origin, an
    author's profile handle (resolved to their own publication), or a share link's post id."""

    origin: str | None = None
    handle: str | None = None
    post_id: int | None = None


def _split(value: str):
    return urlsplit(value if "://" in value else f"https://{value}")


def publication_ref(publication: str) -> Ref:
    """`noahpinion`, `@noahpinion`, a domain, or any URL on a publication, an `open.substack.com/pub/<name>`
    link, or an author's `substack.com/@handle` profile."""
    value = publication.strip()
    if value.startswith("@"):
        return Ref(handle=value[1:].strip("/"))
    if "://" not in value and NAME.match(value.lower()):
        return Ref(origin=f"https://{value.lower()}.substack.com")
    parts = _split(value)
    host = (parts.hostname or "").lower()
    if not host:
        raise ArgumentError(f"not a Substack publication: {publication!r}", help=NOT_A_PUBLICATION)
    if host == "open.substack.com" and (m := re.match(r"^/pub/([^/?#]+)", parts.path)):
        return Ref(origin=f"https://{m.group(1).lower()}.substack.com")
    if host in READER_HOSTS:
        if m := re.match(r"^/@([^/?#]+)", parts.path):
            return Ref(handle=m.group(1))
        raise ArgumentError(f"{publication!r} is a page of Substack's reader app, not a publication",
                            help=NOT_A_PUBLICATION)
    return Ref(origin=f"https://{host}")


def publication_origin(publication: str) -> str:
    """A publication argument that names its site directly (no profile lookup) → its origin."""
    ref = publication_ref(publication)
    if ref.origin is None:
        raise ArgumentError(f"{publication!r} names an author's profile, not a publication", help=NOT_A_PUBLICATION)
    return ref.origin


def post_ref(post: str, publication: str | None) -> tuple[Ref, str | None]:
    """A post URL (`…/p/<slug>`, `open.substack.com/pub/<name>/p/<slug>`), an app share link
    (`…/p-<id>`: the slug comes from Substack) or a bare slug plus `--publication` → (publication, slug)."""
    value = post.strip()
    if "://" in value or "/" in value.strip("/"):
        parts = _split(value)
        if m := SHARE.search(parts.path):
            return Ref(post_id=int(m.group(1))), None
        path = re.sub(r"^/pub/[^/]+", "", parts.path) if (parts.hostname or "").lower() == "open.substack.com" else parts.path
        if not (m := re.match(r"^/p/([^/?#]+)", path)):
            raise ArgumentError(f"not a Substack post URL (expected …/p/<slug> or a share link …/p-<id>): {post!r}",
                                help="Pass a post URL such as https://example.substack.com/p/<slug>")
        return publication_ref(value), m.group(1)
    if not publication:
        raise ArgumentError(f"{post!r} is a bare slug, and no --publication names its site",
                            help="Pass the whole post URL, or add --publication <name or domain>")
    return publication_ref(publication), value.strip("/")


def split_post(post: str, publication: str | None) -> tuple[str, str]:
    """`post_ref` for arguments that name the post's site directly → (origin, slug)."""
    ref, slug = post_ref(post, publication)
    if ref.origin is None or slug is None:
        raise ArgumentError(f"{post!r} needs a lookup on Substack to find its site", help="Pass the post's URL")
    return ref.origin, slug


def check_cursor(cursor: str, pages_by: str) -> None:
    """Refuse a `--cursor` Substack didn't issue for this listing before sending it (the API only says
    HTTP 400). Cursors are base64 JSON: `{"post_date": …}` for the newest-first archive, `{"offset": …}`
    for `--sort top` and searches."""
    try:
        decoded = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), validate=True))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise ArgumentError(f"--cursor {cursor!r} is not a cursor Substack issued",
                            help="Copy --cursor from a next-page command, or leave it out to start at the first page") from None
    if not isinstance(decoded, dict) or pages_by not in decoded:
        raise ArgumentError("this --cursor came from a different listing (another --sort, or archive vs search)",
                            help="Run the command again without --cursor, then follow its own next-page command")


def is_preview(p: dict) -> bool:
    """A paid post whose body this session can't read in full."""
    paid = p.get("audience") not in (None, "everyone", "public")
    return paid and not (p.get("body_html") and p.get("is_paywalled") is False)


def bylines(p: dict) -> str | None:
    names = [b.get("name") for b in p.get("publishedBylines") or p.get("published_bylines") or [] if b.get("name")]
    return ", ".join(names) or None


@dataclass
class Article:
    """One row of an archive page or a search result."""

    id: int
    slug: str
    title: str
    subtitle: str
    author: str | None
    date: datetime | None
    url: str
    paid: bool
    comments: int
    likes: int
    words: int

    def to_dict(self) -> dict:
        return {"id": self.id, "slug": self.slug, "title": self.title, "subtitle": self.subtitle,
                "author": self.author, "date": self.date.isoformat() if self.date else None,
                "url": self.url, "paid": self.paid, "comments": self.comments, "likes": self.likes,
                "words": self.words}


def article_from(p: dict, origin: str) -> Article:
    return Article(id=num(p.get("id")), slug=p.get("slug") or "", title=p.get("title") or "",
                   subtitle=p.get("subtitle") or p.get("description") or "", author=bylines(p),
                   date=parse_date(p.get("post_date")),
                   url=p.get("canonical_url") or f"{origin}/p/{p.get('slug')}",
                   paid=p.get("audience") not in (None, "everyone", "public"),
                   comments=num(p.get("comment_count")), likes=num(p.get("reaction_count")),
                   words=num(p.get("wordcount")))


def article_row(a: Article) -> dict:
    """A list row: `to_dict` with one-line fields."""
    row = a.to_dict()
    row.update(title=one_line(a.title), subtitle=one_line(a.subtitle), author=one_line(a.author) or None,
               date=f"{a.date:%Y-%m-%d %H:%M}" if a.date else None)
    return row


@dataclass
class Thread:
    id: int
    title: str
    url: str
    preview: bool              # paid post: post #1 holds only the free preview
    posts: list[Post]          # every post, numbered 1..n


def thread_from(post: dict, comments: list[dict], origin: str) -> Thread:
    """A post (`/api/v1/posts/<slug>`) and its comment tree (`/api/v1/post/<id>/comments`).

    Deleted comments without replies are dropped; ones with replies stay as `[deleted]` so the
    replies' `↩` still points somewhere.
    """
    p = post.get("post", post)
    url = p.get("canonical_url") or f"{origin}/p/{p.get('slug')}"
    flat: list[tuple[dict, int | None]] = []   # (comment, parent comment id)

    def walk(items: list[dict], parent: int | None) -> None:
        for c in items or []:
            kids = c.get("children") or []
            if c.get("body") or kids:
                flat.append((c, parent))
            walk(kids, num(c.get("id")))

    walk(comments, None)
    flat.sort(key=lambda cp: (cp[0].get("date") or "", num(cp[0].get("id"))))
    number = {num(c.get("id")): i for i, (c, _) in enumerate(flat, 2)}

    preview = is_preview(p)
    body = (html_to_text(embeds(p.get("body_html") or ""))
            or (p.get("truncated_body_text") or "").strip())
    hidden = num(p.get("comment_count")) - len(flat)
    notes = ["[paid post: only the free preview is shown]" if preview else "",
             f"[{hidden} comments on the site are not readable from this session (paid or hidden)]"
             if hidden > 0 and not flat else ""]
    head = "\n".join(t for t in (p.get("subtitle") or "", *notes) if t)
    author = bylines(p) or "?"
    posts = [Post(1, author, "\n\n".join(t for t in (head, body) if t) or "(no text)",
                  date=parse_date(p.get("post_date")), likes=num(p.get("reaction_count")), is_op=True, url=url)]
    for c, parent in flat:
        name = c.get("name") or c.get("handle") or DELETED
        posts.append(Post(number[num(c.get("id"))], name if c.get("body") else DELETED,
                          ("[user banned for this comment]\n" if c.get("user_banned_for_comment") else "")
                          + ((c.get("body") or "").strip() or DELETED), date=parse_date(c.get("date")),
                          likes=num(c.get("reaction_count")),
                          is_op=bool(c.get("body")) and name == author,
                          reply_to=number.get(parent) if parent else None,
                          url=f"{origin}/p/{p.get('slug')}/comment/{c.get('id')}", edited=_stamp(c.get("edited_at"))))
    return Thread(id=num(p.get("id")), title=p.get("title") or url, url=url, preview=preview, posts=posts)
