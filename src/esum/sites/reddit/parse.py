"""Pure parsers for Reddit's JSON API (`*.json` listings, `comments/<id>.json`, `api/morechildren`)
and for the HTML pages a guest can read (feeds, search, comment trees, embeds, profiles, wiki pages).
The feed, search, comment and embed parsers return the same `t3` / `t1` data as the API, so one
thread builder serves both.

A thread is the post (post #1) followed by its comments in time order (#2, #3, …), as for Hacker
News: numbers stay stable as comments arrive and `↩#M` names the comment a reply answers.

Reddit never returns a whole big comment tree. Branches it leaves out arrive as `more` stubs: a
list of comment ids to fetch with `api/morechildren` (at most 100 per request), or a "continue this
thread" stub (no ids) for branches nested too deep. `CommentTree` gathers comments across those
requests and keeps track of what is still missing.
"""

from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from esum import (Post, html_to_text, html_to_label, inner_div, one_line, parse_date)

SITE = "https://www.reddit.com"
MORE_BATCH = 100  # api/morechildren accepts at most 100 ids per request


def when(created_utc: float | None) -> datetime | None:
    return datetime.fromtimestamp(created_utc, tz=timezone.utc) if created_utc else None


def strip_kind(fullname: str | None) -> str:
    """`t3_abc` / `t1_abc` -> `abc`."""
    return (fullname or "").split("_", 1)[-1]


@dataclass
class Link:
    """A post as it appears in a listing or search result."""

    id: str
    title: str
    subreddit: str
    author: str
    date: datetime | None
    score: int
    comments: int
    permalink: str
    url: str | None            # the linked page; None for text (self) posts
    flair: str | None = None
    text: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "subreddit": self.subreddit,
                "author": self.author, "date": self.date.isoformat() if self.date else None,
                "score": self.score, "comments": self.comments, "permalink": self.permalink,
                "url": self.url, "flair": self.flair, "text": self.text}


def site_url(url: str | None) -> str | None:
    """A crosspost's link is a path on Reddit, in the API and on the page."""
    return SITE + url if url and url.startswith("/") else url


def _origin(data: dict) -> dict:
    """The post a crosspost copies; {} for any other post."""
    return (data.get("crosspost_parent_list") or [{}])[0]


def link_from(data: dict) -> Link:
    is_self = data.get("is_self", False)
    return Link(id=data["id"], title=data.get("title") or "",
                subreddit=data.get("subreddit_name_prefixed") or f"r/{data.get('subreddit', '?')}",
                author=data.get("author") or "[deleted]", date=when(data.get("created_utc")),
                score=data.get("score") or 0, comments=data.get("num_comments") or 0,
                permalink=SITE + (data.get("permalink") or f"/comments/{data['id']}/"),
                url=None if is_self else site_url(data.get("url_overridden_by_dest") or data.get("url")),
                flair=data.get("link_flair_text") or None,
                text=data.get("selftext") or _origin(data).get("selftext") or "")


def links_from_listing(listing: dict) -> tuple[list[Link], str | None]:
    """A Listing of posts -> (posts, the `after` cursor for the next page)."""
    data = listing.get("data") or {}
    links = [link_from(c["data"]) for c in data.get("children", []) if c.get("kind") == "t3"]
    return links, data.get("after")


def link_row(link: Link) -> dict:
    """A list row: `to_dict` with one-line names (the table shortens dates and text)."""
    row = link.to_dict()
    row.update(title=one_line(link.title), subreddit=one_line(link.subreddit), author=one_line(link.author),
               flair=one_line(link.flair) or None)
    return row


@dataclass
class CommentTree:
    """Comments gathered from `comments/<id>.json` and any number of `api/morechildren` calls."""

    comments: dict[str, dict] = field(default_factory=dict)   # id -> t1 data
    pending: list[str] = field(default_factory=list)          # ids from `more` stubs, not fetched
    deep: int = 0          # "continue this thread" stubs: branches too deep for this API call
    stubs: list[tuple[str, str]] = field(default_factory=list)   # a guest's (path, cursor) "load more" calls

    def add(self, things: list[dict] | None) -> None:
        """Add a list of `t1` / `more` things, walking nested replies."""
        for thing in things or []:
            kind, data = thing.get("kind"), thing.get("data") or {}
            if kind == "t1":
                if data["id"] == "deleted":   # Reddit's id for every comment it no longer has an id for
                    data = {**data, "id": f"deleted-{len(self.comments)}"}
                self.comments.setdefault(data["id"], data)
                self.pending = [i for i in self.pending if i != data["id"]]
                replies = data.get("replies")
                if isinstance(replies, dict):
                    self.add((replies.get("data") or {}).get("children"))
            elif kind == "more":
                ids = [i for i in data.get("children") or [] if i not in self.comments]
                if ids:
                    self.pending.extend(i for i in ids if i not in self.pending)
                elif data.get("id") == "_" or data.get("count", 0) == 0:
                    self.deep += 1

    def next_batch(self) -> list[str]:
        return self.pending[:MORE_BATCH]

    def drop(self, ids: list[str]) -> None:
        """Stop waiting for ids Reddit did not return (deleted since, or not expandable)."""
        gone = set(ids)
        self.pending = [i for i in self.pending if i not in gone]


def more_things(response: dict) -> list[dict]:
    """The things in an `api/morechildren.json?api_type=json` response."""
    body = response.get("json") or {}
    if body.get("errors"):
        raise RuntimeError(f"reddit morechildren errors: {body['errors']}")
    return (body.get("data") or {}).get("things") or []


@dataclass
class Thread:
    link: Link
    locked: bool
    archived: bool
    posts: list[Post]          # every loaded post, numbered 1..n
    pending: int               # comment ids in `more` stubs that were not fetched
    deep: int                  # "continue this thread" stubs not followed
    stubs: int = 0             # a guest's "load more" calls not made
    stickied: list[int] = field(default_factory=list)   # numbers of the comments pinned to the top

    @property
    def title(self) -> str:
        return f"{self.link.title} ({self.link.subreddit})"

    @property
    def total(self) -> int:
        """The post plus every comment Reddit counts (including deleted and unloaded ones)."""
        return max(self.link.comments + 1, len(self.posts))


def post_data(listing: dict) -> dict:
    children = (listing.get("data") or {}).get("children") or []
    if not children or children[0].get("kind") != "t3":
        raise RuntimeError("reddit: post not found in the response (the site data may have changed)")
    return children[0]["data"]


def _minute(stamp: datetime) -> str:
    return stamp.strftime("%Y-%m-%d %H:%M")


def _edited(thing: dict) -> str | None:
    """The API's `edited` is False or a time; `True` on old posts names no time."""
    stamp = thing.get("edited")
    return _minute(when(stamp)) if stamp and stamp is not True else None


def _marks(thing: dict) -> list[str]:
    who = thing.get("distinguished")
    return [f"Distinguished: {who.lower()}" if who else "", "Status: stickied" if thing.get("stickied") else ""]


def _gallery_lines(post: dict) -> list[str]:
    source = post if post.get("gallery_data") else _origin(post)
    media = source.get("media_metadata") or {}
    lines = []
    for item in (source.get("gallery_data") or {}).get("items") or []:
        full = (media.get(item.get("media_id")) or {}).get("s") or {}
        if url := full.get("u") or full.get("gif"):
            lines.append(f"[image: {url}]" + (f" {item['caption']}" if item.get("caption") else ""))
    return lines


def _poll_lines(post: dict) -> list[str]:
    poll = post.get("poll_data") or _origin(post).get("poll_data")
    if not poll:
        return []
    end = when((poll.get("voting_end_timestamp") or 0) / 1000)
    closed = poll.get("closed") or (end is not None and end <= datetime.now(timezone.utc))
    total = poll.get("total_vote_count")
    head = [f"Poll: {total} votes" if total is not None else "Poll"]
    if closed:
        head.append(f"closed {_minute(end)}" if end else "closed")
    elif end:
        head.append(f"closes {_minute(end)}")
    options = [f"- {o.get('text')}" + (f": {o['vote_count']}" if o.get("vote_count") is not None else "")
               for o in poll.get("options") or []]
    return [" · ".join(head), *options]


def build_thread(post: dict, tree: CommentTree) -> Thread:
    link = link_from(post)
    # no time means none Reddit will vouch for, so a tombstone can't lead the thread
    comments = sorted(tree.comments.values(), key=lambda c: (c.get("created_utc") or float("inf"), c["id"]))
    number = {link.id: 1}
    for i, c in enumerate(comments, 2):
        number[c["id"]] = i
    origin = site_url(_origin(post).get("permalink"))
    op_text = "\n\n".join(t for t in (f"Link: {link.url}" if link.url and link.url != origin else "",
                                       f"Crossposted from: {origin}" if origin else "",
                                       "\n".join(_gallery_lines(post)), "\n".join(_poll_lines(post)), link.text) if t)
    bits = [b for b in (f"Flair: {link.flair}" if link.flair else "", *_marks(post),
                        "Status: locked" if post.get("locked") else "",
                        "Status: archived" if post.get("archived") else "") if b]
    if bits:
        op_text = " · ".join(bits) + ("\n\n" + op_text if op_text else "")
    posts = [Post(1, link.author, op_text or "(no text)", date=link.date,
                  likes=None if post.get("hide_score") else post.get("score"),
                  is_op=True, url=link.permalink, edited=_edited(post))]
    for c in comments:
        parent = number.get(strip_kind(c.get("parent_id")))
        author = c.get("author") or "[deleted]"
        text = c.get("body") or "[deleted]"
        if marks := [m for m in _marks(c) if m]:
            text = " · ".join(marks) + "\n" + text
        posts.append(Post(number[c["id"]], author, text,
                          date=when(c.get("created_utc")), likes=None if c.get("score_hidden") else c.get("score"),
                          is_op=bool(c.get("is_submitter")) or (author == link.author and author != "[deleted]"),
                          reply_to=parent if parent and parent != 1 else None,
                          url=SITE + c["permalink"] if c.get("permalink") else None, edited=_edited(c)))
    return Thread(link=link, locked=bool(post.get("locked")), archived=bool(post.get("archived")),
                  posts=posts, pending=len(tree.pending), deep=tree.deep, stubs=len(tree.stubs),
                  stickied=[number[c["id"]] for c in comments if c.get("stickied")])


def missing_note(thread: Thread, *, refusal: str | None = None) -> str:
    """One line on comments Reddit did not return, or "" when the tree is complete."""
    parts = []
    if thread.stubs:
        parts.append(f"{thread.stubs} 'load more' stubs not opened (rerun with a higher --expand to open them)")
    if thread.pending:
        reason = f"Reddit refused loading more: {refusal}" if refusal else "rerun with a higher --expand to fetch them"
        parts.append(f"{thread.pending} comments still behind 'load more' stubs ({reason})")
    if thread.deep:
        parts.append(f"{thread.deep} deeply nested branches not followed "
                     f"(open that comment's permalink to read them)")
    return f"[... not loaded: {'; '.join(parts)} …]" if parts else ""


# --- HTML pages -------------------------------------------------------------------------------------

_TIME = re.compile(r'<faceplate-timeago\b[^>]*\bts="([^"]+)"')


def is_check_page(html: str) -> bool:
    """Reddit's JavaScript check: HTTP 200 and a hidden form. Every normal page has the words
    `js_challenge` in its scripts, so the form's field is what tells them apart."""
    return 'name="js_challenge"' in html


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\s{name}="([^"]*)"', tag)
    return htmllib.unescape(m.group(1)) if m else None


def _iso(stamp: str | None) -> str | None:
    when = parse_date(stamp)
    return when.isoformat() if when else None


def _absolute_links(html: str) -> str:
    """Point a page's relative links at Reddit. A `r/name` or `u/name` link's text already says
    where it goes, so it stays bare instead of repeating itself in every comment."""
    def fix(m: re.Match) -> str:
        if re.fullmatch(r"/?[ru]/[\w-]+/?", m.group(3).strip()):
            return m.group(0)
        return f"{m.group(1)}{SITE}{m.group(2)}{m.group(3)}"
    return re.sub(r'(<a\b[^>]*\bhref=")(/(?!/)[^>]*>)([^<]*)', fix, html)


def _body(html: str, marker: str) -> str:
    """The text of the `<div>` whose id contains `marker`."""
    m = re.search(rf'<div\b[^>]*\bid="[^"]*{marker}[^"]*"', html)
    return html_to_text(_absolute_links(inner_div(html, m.start()))) if m else ""


def _stamp(value: str | None) -> float | None:
    when = parse_date(value)
    return when.timestamp() if when else None


def _flair(card: str) -> str | None:
    m = re.search(r"<shreddit-post-flair\b.*?</shreddit-post-flair>", card, re.S)
    return html_to_label(m.group(0), keep_links=True) or None if m else None


def _is_self(url: str | None) -> bool:
    """A text or gallery post links to itself; the API calls those self posts or has no outside url."""
    return not url or url.startswith(SITE)


def _flag(tag: str, name: str) -> bool:
    return bool(re.search(rf"\s{name}(\s|>)", re.sub(r'="[^"]*"', "", tag)))


def _gallery(html: str, title: str) -> dict:
    """A `<gallery-carousel>` in the API's `gallery_data` / `media_metadata` shape. An image's caption
    is its alt text, which is the post's title when it has none. A post's page and a feed send the
    full image; the embed page only a preview."""
    start = html.find("<gallery-carousel")
    if start < 0:
        return {}
    items, media = [], {}
    for li in re.findall(r"<li\b.*?</li>", html[start:html.find("</gallery-carousel>", start)], re.S):
        urls = re.findall(r'\b(?:src|data-lazy-src)="(https://(?:i|preview)\.redd\.it/[^"]+)"', li)
        if not urls:
            continue
        alt = re.search(r'\salt="([^"]+)"', li)
        caption = re.sub(r"^r/\w+ - ", "", htmllib.unescape(alt.group(1))) if alt else ""
        key = str(len(items))
        media[key] = {"s": {"u": htmllib.unescape(next((u for u in urls if "//i.redd.it/" in u), urls[0]))}}
        items.append({"media_id": key, "caption": "" if caption == title else caption})
    return {"gallery_data": {"items": items}, "media_metadata": media}


def _poll(card: str) -> dict | None:
    """A `<shreddit-post-poll>` in the API's `poll_data` shape. An open poll shows a guest its options
    and no counts; a closed one shows the counts and not when it closed."""
    m = re.search(r"<shreddit-post-poll\b.*?</shreddit-post-poll>", card, re.S)
    if not m:
        return None
    poll = m.group(0)
    total = re.search(r'<faceplate-number\b[^>]*\bnumber="(\d+)"', poll)
    ends = re.search(r'Closes\s*<faceplate-timeago\b[^>]*\bts="([^"]+)"', poll)
    counted = re.findall(r'Option text on top.*?<span\b[^>]*>(.*?)</span>.*?<faceplate-number\b[^>]*\bnumber="(\d+)"',
                         poll, re.S)
    options = [{"text": text, "vote_count": int(count)} for text, count in counted]
    choices = re.findall(r"<faceplate-radio-input\b[^>]*>(.*?)</faceplate-radio-input>", poll, re.S)
    options += [{"text": text} for text in choices]
    stamp = _stamp(ends.group(1)) if ends else None
    return {"total_vote_count": int(total.group(1)) if total else None, "closed": not choices,
            "voting_end_timestamp": stamp * 1000 if stamp else None,
            "options": [{**o, "text": html_to_label(o["text"], keep_links=False)} for o in options]}


def _crosspost(tag: str, card: str, href: str | None) -> tuple[str | None, list[dict]]:
    """The post's link and the API's `crosspost_parent_list`. A crosspost's `content-href` is the
    original post; a page that post links to is the outbound link in the card."""
    if not _flag(tag, "is-crosspost"):
        return href, []
    media = card.split('-post-rtjson-content"')[0]   # a text crosspost's own links are not its link
    outbound = re.search(r'<a\b[^>]*\bhref="(https?://[^"]+)"[^>]*\brel="noopener nofollow ugc"', media)
    return htmllib.unescape(outbound.group(1)) if outbound else href, [{"permalink": href}]


def post_cards(html: str) -> list[dict]:
    """The `<shreddit-post>` cards of a feed or a post's page, in the API's `t3` shape."""
    out = []
    for m in re.finditer(r"<shreddit-post\s[^>]*>", html):
        tag, card = m.group(0), html[m.end():html.find("</shreddit-post>", m.end())]
        thing = _attr(tag, "id")
        url, origin = _crosspost(tag, card, _attr(tag, "content-href"))
        out.append({"id": strip_kind(thing), "title": _attr(tag, "post-title") or "",
                    "subreddit_name_prefixed": _attr(tag, "subreddit-prefixed-name"), "author": _attr(tag, "author"),
                    "created_utc": _stamp(_attr(tag, "created-timestamp")), "score": int(_attr(tag, "score") or 0),
                    "num_comments": int(_attr(tag, "comment-count") or 0), "permalink": _attr(tag, "permalink"),
                    "is_self": _is_self(url), "url": url, "link_flair_text": _flair(card),
                    "locked": _flag(tag, "locked"), "archived": _flag(tag, "archived"), "stickied": _flag(tag, "stickied"),
                    "crosspost_parent_list": origin, "poll_data": _poll(card), **_gallery(card, _attr(tag, "post-title")),
                    "selftext": _body(card, f"{thing}-post-rtjson-content")})
    return out


def feed_has_more(html: str) -> bool:
    return bool(re.search(r'\bmore-posts-cursor="[^"]+"', html))


_SEARCH_POST = re.compile(r'<search-telemetry-tracker\b[^>]*data-faceplate-tracking-context="([^"]*)"[^>]*'
                          r'click-events="search/click/post"')


def _without_origins(html: str) -> str:
    """A crosspost's result holds the original's whole card, marks and counters too, and then its own
    counters. The original isn't a result."""
    kept, pos = [], 0
    for unit in re.finditer(r'data-testid="search-crosspost-unit"', html):
        end = html.find("<hr", unit.end())
        end = len(html) if end < 0 else end
        marks = [(m.start(), json.loads(htmllib.unescape(m.group(1)))["post"]["id"])
                 for m in _SEARCH_POST.finditer(html, unit.end(), end)]
        origin = next((start for start, thing in marks if thing != marks[0][1]), None)
        counters = html.rfind('data-testid="search-counter-row"', unit.end(), end)
        if origin is not None and counters > origin:
            kept.append(html[pos:origin])
            pos = counters
    return "".join(kept) + html[pos:]


def search_links(html: str) -> list[Link]:
    """The post results of a search page. A result has no post text (its snippet can be a comment),
    and not every result names its author: that stays empty, which is not `[deleted]`."""
    html = _without_origins(html)
    marks = [(m, json.loads(htmllib.unescape(m.group(1)))) for m in _SEARCH_POST.finditer(html)]
    out: dict[str, Link] = {}
    for i, (m, meta) in enumerate(marks):
        thing = meta["post"]["id"]
        if thing in out:
            continue
        stop = next((n.start() for n, other in marks[i + 1:] if other["post"]["id"] != thing), len(html))
        card = html[m.end():stop]
        link = re.search(r'data-testid="post-title"[^>]*\bhref="([^"]+)"', card)
        when = _TIME.search(card)
        counts = re.findall(r'<faceplate-number\b[^>]*\bnumber="(-?\d+)"', card[card.find("search-counter-row"):])
        out[thing] = link_from({
            "id": strip_kind(thing), "title": htmllib.unescape(meta["post"].get("title") or ""),
            "subreddit_name_prefixed": f"r/{(meta.get('subreddit') or {}).get('name', '?')}",
            "created_utc": _stamp(when.group(1)) if when else None,
            "score": int(counts[0]) if counts else 0, "num_comments": int(counts[1]) if len(counts) > 1 else 0,
            "permalink": htmllib.unescape(link.group(1)) if link else None, "is_self": True})
        out[thing].author = (meta.get("profile") or {}).get("name") or ""
    return list(out.values())


def comment_tree(html: str) -> dict | None:
    """The post's permalink and comment count from a comment page; None when there is no such post."""
    m = re.search(r"<shreddit-comment-tree\s[^>]*>", html)
    if not m:
        return None
    return {"permalink": _attr(m.group(0), "permalink"), "total": int(_attr(m.group(0), "totalComments") or 0)}


def _tombstone(card: str) -> str:
    """What Reddit prints in place of a comment it no longer shows."""
    m = re.search(r"<p\b[^>]*>(.*?)</p>", card[card.find("</summary>"):], re.S)
    return html_to_text(m.group(1), keep_links=False).strip() if m else ""


def comment_things(html: str) -> list[dict]:
    """The `<shreddit-comment>` cards of a comment page, as the API's `t1` things."""
    things = []
    cards = list(re.finditer(r"<shreddit-comment\s[^>]*>", html))
    for m, after in zip(cards, [*cards[1:], None]):
        tag = m.group(0)
        card = html[m.end():after.start() if after else len(html)]   # stops at its first reply
        thing = _attr(tag, "thingId")
        score = _attr(tag, "score")
        badges = re.search(r"<shreddit-comment-badges\b[^>]*>", card)
        edited = re.search(r'>\s*Edited\s*<faceplate-timeago\b[^>]*\bts="([^"]+)"', card)
        things.append({"kind": "t1", "data": {
            "id": strip_kind(thing), "author": _attr(tag, "author"), "parent_id": _attr(tag, "parentId"),
            # every `t1_deleted` row carries one date, from before the post itself
            "created_utc": None if thing == "t1_deleted" else _stamp(_attr(tag, "created")),
            "score": int(score) if score else None, "permalink": _attr(tag, "permalink"),
            "score_hidden": bool(re.search(r"<shreddit-comment-action-row\b[^>]*\sis-score-hidden", card)),
            "is_submitter": _flag(tag, "is-op"), "edited": _stamp(edited.group(1)) if edited else False,
            "distinguished": _attr(badges.group(0), "distinguished-as") if badges else None,
            "stickied": _flag(badges.group(0), "stickied") if badges else False,
            "body": _body(card, rf"{thing}-(?:comment|post)-rtjson-content") or _tombstone(card)}})
    return things


_STUB = re.compile(r'<faceplate-partial\b[^>]*\bsrc="(/svc/shreddit/more-comments/[^"]+)"[^>]*>\s*'
                   r'<input type="hidden" name="cursor" value="([^"]+)"')


def comment_stubs(html: str) -> list[tuple[str, str]]:
    """The page's "load more" calls as (path, cursor)."""
    return [(htmllib.unescape(path), htmllib.unescape(cursor)) for path, cursor in _STUB.findall(html)]


def embed_post(html: str, tree: dict) -> dict:
    """A post's embed page in the API's `t3` shape. An embed without the post (removed, or not
    embeddable) still gives the id, title words and count that the comment page has."""
    permalink = tree["permalink"]
    parts = permalink.strip("/").split("/")
    base = {"id": parts[3], "title": parts[4].replace("_", " "), "subreddit_name_prefixed": f"r/{parts[1]}",
            "author": "?", "permalink": permalink, "num_comments": tree["total"], "is_self": True}
    data = re.search(r'<shreddit-screenview-data data="([^"]*)"', html)
    post = (json.loads(htmllib.unescape(data.group(1))).get("post") or {}) if data else {}
    if not post.get("id"):
        return {**base, "selftext": "[post text not available: the embed page doesn't show it]"}
    title = re.search(r"<(shreddit-embed-title|h[12])\b[^>]*>(.*?)</\1>", html, re.S)
    author = re.search(r'href="https://www\.reddit\.com/user/([^/"?]+)', html)
    score = re.search(r'<faceplate-number\b[^>]*\bnumber="(-?\d+)"', html)
    created = post.get("created_timestamp")
    text = _body(html, f"{post['id']}-post-rtjson-content")
    if post.get("type") == "poll":
        text = "[poll not available: the embed page doesn't show it]\n\n" + text
    found = {**base, "title": html_to_label(title.group(2), keep_links=False) if title else base["title"],
             "author": author.group(1) if author else None, "score": int(score.group(1)) if score else None,
             "created_utc": created / 1000 if created else None, "is_self": _is_self(post.get("url")),
             "url": post.get("url"), "selftext": text}
    return {**found, **_gallery(html, found["title"])}


def _strip_context(path: str) -> str:
    return SITE + path.split("?")[0]


def search_comments(html: str) -> list[dict]:
    """The comment cards of a search page. Each carries its ids as JSON in a tracking attribute."""
    rows = []
    for card in html.split('data-testid="search-sdui-comment-unit"')[1:]:
        meta = json.loads(htmllib.unescape(re.search(r'data-faceplate-tracking-context="([^"]*)"', card).group(1)))
        start = re.search(r'\bid="search-comment-[^"]*-post-rtjson-content"', card).start()
        user = re.search(r'data-id="user-hover-card"[^>]*\blabel="([^"]*) details"', card)
        when = _TIME.search(card)
        score = re.compile(r'<faceplate-number\b[^>]*\bnumber="(-?\d+)"').search(card, start)
        link = re.search(r'aria-labelledby="comment-content-[^"]*"[^>]*\bhref="([^"]+)"', card)
        rows.append({"id": strip_kind(meta["comment"]["id"]), "post_id": strip_kind(meta["post"]["id"]),
                     "post": one_line(htmllib.unescape(meta["post"].get("title") or "")),
                     "subreddit": (meta.get("subreddit") or {}).get("name"),
                     "author": htmllib.unescape(user.group(1)) if user else None,
                     "date": _iso(when.group(1)) if when else None, "score": int(score.group(1)) if score else None,
                     "text": _body(card, "search-comment-"), "url": _strip_context(link.group(1)) if link else None})
    return rows


def search_cursor(html: str) -> str | None:
    m = re.search(r'src="([^"]*[?&(amp;)]+cursor=[^"]*)"', html)
    return parse_qs(urlsplit(htmllib.unescape(m.group(1))).query)["cursor"][0] if m else None


def no_search_hits(html: str) -> bool:
    return "find any results" in html


def profile_comments(html: str, name: str) -> list[dict]:
    rows = []
    for card in re.findall(r"<shreddit-profile-comment\b.*?</shreddit-profile-comment>", html, re.S):
        head = card[:card.index(">")]
        href = _attr(head, "href") or ""
        post = re.search(r"/comments/([a-z0-9]+)", href)
        place = re.search(r'data-testid="location-anchor"[^>]*\bhref="/r/([^/"]+)', card)
        about = re.search(r'aria-label="[^"]*? comment on ([^"]*)"', card)
        when = _TIME.search(card)
        score = re.search(r'<shreddit-comment-action-row\b[^>]*\bscore="(-?\d+)"', card)
        rows.append({"id": strip_kind(_attr(head, "comment-id")), "post_id": post.group(1) if post else None,
                     "post": one_line(htmllib.unescape(about.group(1))) if about else None,
                     "subreddit": place.group(1) if place else None, "author": name,
                     "date": _iso(when.group(1)) if when else None, "score": int(score.group(1)) if score else None,
                     "text": _body(card, "-post-rtjson-content"), "url": _strip_context(href) if href else None})
    return rows


def profile_posts(html: str) -> list[dict]:
    rows = []
    for tag in re.findall(r"<shreddit-post\s[^>]*>", html):
        rows.append({"id": strip_kind(_attr(tag, "id")), "title": one_line(_attr(tag, "post-title") or ""),
                     "subreddit": _attr(tag, "subreddit-prefixed-name"), "author": _attr(tag, "author"),
                     "date": _iso(_attr(tag, "created-timestamp")), "score": int(_attr(tag, "score") or 0),
                     "comments": int(_attr(tag, "comment-count") or 0), "url": _strip_context(_attr(tag, "permalink") or ""),
                     "link": site_url(_attr(tag, "content-href"))})
    return rows


def profile_cursor(html: str) -> str | None:
    m = re.search(r'<shreddit-feed-load-more-observer\b[^>]*\bcursor="([^"]+)"', html)
    return htmllib.unescape(m.group(1)) if m else None


def no_such_user(html: str) -> bool:
    return "nobody on Reddit goes by that name" in html


def no_such_community(html: str) -> bool:
    """A name Reddit has no community for. A guest sees a "Mature Content" gate there, as on some real
    subreddits, so the only sign is the "We couldn't find that community" dialog the page loads."""
    return "community-not-found-modal" in html


def community_block(html: str) -> str | None:
    """What a subreddit's page says instead of showing it: banned, private, premium or quarantined."""
    start = html.find("<guard-community-modal")
    modal = html[start:html.find("</guard-community-modal>", start)] if start >= 0 else ""
    title = re.search(r'<div slot="title"[^>]*>(.*?)</div>', modal, re.S)
    if not title:
        return None
    said = html_to_label(title.group(1), keep_links=False).rstrip(".")
    text = re.search(r"<p\b[^>]*>(.*?)</p>", modal[title.end():], re.S)
    if not text:
        return said
    # a private subreddit's moderators write what comes before the last line, and error lines carry
    # no untrusted label
    last = re.split(r"<br\s*/?>", text.group(1))[-1]
    return f"{said}: {html_to_label(last, keep_links=False)}"


@dataclass
class WikiPage:
    text: str
    revised_by: str | None
    revised_at: str | None


def wiki_page(html: str) -> WikiPage | None:
    """The page's own text, without the subreddit sidebar. None when Reddit shows no such page."""
    start = html.find('<div class="md wiki">')
    if start < 0:
        return None
    body = _absolute_links(inner_div(html, start))
    revised = re.search(r'Last revised by\s*<a\b[^>]*>([^<]*)</a>\s*<faceplate-timeago\b[^>]*\bts="([^"]+)"', html)
    return WikiPage(html_to_text(body), htmllib.unescape(revised.group(1)) if revised else None,
                    _iso(revised.group(2)) if revised else None)


# --- recent comments, duplicates and wiki revisions ---------------------------------------------------

def comment_row(data: dict) -> dict:
    """A `t1` from a JSON listing (`comments.json`) as a list row, in the shape of `search_comments`."""
    return {"id": data["id"], "post_id": strip_kind(data.get("link_id")),
            "post": one_line(data.get("link_title") or "") or None, "subreddit": data.get("subreddit"),
            "author": data.get("author") or "[deleted]", "date": _iso_utc(data.get("created_utc")),
            "score": None if data.get("score_hidden") else data.get("score"), "text": data.get("body") or "",
            "url": SITE + data["permalink"] if data.get("permalink") else None}


def _iso_utc(created_utc: float | None) -> str | None:
    stamp = when(created_utc)
    return stamp.isoformat() if stamp else None


def _old_things(html: str, kind: str) -> list[tuple[str, str]]:
    """The `thing` cards of an old-Reddit page (`comment` or `link`): (opening tag, the card)."""
    cards = []
    for part in re.split(r'(?=<div class=" thing\b)', html)[1:]:
        tag = part[:part.index(">") + 1]
        if _attr(tag, "data-type") == kind:
            cards.append((tag, part))
    return cards


def old_comments(html: str) -> list[dict]:
    """The comments of the old-Reddit page a guest gets for `/r/<sub>/comments/`."""
    rows = []
    for tag, card in _old_things(html, "comment"):
        permalink = _attr(tag, "data-permalink") or ""
        post = re.search(r"/comments/([a-z0-9]+)", permalink)
        title = re.search(r'<a\b[^>]*\bclass="title"[^>]*>(.*?)</a>', card, re.S)
        stamp = re.search(r'<time\b[^>]*\bclass="live-timestamp"[^>]*>', card)
        score = re.search(r'<span class="score unvoted" title="(-?\d+)"', card)   # hidden: no such span
        body = card.find('<div class="md">')
        rows.append({"id": strip_kind(_attr(tag, "data-fullname")), "post_id": post.group(1) if post else None,
                     "post": one_line(html_to_label(title.group(1), keep_links=False)) if title else None,
                     "subreddit": _attr(tag, "data-subreddit"), "author": _attr(tag, "data-author") or "[deleted]",
                     "date": _iso(_attr(stamp.group(0), "datetime")) if stamp else None,
                     "score": int(score.group(1)) if score else None,
                     "text": html_to_text(_absolute_links(inner_div(card, body))) if body >= 0 else "",
                     "url": SITE + permalink if permalink else None})
    return rows


def old_links(html: str) -> list[dict]:
    """The posts of an old-Reddit page (`/duplicates/<id>/`), as `t3` data for `link_from`."""
    posts = []
    for tag, card in _old_things(html, "link"):
        title = re.search(r'<a\b[^>]*\bclass="title\b[^"]*"[^>]*>(.*?)</a>', card, re.S)
        flair = re.search(r'<span class="linkflairlabel[^"]*"[^>]*>(.*?)</span>', card, re.S)
        url = site_url(_attr(tag, "data-url"))
        stamp = _attr(tag, "data-timestamp")
        posts.append({"id": strip_kind(_attr(tag, "data-fullname")),
                      "title": html_to_label(title.group(1), keep_links=False) if title else "",
                      "subreddit_name_prefixed": _attr(tag, "data-subreddit-prefixed"),
                      "author": _attr(tag, "data-author"),
                      "created_utc": int(stamp) / 1000 if stamp else None,
                      "score": int(_attr(tag, "data-score") or 0), "num_comments": int(_attr(tag, "data-comments-count") or 0),
                      "permalink": _attr(tag, "data-permalink"), "url": url, "is_self": _is_self(url),
                      "link_flair_text": html_to_label(flair.group(1), keep_links=False) if flair else None})
    return posts


def old_cursor(html: str) -> str | None:
    """The `after` of an old-Reddit page's "next" button; None on the last page."""
    m = re.search(r'<span class="next-button"><a href="([^"]+)"', html)
    return (parse_qs(urlsplit(htmllib.unescape(m.group(1))).query).get("after") or [None])[0] if m else None


def no_such_page(html: str) -> bool:
    """Old Reddit's 404 page (a post id it has no post for)."""
    return "the page you requested does not exist" in html


def revision_row(subreddit: str, page: str, revision_id: str, author: str | None, stamp: str | None,
                 reason: str | None) -> dict:
    rid = revision_id.removeprefix("WikiRevision_")
    return {"id": rid, "page": page, "author": author or "[deleted]", "date": stamp, "reason": reason or None,
            "url": f"{SITE}/r/{subreddit}/wiki/{page}?v={rid}"}


def api_revisions(subreddit: str, page: str, listing: dict) -> tuple[list[dict], str | None]:
    """`wiki/revisions/<page>.json` -> (rows, the `after` cursor)."""
    data = listing.get("data") or {}
    rows = [revision_row(subreddit, r.get("page") or page, r["id"], ((r.get("author") or {}).get("data") or {}).get("name"),
                         _iso_utc(r.get("timestamp")), r.get("reason"))
            for r in data.get("children") or []]
    return rows, data.get("after")


def page_revisions(subreddit: str, page: str, html: str) -> list[dict]:
    """The revisions table a guest sees on `/r/<sub>/wiki/revisions/<page>/` (the latest few)."""
    rows = []
    for row in re.findall(r"<tr\b[^>]*>.*?</tr>", html, re.S):
        box = re.search(r"<wiki-version-compare-checkbox\b[^>]*>", row)
        if not box:
            continue
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S)
        reason = html_to_label(cells[1], keep_links=False) if len(cells) > 1 else ""
        rows.append(revision_row(subreddit, page, _attr(box.group(0), "revision-id") or "",
                                 _attr(box.group(0), "revision-author"),
                                 _iso(_attr(box.group(0), "revision-timestamp")), reason))
    return rows
