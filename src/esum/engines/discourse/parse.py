"""Pure parsing for the Discourse engine: Discourse JSON in, `Post`s and plain dicts out.

Kept free of `ctx` so the offline fixture tests can call it directly. Shapes follow the public
Discourse JSON API (`/t/{id}.json`, `/t/{id}/posts.json`, `/search.json`, `/latest.json`,
`/site.json`), as captured from meta.discourse.org.
"""

from __future__ import annotations

import re
from html import escape, unescape
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from esum import Post, html_to_text, html_to_label, one_line, parse_date, element_end, element_inner, remove_elements, quote_html

LIKE_ACTION = 2          # actions_summary id of a like
HEART = "heart"          # the reactions plugin's like
SMALL_ACTION = 3         # post_type of "closed the topic", "split this topic", …
WHISPER = 4              # post_type of a staff-only whisper

SEARCH_ORDERS = ("relevance", "latest", "likes", "views", "latest_topic")


# --- posts -------------------------------------------------------------------------------------------

def likes_of(post: dict) -> int:
    """Likes on a post: the reactions plugin's heart count, else `like_count`, else the like action.

    With the reactions plugin, some forums (linux.do) count every reaction as a like action and in
    `reaction_users_count`, so neither is used when the post lists its reactions."""
    reactions = post.get("reactions")
    if isinstance(reactions, list) and reactions:
        return next((int(r.get("count") or 0) for r in reactions if r.get("id") == HEART), 0)
    if post.get("like_count") is not None:
        return int(post["like_count"])
    for a in post.get("actions_summary") or []:
        if a.get("id") == LIKE_ACTION and a.get("count"):
            return int(a["count"])
    return 0


def other_reactions(post: dict) -> int:
    """Reactions that aren't the heart. Likes plus these make up the forum's own like totals."""
    reactions = post.get("reactions")
    if not isinstance(reactions, list):
        return 0
    return sum(int(r.get("count") or 0) for r in reactions if r.get("id") != HEART)


def op_user_id(topic: dict) -> int | None:
    created_by = (topic.get("details") or {}).get("created_by") or {}
    return created_by.get("id") or topic.get("user_id")


_LIGHTBOX_META = re.compile(r'<div class="meta">.*?</div>', re.S)
_LIGHTBOX = re.compile(r'<a class="lightbox"[^>]*?href="(https?://[^"]+)"[^>]*>\s*(<img\b[^>]*>).*?</a>', re.S)
_IMAGE = re.compile(r"<img\b[^>]*>", re.I)


QUOTE_CHARS = 300       # a quoted selection keeps this much of its text
FULL_QUOTE_CHARS = 80   # a whole-post quote (`data-full`): that post is in the thread itself

_INNER_ASIDE = re.compile(r"<aside\b([^>]*)>((?:(?!<aside\b).)*?)</aside>", re.S | re.I)
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')
_HREF = re.compile(r'href="(https?://[^"]+)"')
_EMOJI = re.compile(r'<img\b[^>]*\bclass="emoji[^"]*"[^>]*>', re.I)


def _aside(m: re.Match, topic_id: int | None) -> str:
    """One innermost `<aside>`: a compact quote or a link preview URL."""
    attrs = dict(_ATTR.findall(m.group(1)))
    body = m.group(2)
    kinds = attrs.get("class", "").split()
    if "onebox" in kinds:
        url = attrs.get("data-onebox-src") or next(iter(_HREF.findall(body)), "")
        return f"<p>[link: {escape(unescape(url))}]</p>" if url else ""
    if "quote" not in kinds:
        return f"<div>{body}</div>"
    body = remove_elements(body, r"<blockquote data-esum-quote\b[^>]*>", "blockquote")
    inner = element_inner(body, r"<blockquote\b[^>]*>", "blockquote")
    if inner is not None:
        body = inner
    links = "".join(f"<p>[link: {escape(unescape(u))}]</p>" for u in dict.fromkeys(_HREF.findall(body)))
    cap = FULL_QUOTE_CHARS if attrs.get("data-full") == "true" else QUOTE_CHARS
    who = attrs.get("data-username") or ""   # a quote of a topic can carry its title here
    who = f"@{who}" if who and " " not in who else who
    where = f"#{attrs['data-post']}" if attrs.get("data-post") else ""
    if attrs.get("data-topic") and topic_id is not None and attrs["data-topic"] != str(topic_id):
        where = f"(topic {attrs['data-topic']} {where})".replace(" )", ")")
    return quote_html(who, where, body, cap=cap - 1) + links


def _image(tag: str, url: str | None = None) -> str:
    """`[image: URL]`: screenshots are often the evidence in a post. Discourse's default alt says nothing."""
    attrs = dict(_ATTR.findall(tag))
    url = unescape(url or attrs.get("src", ""))
    if not url.startswith("http"):
        return tag
    alt = attrs.get("alt", "")
    return f"[image: {escape(alt) + ' ' if alt and alt != 'image' else ''}{escape(url)}]"


def clean_cooked(html: str, topic_id: int | None = None) -> str:
    """Images become `[image: URL]` (the full-size link when there is one, without the lightbox's
    file name and size). An emoji becomes its `:name:`, because it can carry meaning: on uscf
    `5:brick:` is an amount. Quotes keep their body structure as `> @user #N: text…`, cut to QUOTE_CHARS
    (FULL_QUOTE_CHARS for a whole-post quote), followed by the links it quoted; a quote of another
    topic names it. A link preview (onebox) becomes `[link: URL]`."""
    html = _LIGHTBOX_META.sub("", html or "")
    html = _LIGHTBOX.sub(lambda m: _image(m.group(2), m.group(1)), html)
    html = _EMOJI.sub(lambda m: escape(dict(_ATTR.findall(m.group(0))).get("alt", "")), html)
    html = _IMAGE.sub(lambda m: _image(m.group(0)), html)
    while True:
        html, n = _INNER_ASIDE.subn(lambda m: _aside(m, topic_id), html)
        if not n:
            return html


_POLL_START = re.compile(r'<div class="poll"[^>]*>')
_POLL_OPTION = re.compile(r"<li\b[^>]*>(.*?)</li>", re.S)


def _poll_line(block: str, polls: list[dict]) -> str:
    """The cooked HTML always says `0 voters`; the post's `polls` data has the real count."""
    name = dict(_ATTR.findall(block[:block.index(">")])).get("data-poll-name")
    poll = next((p for p in polls if p.get("name") == (name or "poll")), None)
    if poll is None:
        options = " · ".join(html_to_label(o, keep_links=False) for o in _POLL_OPTION.findall(block))
        return f"[poll, votes not available: {options}]"
    options = " · ".join(f"{html_to_label(o.get('html') or '', keep_links=False)} {o.get('votes', 0)}"
                         for o in poll.get("options") or [])
    return f"[poll: {options} · {poll.get('voters', 0)} voters]"


def _with_polls(html: str, polls: list[dict]) -> str:
    while m := _POLL_START.search(html):
        end = element_end(html, m.start())
        html = f"{html[:m.start()]}<p>{escape(_poll_line(html[m.start():end], polls))}</p>{html[end:]}"
    return html


def post_text(post: dict) -> str:
    if post.get("post_type") == SMALL_ACTION and not (post.get("cooked") or "").strip():
        return f"[moderator action: {post.get('action_code') or 'small action'}]"
    cooked = _with_polls(post.get("cooked") or "", post.get("polls") or [])
    text = html_to_text(clean_cooked(cooked, post.get("topic_id"))) or post.get("raw") or ""
    if post.get("post_type") == WHISPER:
        text = f"[staff whisper] {text}"
    if post.get("hidden"):
        text = f"[hidden by the community] {text}"
    return text


def to_post(post: dict, *, base_url: str, topic_id: int, slug: str | None = None,
            op_id: int | None = None) -> Post:
    """One Discourse post as a compact `Post`. `op_id` is the topic creator's user id."""
    number = int(post["post_number"])
    is_op = bool(op_id is not None and post.get("user_id") == op_id)
    return Post(number=number, user=post.get("username") or "?", text=post_text(post),
                date=parse_date(post.get("created_at")), likes=likes_of(post), reactions=other_reactions(post),
                is_op=is_op,
                reply_to=post.get("reply_to_post_number") or None,
                url=post_url(base_url, topic_id, number, slug))


def post_url(base_url: str, topic_id: int, number: int | None = None, slug: str | None = None) -> str:
    url = f"{base_url}/t/{slug or '-'}/{topic_id}"
    return f"{url}/{number}" if number else url


def stream_posts(data: dict) -> list[dict]:
    """The posts of a `/t/{id}.json` or `/t/{id}/posts.json` response."""
    return list((data.get("post_stream") or {}).get("posts") or data.get("posts") or [])


def stream_ids(topic: dict) -> list[int] | None:
    """The visible post ids in thread order, or None for a megatopic (Discourse leaves `stream` out
    of topics with thousands of posts, which must then be paged by post number)."""
    ids = (topic.get("post_stream") or {}).get("stream")
    if not isinstance(ids, list) or not ids:
        return None
    seen: dict[int, None] = {}
    for i in ids:
        if isinstance(i, int) and i > 0:
            seen.setdefault(i, None)
    return list(seen)


SMALL_GAP = 10   # leaving out this few posts saves little and often cuts a conversation


def select_ids(ids: list[int], mode: str, head: int, tail: int) -> list[int]:
    """Stream positions to read: everything for `full`, the first `head` + last `tail` for `medium`."""
    if mode == "full" or len(ids) <= head + tail + SMALL_GAP:
        return list(ids)
    return ids[:head] + ids[-tail:]


def topic_meta_line(topic: dict, category: str | None = None) -> str:
    tags = ", ".join(t if isinstance(t, str) else (t or {}).get("name", "") for t in topic.get("tags") or [])
    status = "archived" if topic.get("archived") else "closed" if topic.get("closed") else "open"
    first = next((p for p in stream_posts(topic) if p.get("post_number") == 1), {})
    parts = [f"Category: {category}" if category else None,
             f"Tags: {tags}" if tags else None,
             "Wiki" if first.get("wiki") else None,
             f"Created: {_day(topic.get('created_at'))}",
             f"First post edited: {_day(first.get('updated_at'))}" if (first.get("version") or 1) > 1 else None,
             f"Last post: {_day(topic.get('last_posted_at'))}",
             f"Views: {topic.get('views')}" if topic.get("views") is not None else None,
             f"Likes: {topic.get('like_count')}" if topic.get("like_count") is not None else None,
             f"Status: {status}"]
    return " · ".join(p for p in parts if p)


def _day(value: str | None) -> str:
    d = parse_date(value)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d") if d else "?"


# --- topic lists and search ----------------------------------------------------------------------------

def topic_summary(t: dict, base_url: str, categories: dict[int, str] | None = None,
                  post_number: int | None = None, blurb: str | None = None) -> dict:
    """One topic of a list or search result, reduced to what an agent needs to choose what to read."""
    cat = (categories or {}).get(t.get("category_id")) if t.get("category_id") else None
    out = {"id": t["id"], "title": t.get("title") or t.get("fancy_title") or f"Topic {t['id']}",
           "url": post_url(base_url, t["id"], post_number, t.get("slug")),
           "category": cat, "category_id": t.get("category_id"),
           "posts_count": t.get("posts_count"), "like_count": t.get("like_count"),
           "views": t.get("views"), "created_at": t.get("created_at"),
           "last_posted_at": t.get("last_posted_at") or t.get("bumped_at"),
           "tags": [x if isinstance(x, str) else (x or {}).get("name") for x in t.get("tags") or []],
           "pinned": bool(t.get("pinned")), "closed": bool(t.get("closed"))}
    if post_number:
        out["post_number"] = post_number
    if blurb:
        out["blurb"] = " ".join(blurb.split())
    elif t.get("excerpt"):
        out["excerpt"] = html_to_label(t["excerpt"], keep_links=True)
    return out


def topic_list(data: dict, base_url: str, categories: dict[int, str] | None = None,
               include_pinned: bool = True) -> list[dict]:
    topics = (data.get("topic_list") or {}).get("topics") or []
    return [topic_summary(t, base_url, categories) for t in topics
            if t.get("archetype") != "private_message" and (include_pinned or not t.get("pinned"))]


def has_more(data: dict) -> bool:
    return bool((data.get("topic_list") or {}).get("more_topics_url"))


def search_results(data: dict, base_url: str, categories: dict[int, str] | None = None) -> list[dict]:
    """`/search.json` -> one entry per topic, linked to the first matching post with its blurb."""
    hit: dict[int, dict] = {}
    for p in data.get("posts") or []:
        hit.setdefault(p.get("topic_id"), p)
    out = []
    for t in data.get("topics") or []:
        p = hit.get(t["id"]) or {}
        entry = topic_summary(t, base_url, categories, post_number=p.get("post_number"),
                              blurb=p.get("blurb"))
        if p:
            entry["post_user"] = p.get("username")
            entry["post_date"] = p.get("created_at")
        out.append(entry)
    return out


def post_hits(data: dict, base_url: str) -> list[dict]:
    """`/search.json` inside one topic -> one row per matching post."""
    slugs = {t["id"]: t.get("slug") for t in data.get("topics") or []}
    return [{"post_number": p.get("post_number"), "date": utc_time(p.get("created_at")),
             "user": one_line(p.get("username") or ""), "likes": p.get("like_count"),
             "snippet": one_line(p.get("blurb") or ""),
             "url": post_url(base_url, p.get("topic_id"), p.get("post_number"), slugs.get(p.get("topic_id")))}
            for p in data.get("posts") or []]


def search_more(data: dict) -> bool:
    return bool((data.get("grouped_search_result") or {}).get("more_full_page_results"))


def search_term(query: str, *, category: str | None = None, order: str = "relevance",
                after: str | None = None, before: str | None = None, user: str | None = None,
                topic: int | None = None) -> str:
    """Discourse advanced-search syntax: `q category:12 topic:7 @user after:2026-01-01 order:latest`."""
    parts = [query.strip()]
    if category:
        parts.append(f"category:{category}")
    if topic:
        parts.append(f"topic:{topic}")
    if user:
        parts.append(f"@{user.lstrip('@')}")
    if after:
        parts.append(f"after:{after}")
    if before:
        parts.append(f"before:{before}")
    if order and order != "relevance":
        parts.append(f"order:{order}")
    return " ".join(p for p in parts if p)


def days_ago(days: int, today: date | None = None) -> str:
    today = today or datetime.now(timezone.utc).date()
    return (today - timedelta(days=days)).isoformat()


SNIPPET_CHARS = 300


def topic_row(t: dict, search: bool = False) -> dict:
    """A `topic_summary` as a list row: one-line text, UTC times, the snippet cut to 300 characters.

    Search rows add `post_number`, `post_user`, `date` (that post's time) and `snippet` (its blurb)."""
    row = {"id": t["id"], "title": one_line(t["title"]), "posts": t.get("posts_count"),
           "likes": t.get("like_count"), "views": t.get("views"),
           "last": utc_time(t.get("last_posted_at")), "created": utc_time(t.get("created_at")),
           "category": one_line(t["category"]) if t.get("category") else None,
           "category_id": t.get("category_id"), "tags": [one_line(x) for x in t.get("tags") or [] if x],
           "pinned": t.get("pinned", False), "closed": t.get("closed", False), "url": t["url"]}
    if search:
        row.update(post_number=t.get("post_number"), post_user=one_line(t.get("post_user") or "") or None,
                   date=utc_time(t.get("post_date")))
    snippet = one_line(t.get("blurb") or t.get("excerpt") or "")
    if snippet or search:
        row["snippet" if search else "excerpt"] = (f"{snippet[:SNIPPET_CHARS - 1]}…" if len(snippet) > SNIPPET_CHARS
                                                   else snippet)
    return row


def utc_time(value: str | None) -> datetime | None:
    """An API timestamp as a UTC datetime (list cells print it to the minute)."""
    d = parse_date(value)
    return d.astimezone(timezone.utc) if d else None


# --- posts across the forum, and one user's posts ------------------------------------------------------

def latest_post_rows(data: dict, base_url: str, categories: dict[int, str] | None = None) -> list[dict]:
    """`/posts.json` (newest first) -> rows. Small action posts ("closed the topic") and hidden or
    deleted posts are left out. `text` is the whole post as `thread` renders it (quotes as
    `> @user #N: …`, no emoji or image chrome); a list cell shows its start."""
    rows = []
    for p in data.get("latest_posts") or []:
        if p.get("post_type", 1) != 1 or p.get("hidden") or p.get("deleted_at"):
            continue
        rows.append({
            "id": p.get("id"), "date": utc_time(p.get("created_at")), "user": one_line(p.get("username") or ""),
            "topic": p.get("topic_id"), "number": p.get("post_number"),
            "title": one_line(p.get("topic_title") or ""), "reply_to": p.get("reply_to_post_number"),
            "category": (categories or {}).get(p.get("category_id")), "category_id": p.get("category_id"),
            "text": post_text(p),
            "url": post_url(base_url, p.get("topic_id"), p.get("post_number"), p.get("topic_slug")),
        })
    return rows


USER_ACTION_KINDS = {4: "topic", 5: "reply"}


def user_action_rows(data: dict, base_url: str, categories: dict[int, str] | None = None) -> list[dict]:
    """`/user_actions.json?filter=4,5` (newest first) -> rows: the user's new topics and replies.
    `excerpt` is Discourse's own short excerpt of the post."""
    rows = []
    for a in data.get("user_actions") or []:
        if a.get("deleted") or a.get("hidden"):
            continue
        rows.append({
            "date": utc_time(a.get("created_at")), "kind": USER_ACTION_KINDS.get(a.get("action_type"), "post"),
            "topic": a.get("topic_id"), "number": a.get("post_number"), "title": one_line(a.get("title") or ""),
            "reply_to": a.get("reply_to_post_number"), "category": (categories or {}).get(a.get("category_id")),
            "category_id": a.get("category_id"),
            "excerpt": html_to_label(clean_cooked(a.get("excerpt") or "", a.get("topic_id")), keep_links=False),
            "post_id": a.get("post_id"),
            "url": post_url(base_url, a.get("topic_id"), a.get("post_number"), a.get("slug")),
        })
    return rows


def user_profile(user: dict, summary: dict | None, base_url: str) -> dict:
    """`/u/{name}.json` (+ `/u/{name}/summary.json` when the profile shows it) -> the account facts
    that tell a person from a bot: age, last seen, trust level, reading time, visits, activity."""
    s = summary or {}

    def hours(seconds):
        return round(seconds / 3600, 1) if isinstance(seconds, (int, float)) else None

    name = user.get("username") or ""
    return {
        "username": one_line(name), "profile_hidden": bool(user.get("profile_hidden")),
        "name": one_line(user.get("name") or "") or None,
        "title": one_line(user.get("title") or "") or None,
        "created": utc_time(user.get("created_at")), "last_seen": utc_time(user.get("last_seen_at")),
        "last_posted": utc_time(user.get("last_posted_at")), "trust_level": user.get("trust_level"),
        "moderator": bool(user.get("moderator")), "admin": bool(user.get("admin")),
        "suspended_till": utc_time(user.get("suspended_till")), "silenced_till": utc_time(user.get("silenced_till")),
        "read_hours": hours(s.get("time_read", user.get("time_read"))),
        "read_hours_60d": hours(s.get("recent_time_read", user.get("recent_time_read"))),
        "days_visited": s.get("days_visited"), "posts_read": s.get("posts_read_count"),
        "topics_entered": s.get("topics_entered"), "topics": s.get("topic_count"),
        "posts": s.get("post_count", user.get("post_count")),
        "likes_given": s.get("likes_given"), "likes_received": s.get("likes_received"),
        "badges": user.get("badge_count"), "profile_views": user.get("profile_view_count"),
        "top_categories": [one_line(c.get("name") or "") for c in s.get("top_categories") or [] if c.get("name")],
        "url": f"{base_url}/u/{name}",
    }


# --- categories ----------------------------------------------------------------------------------------

def categories_from_site(data: dict) -> list[dict]:
    """`/site.json` categories as `{id, name, slug, parent_id, path, restricted}`, parents first."""
    raw = data.get("categories") or []
    by_id = {c["id"]: c for c in raw if "id" in c}
    out = []
    for c in raw:
        parent = by_id.get(c.get("parent_category_id"))
        path = f"{parent['name']} / {c['name']}" if parent else c["name"]
        order = ((parent or c).get("position") or 0) * 1000 + ((c.get("position") or 0) + 1 if parent else 0)
        out.append({"id": c["id"], "name": c["name"], "slug": c.get("slug") or "",
                    "parent_id": c.get("parent_category_id"), "path": path,
                    "restricted": bool(c.get("read_restricted")), "topic_count": c.get("topic_count"),
                    "_order": order})
    out.sort(key=lambda c: c.pop("_order"))
    return out


def category_names(categories: Iterable[dict]) -> dict[int, str]:
    return {c["id"]: c.get("path") or c["name"] for c in categories}


def find_category(categories: list[dict], wanted: str) -> dict | None:
    """By id, exact name, path ("Parent / Child") or slug; case-insensitive."""
    w = wanted.strip()
    if re.fullmatch(r"\d+", w):
        return next((c for c in categories if c["id"] == int(w)), None)
    low = w.lower()
    for key in ("name", "path", "slug"):
        for c in categories:
            if (c.get(key) or "").lower() == low:
                return c
    return None


# --- errors --------------------------------------------------------------------------------------------

def error_type(body: Any) -> str | None:
    """Discourse's `{"errors": [...], "error_type": "not_logged_in"}`."""
    return body.get("error_type") if isinstance(body, dict) else None
