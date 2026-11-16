"""Discourse engine: search, topic lists, categories, full-coverage thread reads and the bookmark write.

Any Discourse forum is a site with just a base URL:

    forum = site(name="meta", engine="discourse", base_url="https://meta.discourse.org",
                 label="Discourse Meta: Discourse's own support forum")

Thread reads cover every visible post. Normal topics are read by post id from the topic's
`post_stream.stream` (50 ids per request, the first ~20 posts arrive with `/t/{id}.json`).
Megatopics (thousands of posts) have no `stream`, so they and every `--from/--to` range are paged
by post number with `/t/{id}/posts.json?post_number=N&asc=true|false` (20 posts per page, sequential
because deleted posts leave gaps). Gaps in the numbering are marked, as "not read" or as "deleted or
hidden".

Bookmarks need the user's logged-in session. Reads use the site's declared transport; the bookmark
uses `extras["write_transport"]` if set, else T2 for a T1 site, else the site's own transport. A T2
write always borrows the browser's cookies, even when the site reads without them.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import quote

from esum import (NoSuchItem, ArgumentError, HttpError, Listing, LoginNeeded, Mode, Next, Result, Tier, Write, action,
                  chunked, engine, one_line, parse_since, thread_view, before_span)

from . import auth, parse
from .account import account_for

FORMAT = 1
PORTED_FROM = ("xgoose skills/discourse-forum.source.xgs.js (github.com/xGoose-dev/xgoose); "
               "nitan_plugin My.user.js (Discourse adapter: megathreads, post-number ranges); "
               "nitan-mcp src/tools/builtin/search.ts, src/http/client.ts (Cloudflare challenge heuristics), "
               "src/user-api-key-generator.ts (User API Key login)")

EXTRAS = ("categories", "write_transport", "guest_search")  # site extras the actions read, besides SITE_EXTRAS
discourse = engine("discourse", description="Discourse forums: search, topic lists, full threads, bookmarks")

HEAD = 30                 # medium mode: first posts …
TAIL = 30                 # … and last posts
IDS_PER_REQUEST = 50      # post ids per /t/{id}/posts.json call
MAX_POSTS = 1000          # posts one thread call reads; bigger reads go range by range
RECENT_DAYS = 60
MAX_LIST_POSTS = 10000    # posts one `posts` / `user-posts` call reads (uscf: about 6,000 a day)
SEARCH_PAGES = 10         # Discourse refuses search pages past this
MAX_PICKED_POSTS = 30     # posts one `thread --posts` reads, one request each

Order = Literal["relevance", "latest", "likes", "views", "latest_topic"]
Period = Literal["daily", "weekly", "monthly", "quarterly", "yearly", "all"]


# --- fetching ------------------------------------------------------------------------------------------

def categories(ctx) -> list[dict]:
    """The site's categories: `extras["categories"]` when the site ships a static map, else /site.json."""
    static = ctx.site.extras.get("categories")
    if static:
        by_id = {c["id"]: c for c in static}
        out = []
        for c in static:
            parent = by_id.get(c.get("parent_id"))
            out.append({"slug": "", "parent_id": None, "restricted": False, "topic_count": None, **c,
                        "path": f"{parent['name']} / {c['name']}" if parent else c["name"]})
        return out
    return parse.categories_from_site(account_for(ctx).fetch_json("/site.json"))


def static_category_names(ctx) -> dict[int, str]:
    """Category names without a request (only sites with a static map have them)."""
    return parse.category_names(categories(ctx)) if ctx.site.extras.get("categories") else {}


def resolve_category(ctx, wanted: str) -> dict:
    if wanted.strip().isdigit() and not ctx.site.extras.get("categories"):
        return {"id": int(wanted), "name": wanted, "path": f"category {wanted}"}
    available = categories(ctx)
    if not available:
        raise RuntimeError(f"{ctx.site.name}: no categories found (the site data may have changed)")
    found = parse.find_category(available, wanted)
    if not found:
        raise ArgumentError(f"unknown category {wanted!r} on {ctx.site.name}",
                            help=f"Run `esum {ctx.site.name} categories` and use an id, a name or a slug")
    return found


# --- reads ---------------------------------------------------------------------------------------------

READ_TOPIC = Next("thread", {"id": "<id>"}, "read a topic")


def _search_day(value: str | None, flag: str) -> str | None:
    """A search date: YYYY-MM-DD, an ISO time or a relative 7d / 2w -> the YYYY-MM-DD Discourse takes."""
    if not value:
        return None
    if value.strip()[-1:].lower() in ("h", "m"):
        raise ArgumentError(f"{flag} {value!r}: search filters by whole days",
                            help="use a date or a relative 1d; `posts --since 12h` reads by the minute")
    if re.fullmatch(r"0+\s*[dw]", value.strip(), re.I):   # by whole days, 0d is today, not now
        return f"{datetime.now(timezone.utc):%Y-%m-%d}"
    try:
        return f"{parse_since(value):%Y-%m-%d}"
    except ArgumentError as e:
        raise ArgumentError(str(e).replace("--since", flag), help=e.help) from None


def _listing(ctx, kind: str, category: str | None, params: dict[str, Any], page: int,
             fields: tuple[str, ...], include_pinned: bool = True) -> Listing:
    cat = resolve_category(ctx, category) if category else None
    path = f"/c/{cat['id']}/l/{kind}.json" if cat else f"/{kind}.json"
    try:
        data = account_for(ctx).fetch_json(path, {**params, "page": page or None})
    except HttpError as e:
        if e.status in (403, 404) and cat and cat.get("restricted"):
            raise account_for(ctx).member_only(cat) from None
        if e.status in (403, 404) and cat and account_for(ctx).is_guest:
            raise ArgumentError(f"{cat['path']} doesn't exist or may need a login on {ctx.site.name}",
                                help=f"Run `esum {ctx.site.name} categories` for the ones a guest can read") from None
        raise
    items = parse.topic_list(data, ctx.site.base_url, static_category_names(ctx), include_pinned)
    where = f" in {cat['path']}" if cat else ""
    return Listing(name="topics", rows=[parse.topic_row(t) for t in items], fields=fields,
                   about={"list": kind, **params, "category": cat["path"] if cat else None, "page": page,
                          "times": "UTC" if {"last", "created"} & set(fields) else None,
                          "scope": None if cat else account_for(ctx).guest_scope()},
                   more={"page": page + 1} if parse.has_more(data) else None,
                   empty=f"no {kind} topics{where}" + (f" on page {page}" if page else ""),
                   next=[READ_TOPIC])


@action(discourse, tier=Tier.READ)
def search(ctx, query: str | None = None, category: str | None = None, order: Order = "relevance",
           recent: bool = False, days: int = RECENT_DAYS, after: str | None = None,
           before: str | None = None, user: str | None = None, page: int = 1, topic: int | None = None,
           snippet: bool = True):
    """Full-text search: matching topics, each with one matching post (`date`, `post_number`) and a snippet.

    Search is keyword-based, so run several phrasings (formal name, abbreviation, slang). Snippets
    are not evidence: read the threads before answering. `date` is that post's time (UTC); which
    post a topic shows follows --order, so with `latest` it is the newest match.
    Without --query, the filters alone select (e.g. `--user x --after 2026-10-01`), newest first.
    One row per topic, or one per post with `--topic`, which finds a subject inside a long thread
    without reading it. For every post of one user use `user-posts`, for every new post use `posts`.
    Search serves at most 10 pages; narrow a longer one with --after/--before. A useful pattern for
    new topics with their first post: `--query "<words> in:first" --order latest_topic`.

    Args:
        query: Search words (optional with a filter). Discourse operators also work inside it
            (`tags:x`, `in:first`, `status:open`).
        category: Limit to a category: id, name or slug (see the `categories` action).
        order: relevance, latest (newest post), likes, views or latest_topic (newest topic).
        recent: Only the last --days days, newest first.
        days: How far back --recent looks.
        after: Only posts after this day: YYYY-MM-DD, or relative in days or weeks like 7d / 2w.
        before: Only posts before this day: YYYY-MM-DD, or relative in days or weeks like 7d / 2w.
        user: Only posts by this username.
        page: Result page (1-based, about 50 posts per page).
        topic: Only inside this topic id, listing every matching post, newest first.
        snippet: Show the snippet column; `--no-snippet` keeps a long result short.
    """
    if not (query or "").strip():
        if not (category or recent or after or before or user or topic):
            raise ArgumentError("search needs --query or a filter",
                                help="add --query <words>, or filter with --user, --after, --before, --category, "
                                     "--topic or --recent")
        if order == "relevance":
            order = "latest"   # nothing to rank by relevance: newest first
    if page > SEARCH_PAGES:
        raise ArgumentError(f"{ctx.site.name} search serves at most {SEARCH_PAGES} pages",
                            help="narrow it with --after/--before (or --recent --days N) and start again at page 1")
    after, before = _search_day(after, "--after"), _search_day(before, "--before")
    cat = resolve_category(ctx, category) if category else None
    if cat and cat.get("restricted") and account_for(ctx).is_guest:
        raise account_for(ctx).member_only(cat)   # Discourse answers a guest with an empty list, which reads as "no results"
    if recent:
        after = after or parse.days_ago(days)
    if (recent or topic) and order == "relevance":
        order = "latest"
    term = parse.search_term(query or "", category=str(cat["id"]) if cat else None, order=order,
                             after=after, before=before, user=user, topic=topic)
    data = account_for(ctx).fetch_json("/search.json", {"q": term, "page": page if page > 1 else None})
    if err := (data.get("grouped_search_result") or {}).get("error"):
        raise RuntimeError(f"{ctx.site.name} search error: {err}")
    more = parse.search_more(data)
    about: dict[str, Any] = {"query": term, "page": page, "times": "UTC", "scope": None if cat else account_for(ctx).guest_scope()}
    if more and page >= SEARCH_PAGES:
        about["note"] = (f"more results exist, but {ctx.site.name} search stops at page {SEARCH_PAGES}: "
                         "narrow it with --after/--before")
    next_page = {"page": page + 1} if more and page < SEARCH_PAGES else None
    empty = f"no {'posts' if topic else 'topics'} match {term!r}" + (
        f" on page {page}" if page > 1 else "; every word must match, so try fewer")
    if topic:
        fields = ("post_number", "date", "user", "snippet") if snippet else ("post_number", "date", "user")
        return Listing(name="posts", rows=parse.post_hits(data, ctx.site.base_url), fields=fields, about=about,
                       more=next_page, empty=empty,
                       next=[Next("thread", {"id": topic, "posts": "<post_number>"}, "read a matching post")])
    names = static_category_names(ctx)
    rows = [parse.topic_row(t, search=True) for t in parse.search_results(data, ctx.site.base_url, names)]
    fields = ("id", "title", "post_number", "date", "snippet")
    return Listing(name="topics", rows=rows, fields=fields if snippet else fields[:-1], about=about,
                   more=next_page, empty=empty,
                   next=[READ_TOPIC, Next("thread", {"id": "<id>", "from_": "<post_number>", "to": "<post_number>"},
                                          "read the matching post")])


@action(discourse, tier=Tier.READ)
def latest(ctx, category: str | None = None, order: Literal["activity", "created"] = "activity",
           page: int = 0, pinned: bool = True):
    """Latest topics, by last activity or by creation time, optionally in one category.

    Args:
        category: Limit to a category: id, name or slug.
        order: activity (recently bumped first) or created (newest topics first; pinned topics left out).
        page: List page (0-based, about 30 topics per page).
        pinned: Include pinned topics; `--no-pinned` leaves them out (their old dates mislead paging
            by date). For every post since a time, `posts --since` is the direct way.
    """
    params = {"order": "created"} if order == "created" else {}
    when = "created" if order == "created" else "last"
    return _listing(ctx, "latest", category, params, page, ("id", "title", "posts", when),
                    include_pinned=order != "created" and pinned)


@action(discourse, tier=Tier.READ)
def top(ctx, period: Period = "weekly", category: str | None = None, page: int = 0, pinned: bool = True):
    """Top topics of a period (by likes, replies and views), optionally in one category.

    Discourse ranks this by its own score, which it doesn't expose, and a busy old thread may be
    missing from a daily list. For what got the most new posts, count `posts --since 24h` by topic.

    Args:
        period: daily, weekly, monthly, quarterly, yearly or all.
        category: Limit to a category: id, name or slug.
        page: List page (0-based).
        pinned: Include pinned topics; `--no-pinned` leaves them out.
    """
    return _listing(ctx, "top", category, {"period": period}, page, ("id", "title", "posts", "likes"),
                    include_pinned=pinned)


@action(discourse, tier=Tier.READ)
def hot(ctx, category: str | None = None, page: int = 0, pinned: bool = True):
    """Hot (trending) topics, optionally in one category. Needs a recent Discourse version.

    Trending right now, by a score Discourse doesn't expose; `posts` in the rows is the topic's
    all-time count, not recent activity.

    Args:
        category: Limit to a category: id, name or slug.
        page: List page (0-based).
        pinned: Include pinned topics (the forum rules usually head the list); `--no-pinned` leaves them out.
    """
    return _listing(ctx, "hot", category, {}, page, ("id", "title", "posts", "last"), include_pinned=pinned)


def _cap(limit: int | None, after: datetime | None) -> int:
    """No --limit: 100 posts, or with --since every post back to it (up to MAX_LIST_POSTS)."""
    if limit is not None:
        return limit
    return MAX_LIST_POSTS if after else 100


def _more(after: datetime | None, **cursor) -> dict:
    """The next-page arguments: a relative --since (`12h`) becomes the time it meant, so the next
    page continues the same window."""
    return {**cursor, **({"since": f"{after:%Y-%m-%dT%H:%M:%SZ}"} if after else {})}


def read_back(next_page, to_rows, after: datetime | None, limit: int) -> tuple[list[dict], int, Any, bool]:
    """Page back through a newest-first feed. -> (rows, items seen, last item seen, reached the end).

    Feeds are ordered by id, not date: a post held for review or moved in keeps its old date under
    a new id. So one old post is skipped, and only a whole page of them ends the read."""
    rows: list[dict] = []
    seen, last, reached = 0, None, False
    while len(rows) < limit and not reached:
        page = next_page(seen, last)
        old = 0
        for item in page:
            if len(rows) >= limit:
                break
            seen, last = seen + 1, item
            created = parse.utc_time(item.get("created_at"))
            if after and created and created <= after:
                old += 1
            else:
                rows += to_rows(item)
        reached = not page or old == len(page)
    return rows, seen, last, reached


def _since_text(after: datetime | None) -> str | None:
    return f"{after:%Y-%m-%d %H:%M} UTC" if after else None


@action(discourse, tier=Tier.READ)
def posts(ctx, since: str | None = None, limit: int | None = None, before: int | None = None):
    """Every new post across the forum (topics and replies), newest first, with exact times.

    This is the way to read "everything since a time": it pages back 50 posts per request until
    --since or --limit. Each row has the date (UTC, to the minute), the topic id and post number
    (for `thread --id <topic> --from <number>`) and the post's text as `thread` shows it
    (`--fields user,title` adds the author and the topic title; `--json` has every field and the whole text). For
    one topic since a time, `thread --id <topic> --since …` is faster.

    Args:
        since: Only posts after this: YYYY-MM-DD, an ISO time (UTC unless it has an offset), or
            relative like 2w / 12h / 30m / 1d.
        limit: At most this many posts (default 100; with --since, all of them; max 10000).
        before: Start below this post id (the `--before` of the next-page hint).
    """
    after = parse_since(since)
    limit = _cap(limit, after)
    def page(seen: int, last: dict | None) -> list[dict]:
        cursor = last["id"] if last else before
        return account_for(ctx).fetch_json("/posts.json", {"before": cursor}).get("latest_posts") or []

    names = static_category_names(ctx)
    rows, _, last, reached = read_back(page, lambda item: parse.latest_post_rows({"latest_posts": [item]},
                                                                                 ctx.site.base_url, names),
                                       after, limit)
    return Listing(name="posts", rows=rows, fields=("date", "topic", "number", "text"),
                   about={"since": after, "oldest": rows[-1]["date"] if rows else None, "times": "UTC",
                          "scope": account_for(ctx).guest_scope()},
                   more=_more(after, before=last["id"]) if rows and not reached else None,
                   empty="no posts" + (f" since {_since_text(after)}" if after else ""),
                   next=[Next("thread", {"id": "<topic>", "from_": "<number>"}, "read a post in its topic"),
                         Next("user", {"name": "<user>"}, "check an author's account")])


def _user_json(ctx, name: str) -> dict:
    try:
        return account_for(ctx).fetch_json(f"/u/{quote(name)}.json").get("user") or {}
    except HttpError as e:
        if e.status == 404:
            raise NoSuchItem(f"no user {name!r} on {ctx.site.name}") from None
        raise


@action(discourse, tier=Tier.READ)
def user(ctx, name: list[str]):
    """Users' public profiles: account age, last seen and last posted, trust level, reading time,
    days visited, posts read, topics and posts written, likes, badges and top categories.

    These tell a person from a bot or a throwaway account. Times are UTC. Give several names to
    check several accounts in one call; a name that doesn't exist is marked and the rest still
    run. A user may hide their profile: then only the name shows, and the output says so.

    Args:
        name: Usernames (with or without @), separated by spaces or commas.
    """
    names = [n.strip().lstrip("@") for part in name for n in part.split(",") if n.strip().lstrip("@")]
    if not names:
        raise ArgumentError("user needs a name", help="--name <user> [<user> …]")
    profiles, blocks = [], []
    for n in names:
        try:
            data = _user_json(ctx, n)
        except NoSuchItem:
            if len(names) == 1:
                raise
            profiles.append({"username": n, "error": "no such user", "profile_hidden": False})
            blocks.append(f"username: {n}\nerror: no such user")
            continue
        except LoginNeeded:
            if len(names) == 1:
                raise
            profiles.append({"username": n, "error": "login needed", "profile_hidden": False})
            blocks.append(f"username: {n}\nerror: login needed")
            continue
        summary = None
        if not data.get("profile_hidden"):
            try:
                summary = account_for(ctx).fetch_json(f"/u/{quote(n)}/summary.json").get("user_summary")
            except HttpError:
                pass   # stats hidden from guests: the account facts remain
        profile = parse.user_profile(data, summary, ctx.site.base_url)
        profiles.append(profile)
        lines = [f"{k}: {_value(v)}" for k, v in profile.items() if v not in (None, "", [], False)]
        if profile["profile_hidden"]:
            lines.append("note: the user hides their profile, so its dates, trust level, reading time and "
                         "activity are not shown, and `user-posts` can't list their posts")
        blocks.append("\n".join(lines))
    who = [p["username"] or n for p, n in zip(profiles, names)]
    hidden = [w for p, w in zip(profiles, who) if p["profile_hidden"]]
    shown = [w for p, w in zip(profiles, who) if not p["profile_hidden"] and "error" not in p]
    steps = [Next("user-posts", {"name": w}, "read their latest topics and replies") for w in shown[:3]]
    steps += [Next("search", {"user": w, "after": "<YYYY-MM-DD>"},
                   "find their posts, newest first") for w in hidden[:3]]
    text = "times: UTC\n" + "\n\n".join(blocks)
    if locked := [p["username"] for p in profiles if p.get("error") == "login needed"]:
        text += f"\nneeds login: {', '.join(locked)} (tell the user at the end; they log in themselves)"
    return Result(data={"users": profiles}, text=text, next=steps)


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, datetime):
        return f"{v:%Y-%m-%d %H:%M}"
    if isinstance(v, list):
        return "; ".join(map(str, v))
    return str(v)


@action(discourse, name="user-posts", tier=Tier.READ)
def user_posts(ctx, name: str, since: str | None = None, limit: int | None = None, offset: int = 0):
    """One user's new topics and replies, newest first, with exact times.

    Unlike `search --user`, which keeps one row per topic, every post is a row. Each has the date
    (UTC), kind (topic or reply), topic id and post number (for `thread`), the topic title and
    Discourse's short excerpt (read the post with `thread` for all of it). Pages back 30 posts per
    request until --since or --limit. A user who hides their profile has no list: their posts are
    in `posts --since … --json` (filter by user) or `search --user`.

    Args:
        name: Username (with or without @).
        since: Only posts after this: YYYY-MM-DD, an ISO time (UTC unless it has an offset), or
            relative like 2w / 7d / 12h.
        limit: At most this many posts (default 100; with --since, all of them; max 10000).
        offset: Skip this many of the user's newest posts (the `--offset` of the next-page hint).
    """
    name = name.strip().lstrip("@")
    after = parse_since(since)
    limit = _cap(limit, after)
    def page(seen: int, last: dict | None) -> list[dict]:
        try:
            return account_for(ctx).fetch_json("/user_actions.json", {"username": name, "filter": "4,5",
                                                          "offset": offset + seen}).get("user_actions") or []
        except HttpError as e:
            if e.status != 404:
                raise
            _user_json(ctx, name)   # no such user: says so
            raise ArgumentError(f"{name} hides their profile on {ctx.site.name}, so it has no post list",
                                help=f"Run `esum {ctx.site.name} search --user {name} --after <YYYY-MM-DD>` "
                                     f"(newest first), or filter `esum --json {ctx.site.name} posts --since …` "
                                     f"by user") from None

    names = static_category_names(ctx)
    rows, seen, _, reached = read_back(page, lambda item: parse.user_action_rows({"user_actions": [item]},
                                                                                 ctx.site.base_url, names),
                                       after, limit)
    return Listing(name="posts", rows=rows, fields=("date", "topic", "number", "excerpt"),
                   about={"user": name, "since": after, "oldest": rows[-1]["date"] if rows else None, "times": "UTC"},
                   more=_more(after, offset=offset + seen) if rows and not reached else None,
                   empty=f"no posts by {name}" + (f" since {_since_text(after)}" if after else ""),
                   next=[Next("thread", {"id": "<topic>", "from_": "<number>"}, "read a post in its topic"),
                         Next("user", {"name": name}, "check the account")])


@action(discourse, name="tags", tier=Tier.READ)
def list_tags(ctx, query: str | None = None):
    """The forum's tags with their topic counts, most used first, for searching one subject.

    A tag is often the sharpest filter: `search --query "拒 tags:<name>"`. Use the tag's `name`,
    because `tags:` doesn't match a slug.

    Args:
        query: Only tags whose name or slug contains this.
    """
    data = account_for(ctx).fetch_json("/tags.json")
    groups = (data.get("extras") or {}).get("tag_groups") or []
    found = {t.get("name"): t for t in [*(data.get("tags") or []), *(t for g in groups for t in g.get("tags") or [])]}
    rows = [{"name": one_line(t.get("name") or ""), "slug": one_line(t.get("slug") or ""), "count": t.get("count", 0)}
            for t in sorted(found.values(), key=lambda t: -(t.get("count") or 0))]
    if query:
        q = query.lower()
        rows = [r for r in rows if q in r["name"].lower() or q in r["slug"].lower()]
    return Listing(name="tags", rows=rows, fields=("name", "count"), about={"query": query}, total=len(rows),
                   empty=f"no tag name or slug contains {query!r}" if query else "the forum has no tags",
                   next=[Next("search", {"query": "<words> tags:<name>"}, "search one tag")])


@action(discourse, name="categories", tier=Tier.READ)
def list_categories(ctx):
    """The forum's categories: id, "Parent / Child" name and slug, for --category."""
    cats = categories(ctx)
    counted = any(c.get("topic_count") is not None for c in cats)
    order = ("id", "path", "slug", "name", "parent_id", "restricted", "topic_count")
    rows = [{k: (one_line(c.get(k) or "") if k in ("name", "path", "slug") else c.get(k))
             for k in order if k in c and (k != "topic_count" or counted)} for c in cats]
    return Listing(name="categories", rows=rows,
                   fields=("id", "path", "slug", "topic_count") if counted else ("id", "path", "slug"),
                   empty=f"{ctx.site.name} lists no categories",
                   next=[Next("latest", {"category": "<id>"}, "list a category's latest topics"),
                         Next("search", {"query": "<words>", "category": "<id>"}, "search one category")])


@action(discourse, tier=Tier.READ)
def thread(ctx, id: int, mode: Mode = "medium", from_: int | None = None, to: int | None = None,
           since: str | None = None, posts: list[int] | None = None):
    """A topic and its posts in compact-post format, with every gap in the numbering marked.

    `medium` reads the first 30 and the last 30 visible posts; `full` reads every visible post.
    `--from`/`--to` read a post-number range instead (inclusive; either may be left out) and work
    on megatopics too. One call reads at most 1000 posts: read bigger threads range by range.

    `--since` reads the first post plus the posts after a time (one request finds them), which is
    how to follow a busy topic such as a daily thread: `--since 6h`, `--since 2d`, `--since
    2026-10-30`. Times are UTC.

    A quote is shortened to one `> @user #N: …` line (300 characters of a quoted selection, 80
    of a whole-post quote; read #N for the rest), and a link preview to `[link: URL]`.

    Args:
        id: Topic id (the number in the topic URL: /t/<slug>/<id>).
        mode: medium (first 30 + last 30 posts) or full (every visible post).
        from_: First post number to read (overrides --mode).
        to: Last post number to read (overrides --mode).
        since: Only posts after this: YYYY-MM-DD, an ISO time, or relative like 2w / 6h / 30m (overrides
            --mode and --from).
        posts: Read just these post numbers, scattered ones included (at most 30).
    """
    after = parse_since(since)
    if posts and (from_ is not None or to is not None or after is not None):
        raise ArgumentError("--posts can't be combined with --from, --to or --since", help="use one way of choosing posts")
    if posts and len(set(posts)) > MAX_PICKED_POSTS:
        raise ArgumentError(f"--posts takes at most {MAX_PICKED_POSTS} numbers", help="read longer stretches with --from/--to")
    if from_ is not None and to is not None and from_ > to:
        raise ArgumentError(f"--from {from_} is after --to {to}", help="give --from ≤ --to")
    try:
        topic = account_for(ctx).fetch_json(f"/t/{id}.json")
    except HttpError as e:
        if e.status == 404:
            raise NoSuchItem(f"no topic {id} on {ctx.site.name} (deleted, private, or a wrong id)") from None
        raise
    tid, slug = int(topic.get("id") or id), topic.get("slug")
    highest = int(topic.get("highest_post_number") or topic.get("posts_count") or 0)
    ids = parse.stream_ids(topic)
    inline = parse.stream_posts(topic)
    if from_ is not None and from_ > highest:
        raise ArgumentError(f"topic {id} has posts #1–#{highest}; --from {from_} is past the end",
                            help=f"Run `esum {ctx.site.name} thread --id {tid} --from {max(1, highest - 29)} --to {highest}` to read the last posts")
    if after is not None:
        from_ = first_post_since(ctx, tid, after, topic)
        if from_ is None:
            from_ = highest + 1   # nothing since: an empty read with the whole topic marked omitted
    if posts:
        wanted = sorted(set(posts))
        raw = posts_picked(ctx, tid, wanted, inline)
        covered = [(n, n) for n in wanted]
        selection = "posts " + ", ".join(f"#{n}" for n in wanted)
    elif from_ is not None or to is not None:
        lo, hi = max(1, from_ or 1), min(highest, to or highest)
        if hi - lo + 1 > MAX_POSTS:
            raise ArgumentError(f"topic {tid}: #{lo}–#{hi} is {hi - lo + 1} posts; one call reads at most {MAX_POSTS}",
                                help=f"Run `esum {ctx.site.name} thread --id {tid} --from {lo} --to {lo + MAX_POSTS - 1}` to read the first chunk")
        raw = posts_by_number(ctx, tid, lo, hi, inline) if lo <= hi else []
        covered = [(lo, hi)]
        selection = f"posts #{lo}–#{hi}"
    elif ids is not None:
        want = parse.select_ids(ids, mode, HEAD, TAIL)
        if len(want) > MAX_POSTS:
            raise ArgumentError(f"topic {tid} has {len(ids)} visible posts; --mode full reads at most "
                                f"{MAX_POSTS}, so read it in ranges with --from/--to",
                                help=f"Run `esum {ctx.site.name} thread --id {tid} --from 1 --to {MAX_POSTS}` to read the first chunk")
        raw = posts_by_ids(ctx, tid, want, inline)
        covered = [(1, highest)]
        if len(want) < len(ids):
            number = {p["id"]: p["post_number"] for p in raw}
            covered = [(1, number.get(want[HEAD - 1], 1)), (number.get(want[HEAD], highest), highest)]
        selection = "all visible posts" if len(want) == len(ids) else \
            f"first {HEAD} and last {TAIL} of {len(ids)} visible posts"
    else:
        if mode == "full":
            raise ArgumentError(f"topic {tid} is a megatopic (highest post #{highest}); read it in ranges with "
                                f"--from/--to (at most {MAX_POSTS} posts per call)",
                                help=f"Run `esum {ctx.site.name} thread --id {tid} --from 1 --to {MAX_POSTS}` to read the first chunk")
        raw, covered = megatopic_head_tail(ctx, tid, inline, HEAD, TAIL, highest)
        selection = f"first {HEAD} and last {TAIL} posts of a megatopic"
    op_id = parse.op_user_id(topic)
    read = [parse.to_post(p, base_url=ctx.site.base_url, topic_id=tid, slug=slug, op_id=op_id)
            for p in raw]
    opening = next((parse.to_post(p, base_url=ctx.site.base_url, topic_id=tid, slug=slug, op_id=op_id)
                    for p in inline if p["post_number"] == 1), None)
    url = parse.post_url(ctx.site.base_url, tid, slug=slug)
    names = static_category_names(ctx)
    meta = parse.topic_meta_line(topic, names.get(topic.get("category_id")))
    if after is not None:
        selection = (f"posts since {after:%Y-%m-%d %H:%M} UTC" if covered[0][0] <= covered[0][1]
                     else f"no posts since {after:%Y-%m-%d %H:%M} UTC")
    visible = len(ids) if ids else int(topic.get("posts_count") or highest)   # numbers skip deleted posts
    lo, hi = covered[0] if from_ is not None or to is not None else (1, highest)
    spans = [(max(lo, a), min(hi, b), "deleted or hidden") for a, b in covered]
    spans.append((1, highest, "not read"))
    if after is not None:
        spans.insert(0, before_span(after, min(lo - 1, highest)))
        if opening is not None and 1 < lo <= highest:
            read.append(opening)
    view = thread_view(read, spans, count=visible)
    steps = [] if posts else view.range_steps({"id": tid})
    if steps and (ctx.site.extras.get("guest_search", True) or not account_for(ctx).is_guest):
        steps.append(Next("search", {"query": "<words>", "topic": tid}, "find a subject in them"))
    data = {"id": tid, "title": topic.get("title"), "url": url, "highest_post_number": highest,
            "posts_count": topic.get("posts_count"), "visible_posts": len(ids) if ids else None,
            "megatopic": ids is None, "selection": selection, "category_id": topic.get("category_id")}
    return view.result(topic.get("title") or f"Topic {tid}", url=url, data=data,
                       notes=[f"{meta} · Highest post: #{highest} · Times: UTC · Read: {selection}"], next=steps)


def first_post_since(ctx, topic_id: int, after: datetime, topic: dict) -> int | None:
    """The number of the topic's first post after `after`, or None when there is none. Discourse's
    `/posts/by-date/{topic}/{time}` answers with that post in one request (404 when none)."""
    stamp = after.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        post = account_for(ctx).fetch_json(f"/posts/by-date/{topic_id}/{stamp}.json")
    except HttpError as e:
        if e.status != 404:
            raise
        last = parse.utc_time(topic.get("last_posted_at"))
        if last is not None and last > after:
            raise ArgumentError(f"{ctx.site.name} can't find posts by date (an older Discourse)",
                                help="read by number with --from/--to instead") from None
        return None
    return int(post["post_number"]) if post.get("post_number") else None


def posts_picked(ctx, topic_id: int, numbers: list[int], inline: list[dict]) -> list[dict]:
    """Scattered posts by number. A deleted one is skipped, and shows as a gap."""
    known = {p["post_number"]: p for p in inline}
    out = []
    for n in numbers:
        if n in known:
            out.append(known[n])
            continue
        try:
            out.append(account_for(ctx).fetch_json(f"/posts/by_number/{topic_id}/{n}.json"))
        except HttpError as e:
            if e.status != 404:
                raise
    return out


def posts_by_ids(ctx, topic_id: int, ids: list[int], inline: list[dict]) -> list[dict]:
    """Posts by id, in thread order; reuses the posts `/t/{id}.json` already returned."""
    by_id = {p["id"]: p for p in inline}
    need = [i for i in ids if i not in by_id]
    for batch in chunked(need, IDS_PER_REQUEST):
        data = account_for(ctx).fetch_json(f"/t/{topic_id}/posts.json", {"post_ids[]": batch})
        for p in parse.stream_posts(data):
            by_id[p["id"]] = p
    return [by_id[i] for i in ids if i in by_id]


def posts_by_number(ctx, topic_id: int, lo: int, hi: int, inline: list[dict]) -> list[dict]:
    """Every visible post numbered lo..hi, paging forward by post number (20 per page)."""
    found = {p["post_number"]: p for p in inline if lo <= p["post_number"] <= hi}
    inline_max = max((p["post_number"] for p in inline), default=0)
    # The inline posts are the topic's first visible posts, so nothing below their last one is missing.
    cursor = inline_max if lo <= inline_max else lo - 1
    anchor = None if lo <= inline_max else lo
    while cursor < hi:
        page = parse.stream_posts(account_for(ctx).fetch_json(f"/t/{topic_id}/posts.json",
                                             {"post_number": cursor, "asc": "true"}))
        nums = [p["post_number"] for p in page]
        if anchor is not None and anchor not in nums:
            # Discourse resolves a missing anchor post to the nearest one, which can be post `lo`
            # itself, and then returns only posts after it. A near request includes `lo`.
            page += parse.stream_posts(account_for(ctx).fetch_json(f"/t/{topic_id}/posts.json",
                                                  {"post_number": anchor}))
            nums = [p["post_number"] for p in page]
        anchor = None
        if not nums or max(nums) <= cursor:
            break
        for p in page:
            if lo <= p["post_number"] <= hi:
                found.setdefault(p["post_number"], p)
        cursor = max(nums)
    return [found[n] for n in sorted(found)]


def megatopic_head_tail(ctx, topic_id: int, inline: list[dict], head: int, tail: int,
                        highest: int) -> tuple[list[dict], list[tuple[int, int]]]:
    """First `head` and last `tail` posts of a topic without a post stream, and the post-number
    spans they cover completely."""
    first = sorted(inline, key=lambda p: p["post_number"])
    while len(first) < head:
        page = parse.stream_posts(account_for(ctx).fetch_json(f"/t/{topic_id}/posts.json",
                                             {"post_number": first[-1]["post_number"] if first else 0,
                                              "asc": "true"}))
        page = [p for p in page if not first or p["post_number"] > first[-1]["post_number"]]
        if not page:
            break
        first += sorted(page, key=lambda p: p["post_number"])
    last = parse.stream_posts(account_for(ctx).fetch_json(f"/t/{topic_id}/last.json"))
    while last and len(last) < tail:
        low = min(p["post_number"] for p in last)
        page = [p for p in parse.stream_posts(account_for(ctx).fetch_json(f"/t/{topic_id}/posts.json",
                                                         {"post_number": low, "asc": "false"}))
                if p["post_number"] < low]
        if not page:
            break
        last += page
    first = first[:head]
    last = sorted(last, key=lambda p: p["post_number"])[-tail:]
    merged = {p["post_number"]: p for p in first + last}
    covered = []
    if first:
        covered.append((1, first[-1]["post_number"]))
    if last:
        covered.append((last[0]["post_number"], highest))
    return [merged[n] for n in sorted(merged)], covered


# --- the write -----------------------------------------------------------------------------------------

@action(discourse, tier=Tier.REVERSIBLE)
def bookmark(ctx, topic: int, posts: list[int] | None = None):
    """Bookmark a topic, or posts in it, on the user's account.

    Reversible: the user removes a bookmark on the site. Uses a saved key with the write scope
    (`login`), else the user's login in the browser ESUM uses. A write: run it only when the user
    asks. The output and the write log name each bookmark and the account it went to. Something the
    account already has bookmarked is a no-op: nothing is sent, and it exits 0.

    Args:
        topic: Topic id.
        posts: Post numbers in that topic to bookmark instead of the topic itself.
    """
    writer = account_for(ctx).for_write()
    meta = writer.fetch_json(f"/t/{topic}.json")
    title = meta.get("title") or f"topic {topic}"
    targets: list[tuple[str, int, str, bool]] = []
    if not posts:
        targets.append(("Topic", topic, f"topic #{topic} “{title}”", _topic_bookmarked(meta, topic)))
    for n in posts or []:
        post = writer.fetch_json(f"/posts/by_number/{topic}/{n}.json")
        who = post.get("username") or "?"
        targets.append(("Post", int(post["id"]), f"post #{n} by {who} in topic #{topic} “{title}”",
                        post.get("bookmarked") is True))

    def run(kind: str, target: int, already: bool):
        if already:   # the account has it bookmarked: the asked-for state holds, so nothing is sent (AXI §6)
            return {"already_bookmarked": True, kind.lower(): target}
        out = writer.post("/bookmarks.json", {"bookmarkable_id": target, "bookmarkable_type": kind},
                          failure_help=f"Run `esum {ctx.site.name} thread --id {topic}` to check bookmark state before retrying")
        return {"bookmark_id": out.get("id"), kind.lower(): target}

    where = ctx.site.base_url
    return [Write(f"{'Already bookmarked (no-op):' if already else 'Bookmark'} {desc} for @{writer.username} on {where}",
                  lambda kind=kind, target=target, already=already: run(kind, target, already))
            for kind, target, desc, already in targets]


def _topic_bookmarked(meta: dict, topic: int) -> bool:
    """Whether the reading account has the topic itself bookmarked: `bookmarks` lists each of its
    bookmarks in the topic (topic and posts); older forums only say `bookmarked`."""
    marks = meta.get("bookmarks")
    if isinstance(marks, list):
        return any(isinstance(b, dict) and b.get("bookmarkable_type") == "Topic"
                   and b.get("bookmarkable_id") == topic for b in marks)
    return meta.get("bookmarked") is True


# --- login: a User API Key the user authorizes in their browser (D-46) -------------------------------

def _ask(prompt: str) -> str:
    """One line from the user. Prompts go to stderr, so `--json` stdout stays one document."""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    line = sys.stdin.readline()
    if not line:
        raise ArgumentError("login cancelled", help="nothing was saved")
    return line.strip()


def _read_payload() -> str:
    """The payload, which may be pasted over several lines: read until it is all there."""
    text = _ask("Paste the payload here (an empty line ends it): ")
    while len("".join(text.split())) < auth.PAYLOAD_CHARS:
        line = sys.stdin.readline()
        if not line.strip():
            break
        text += line.strip()
    return text


@action(discourse, tier=Tier.READ)
def login(ctx, scopes: str | None = None, open_browser: bool = True):
    """Log in with a User API Key, interactively: reads then see what the user's account sees,
    without a browser session.

    The user runs this in their own terminal, never an agent; without a terminal it refuses. It asks
    for the scopes, opens the authorization page in the default browser, and waits. The user logs
    in there themselves, approves, and pastes back the payload Discourse shows. ESUM never sees a
    password and never prints the key.

    Args:
        scopes: What the key may do: read, or read,write so write actions such as `bookmark` can use
            it too. Asked for when left out.
        open_browser: Open the page in the default browser (the URL is printed either way).
    """
    name = ctx.site.name
    ctx.require_terminal()
    wanted = auth.parse_scopes(scopes or _ask("Scopes? read, or read,write [read]: ") or "read")
    account = account_for(ctx)
    pending = auth.start(ctx.site.base_url, wanted, account.saved)
    sys.stderr.write(f"\nAuthorize ESUM on {ctx.site.base_url}:\n\n{pending.url}\n\n"
                     "Log in there if asked, approve, and copy the payload the page shows.\n")
    if open_browser:
        import webbrowser
        webbrowser.open(pending.url)
    for tries_left in (2, 1, 0):
        try:
            saved = auth.finish(name, pending, _read_payload())
            break
        except ArgumentError as e:
            if not tries_left or "cancelled" in str(e):
                raise
            sys.stderr.write(f"{e}; {e.help}. Try again.\n")
    account.adopt(saved)
    who = (account.current_user(ctx.http) or {}).get("username") or "?"
    return Result(data={"username": who, "scopes": saved["scopes"]},
                  text=f"logged in: {one_line(who)}\nscopes: {','.join(saved['scopes'])}")


@action(discourse, tier=Tier.READ)
def whoami(ctx):
    """Which account reads use: the saved key's user and scopes, or none. Never prints the key."""
    account = account_for(ctx)
    saved = account.saved
    if not saved:
        return Result(data={"logged_in": False}, text=f"logged in: no (no saved key on {ctx.site.name})")
    user = account.current_user(ctx.http)
    if not user:
        raise account.invalid_login(ctx.http,
                                            f"HTTP login check at {ctx.http.url('/session/current.json')} shows no logged-in user")
    who = one_line(user.get("username") or "?")
    return Result(data={"logged_in": True, "username": who, "scopes": saved.get("scopes"),
                        "created": saved.get("created")},
                  text=f"logged in: {who}\nscopes: {','.join(saved.get('scopes', []))}\ncreated: {saved.get('created')}")
