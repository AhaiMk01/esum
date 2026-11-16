"""vBulletin engine: vBulletin 3.x forums read from their HTML pages (there is no JSON API).

Thread pages hold a fixed number of posts each (15 on FlyerTalk), so a thread read fetches only
the pages that hold the wanted post numbers. Search posts vBulletin's own `search.php` form with
the session's security token (read from `search.php`, read again once if rejected); a result
page is addressed by the `searchid` the first request redirects to.

The site's `base_url` is the forum root (the folder holding `showthread.php`), e.g.
`https://www.flyertalk.com/forum`.

Site extras:
    forums      [(id, slug, name), …]: a forum catalog, so `--forum` takes a slug as well as an id
    utc_offset  hours from UTC of the forum's display time zone: dates "Today" / "Yesterday" on a
                page without vBulletin's "The time now is" footer (else the local date is used)
    time_zone   an IANA zone name (e.g. "America/Chicago") for a forum that
                shows each date with daylight saving as of that date, so the footer's one offset
                ("GMT -6") is not every date's
"""

from __future__ import annotations

import re
from typing import Literal

from esum import (ArgumentError, Listing, Mode, Next, NoSuchItem, Post, Tier, action, engine, thread_view,
                  read_pages, read_spans, select_numbers)

from . import parse

FORMAT = 1
PORTED_FROM = ("xgoose skills/vbulletin-forum.source.xgs.js (github.com/xGoose-dev/xgoose); xgoose "
               "vbulletin-forum.xgs.js getRecentThreads (RSS feed for `recent`); "
               "nitan_plugin My.user.js (vBulletin adapter: per-page thread fetches, quote markers, page count, "
               "ISO-8859-1 decoding)")

EXTRAS = ("forums", "utc_offset", "time_zone")  # site extras the actions read, besides SITE_EXTRAS
vbulletin = engine("vbulletin", description="vBulletin 3.x forums from their HTML pages: post and thread "
                                            "search, forum listings, threads in compact-post format")

Show = Literal["posts", "threads"]
Sort = Literal["last-post", "newest", "replies", "views"]
SORTS = {"last-post": "lastpost", "newest": "dateline", "replies": "replycount", "views": "views"}


def page_today(ctx, html: str):
    """The forum's date for "Today" on this page: its footer, else the `utc_offset` extra."""
    return parse.page_today(html, ctx.site.extras.get("utc_offset"))


def page_zone(ctx, html: str) -> str:
    """The zone this page's dates are in, as `parse.in_zone` takes it."""
    return parse.time_zone(html, ctx.site.extras.get("time_zone") or "")


def forum_id(ctx, ref: str) -> int:
    """A forum id, URL, or a slug / name from the site's `forums` catalog -> id."""
    if (fid := parse.forum_ref(ref)) is not None:
        return fid
    want = ref.strip().lower()
    for fid, slug, name in ctx.site.extras.get("forums") or ():
        if want in (slug.lower(), name.lower()):
            return fid
    forums = ctx.site.extras.get("forums")
    if forums:
        raise ArgumentError(f"unknown forum {ref!r}: pass a forum id, forum URL or a slug such as {forums[0][1]!r}",
                            help=f"`esum {ctx.site.name} forums` lists the slugs; any other forum works by id or URL")
    raise ArgumentError(f"unknown forum {ref!r}: pass a forum id or forum URL")


def forum_help(ctx) -> str:
    """The next step after a forum that doesn't exist."""
    if ctx.site.extras.get("forums"):
        return (f"Check the forum: `esum {ctx.site.name} forums` lists forum ids and slugs, "
                "and any other forum works by its id or URL")
    return "Check the forum id: copy the forum's URL from the site"


def get_page(ctx, path: str, params: dict, what: str, missing_help: str | None = None):
    """A page, with a 404 (an unknown thread or forum id) as NoSuchItem: its HTML says nothing more."""
    resp = ctx.http.get(path, params=params, check=False)
    if resp.status == 404:
        error = NoSuchItem(f"{ctx.site.name} {what}: not found (HTTP 404)")
        error.help = missing_help
        raise error
    if not resp.ok:
        raise resp.http_error()
    return resp


def check_page(ctx, html: str, what: str) -> None:
    """vBulletin answers errors with a 200 "vBulletin Message" page: no permission (a private forum
    or a logged-out session) becomes LOGIN NEEDED, anything else (flood control, bad id) passes
    through as raised."""
    msg = parse.error_message(html)
    if msg is None or parse.NO_MATCHES.search(msg):
        return
    if parse.NO_PERMISSION.search(msg):
        raise ctx.login_needed(detail=msg)
    raise RuntimeError(f"{ctx.site.name} {what}: {msg}")


# --- search --------------------------------------------------------------------------------------------

@action(vbulletin, tier=Tier.READ)
def search(ctx, query: str, show: Show = "posts", titles_only: bool = False, forum: list[str] | None = None,
           user: str | None = None, days: int | None = None, page: int = 1):
    """Search the forum's posts or threads by keyword.

    `posts` (default) lists matching posts with a snippet, their thread id and post id (`--fields
    date` adds the post's date; read the
    page holding a post with `thread --id <thread> --post <post>`). `threads` lists matching
    threads with reply counts and the latest reply's date (`--fields pages,last_user,excerpt`
    adds more). Results come 25 per page in the site's order, which is often but not always newest
    first; use `--days` to keep only recent posts. A snippet is the site's preview and doesn't mark
    quoted text, so it can be someone else's words. All words must match, and the index leaves out
    short words and punctuated terms such as `5/24`, so a query with one finds nothing: an empty
    result names such terms and gives the search without them.

    Args:
        query: Search words (English; the index does not match CJK text).
        show: posts (each matching post) or threads (each matching thread once).
        titles_only: Match thread titles only.
        forum: Only these forums and their sub-forums (ids, forum URLs, or slugs from `forums`).
        user: Only posts by this user (exact user name).
        days: Only posts from the last N days.
        page: Result page.
    """
    # The search form is posted, as the site's own search box does: FlyerTalk ignores some fields
    # (titleonly) when they come as a GET query. It carries the session's own security token
    # (a member's is not "guest"); a stale one is rejected, so it is read again once.
    form = {
        "do": "process", "s": "", "query": query,
        "showposts": 1 if show == "posts" else 0, "titleonly": 1 if titles_only else 0,
        "forumchoice[]": [forum_id(ctx, f) for f in forum] if forum else None,
        "childforums": 1 if forum else None,
        "searchuser": user, "exactname": 1 if user else None,
        "searchdate": days, "beforeafter": "after" if days else None,
    }
    form = {k: v for k, v in form.items() if v is not None}
    for attempt in (1, 2):
        token = parse.security_token(ctx.http.get("search.php", dedupe=False).text())
        resp = ctx.http.request("POST", "search.php?do=process", data={**form, "securitytoken": token})
        html = resp.text()
        msg = parse.error_message(html)
        if attempt == 1 and msg and parse.STALE_TOKEN.search(msg):
            continue
        break
    check_page(ctx, html, "search")
    meta = parse.parse_search_meta(html, resp.url)
    # some boards answer with a "transfer you to the results" page that refreshes to them
    redirect = None if meta["total"] else parse.refresh_searchid(html)
    if redirect or (page > 1 and meta["searchid"] and meta["total"]):
        resp = ctx.http.get("search.php", params={"searchid": redirect or meta["searchid"],
                                                  "page": page if page > 1 else None})
        html = resp.text()
        check_page(ctx, html, "search")
        meta = {**parse.parse_search_meta(html, resp.url), "page": page}
    base = ctx.http.url("/")
    today, zone = page_today(ctx, html), page_zone(ctx, html)
    rows = []
    if meta["total"]:
        rows = (parse.parse_search_posts(html, base, today, zone) if show == "posts"
                else parse.parse_thread_rows(html, base, today, zone))
    about = {"query": query, "show": show, "titles_only": titles_only or None, "user": user, "days": days,
             "forum": ", ".join(forum) if forum else None, "page": page, "pages": meta["pages"],
             "times": zone or None}
    # vBulletin's index drops short words and splits on punctuation, and a dropped term can zero an
    # all-words search (FlyerTalk: `sapphire 5/24` finds nothing): name such terms on an empty list.
    unindexed, kept = unindexed_terms(query)
    hint = (f"; the index leaves out short words and punctuated terms, so {', '.join(map(repr, unindexed))} "
            f"may match nothing: search without them, then look for them in the threads"
            if unindexed and not rows else "")
    retry = [Next("search", {"query": kept, "page": 1}, f"search without {', '.join(unindexed)}", when_empty=True)
             ] if hint and kept else []
    if show == "posts":
        return Listing(name="results", rows=rows, fields=("thread_id", "post_id", "title", "snippet"),
                       about=about, total=meta["total"], more={"page": page + 1} if rows and page < meta["pages"] else None,
                       empty=f"no posts match {query!r}" + hint,
                       next=[*retry,
                             Next("thread", {"id": "<thread_id>", "post": "<post_id>"}, "read the page holding a post"),
                             Next("thread", {"id": "<thread_id>"}, "read a whole thread")])
    return Listing(name="results", rows=rows, fields=("id", "title", "replies", "last_post"),
                   about=about, total=meta["total"], more={"page": page + 1} if rows and page < meta["pages"] else None,
                   empty=f"no threads match {query!r}" + hint,
                   next=[*retry, Next("thread", {"id": "<id>"}, "read a thread")])


def _indexed(word: str) -> bool:
    core = word.strip('"+-*')
    return len(core) >= 3 and re.fullmatch(r"[\w'-]+", core) is not None


def unindexed_terms(query: str) -> tuple[list[str], str]:
    """The query's words vBulletin's index leaves out (short or punctuated), and the query without
    them, keeping `"quoted phrases"` (and a +/- in front of one) well-formed. The query without them
    is "" when nothing is left, or when its quotes don't pair up, so no broken retry is offered."""
    dropped: list[str] = []
    kept: list[str] = []
    for token in re.findall(r'[+-]?"[^"]*"|\S+', query):
        if m := re.fullmatch(r'([+-]?)"([^"]*)"', token):
            words = m.group(2).split()
            dropped += [w for w in words if not _indexed(w)]
            left = [w for w in words if _indexed(w)]
            if len(left) > 1:
                kept.append(f'{m.group(1)}"{" ".join(left)}"')
            elif left:
                kept.append(m.group(1) + left[0])
        elif _indexed(token):
            kept.append(token)
        else:
            dropped.append(token)
    return dropped, "" if query.count('"') % 2 else " ".join(kept)


# --- forum listing -------------------------------------------------------------------------------------

@action(vbulletin, tier=Tier.READ)
def forum(ctx, id: str, page: int = 1, sort: Sort = "last-post"):
    """One page of a forum's thread list: stickies first, then threads by latest reply.

    Each thread shows its id (for `thread`), title, replies and the latest reply's date;
    `--fields starter,started,pages` (or `--fields all`) adds the starter, views, page count and
    more. A thread merged into another keeps its row as `Moved to <id>: <title>`, with no counts;
    `thread` on either id reads the thread it moved to.

    Args:
        id: Forum id, forum URL, or a slug from `forums`.
        page: Page of the thread list.
        sort: last-post (latest reply first), newest (newest threads first), replies, or views.
    """
    fid = forum_id(ctx, id)
    params = {"f": fid, "page": page if page > 1 else None,
              "sort": SORTS[sort] if sort != "last-post" else None, "order": "desc" if sort != "last-post" else None}
    resp = get_page(ctx, "forumdisplay.php", params, f"forum {fid}", forum_help(ctx))
    html = resp.text()
    check_page(ctx, html, f"forum {fid}")
    zone = page_zone(ctx, html)
    data = parse.parse_forum_page(html, ctx.http.url("/"), page_today(ctx, html), zone)
    about = {"id": fid, "title": data["title"], "sort": sort, "page": data["page"], "pages": data["pages"],
             "url": data["url"] or resp.url, "times": zone or None}
    return Listing(name="threads", rows=data["threads"], fields=("id", "title", "replies", "last_post"), about=about,
                   more={"page": data["page"] + 1} if data["page"] < data["pages"] else None,
                   empty=f"forum {fid} has no threads on page {data['page']}",
                   next=[Next("thread", {"id": "<id>"}, "read a thread")])


@action(vbulletin, tier=Tier.READ)
def recent(ctx, forum: list[str] | None = None):
    """The newest threads site-wide, or in some forums, from the forum's RSS feed: newest started first.

    One request for the latest threads across every forum (or only `--forum`, sub-forums
    excluded), each with its id, title, forum and start date. The feed carries each thread's first
    post: `--fields excerpt` adds a one-line preview, `--fields text` (or `--json`) the first post as
    the feed gives it, which may be shortened (`thread --id <id>` reads it whole). The feed has no
    pages; for older threads in one forum use `forum --id <forum> --sort newest`.

    Args:
        forum: Only these forums (ids, forum URLs, or slugs from `forums`); default all forums.
    """
    fids = [forum_id(ctx, f) for f in forum] if forum else []
    resp = ctx.http.get("external.php", params={"type": "RSS2",
                                                "forumids": ",".join(map(str, fids)) if fids else None},
                        headers={"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"})
    body = resp.text()
    if fids and not body.strip("﻿ \r\n\t"):   # vBulletin sends no feed at all for a forum id it doesn't have
        error = NoSuchItem(f"{ctx.site.name} recent: no forum {', '.join(map(str, fids))} (the feed is empty)")
        error.help = forum_help(ctx)
        raise error
    if not body.lstrip("﻿ \r\n\t").startswith("<?xml") and "<rss" not in body[:500]:
        check_page(ctx, body, "recent")   # an HTML "vBulletin Message" (no permission, RSS off)
    rows = parse.parse_rss(body)
    about = {"forum": ", ".join(forum) if forum else "all forums", "times": "UTC" if rows else None}
    # The feed holds only threads posted directly in the named forums, so a section such as
    # "Airlines & Mileage Programs" (all its threads are in sub-forums) gives an empty feed.
    steps = [Next("thread", {"id": "<id>"}, "read a thread")]
    if forum and len(forum) == 1:
        steps.append(Next("forum", {"id": forum[0], "sort": "newest"}, "page back through the forum by start date"))
    if forum and not rows and "forums" in ctx.site.all_actions:
        steps.append(Next("forums", {}, "find a sub-forum", when_empty=True))
    empty = (f"the feed lists no threads in {', '.join(forum)}: it leaves out sub-forums, so pass a sub-forum"
             if forum else "the feed lists no threads")
    return Listing(name="threads", rows=rows, fields=("id", "title", "forum", "started"), about=about,
                   total=len(rows), empty=empty, next=steps)


# --- the thread ----------------------------------------------------------------------------------------

def thread_page(ctx, tid: int | None, page: int | None, post: int | None = None) -> parse.ThreadPage:
    """One thread page, by page number, or (`post`) the page that holds that post."""
    params = {"p": post} if post else {"t": tid, "page": page if page and page > 1 else None}
    resp = get_page(ctx, "showthread.php", params, f"thread {tid}" if tid else f"post {post}")
    html = resp.text()
    check_page(ctx, html, f"thread {tid or f'of post {post}'}")
    tp = parse.parse_thread_page(html, resp.url, None if post else page or 1, page_today(ctx, html),
                                 page_zone(ctx, html))
    if not tp.posts and (post or (page or 1) == 1):
        raise RuntimeError(f"{ctx.site.name}: no posts found on {resp.url}")
    return tp


@action(vbulletin, tier=Tier.READ)
def thread(ctx, id: str, mode: Mode = "medium", from_: int | None = None, to: int | None = None,
           page: int | None = None, post: int | None = None):
    """A thread in compact-post format.

    Posts are numbered #1… as on the site; `↩#M` marks a post that quotes post #M, and `↩post N` one
    that quotes a post outside what was read (`--post N` reads it). A quote is one `> @user: …` line,
    cut only when the quoted post is shown too. `medium` reads the first and last 30 posts, `full` reads
    everything (one request per page of 15, so prefer ranges for very long threads). A FlyerTalk
    wikipost (the community summary above post #1) is shown as #0 whenever post #1 is read.

    Args:
        id: Thread id, thread URL or post URL (a URL with a page number or post id reads that page).
        mode: medium (first and last 30 posts) or full.
        from_: First post number to read (overrides mode).
        to: Last post number to read (overrides mode).
        page: Read this thread page only (as numbered on the site; overrides mode).
        post: Read the page holding this post id (as `search` lists it; overrides mode).
    """
    tid, url_page, url_post = parse.thread_ref(str(id))
    ranged = from_ is not None or to is not None
    if page is None and not ranged:
        page = url_page
        post = post or (url_post if page is None else None)
    if post and page is None and not ranged:
        first = thread_page(ctx, tid, None, post=post)
        page = first.page
        if tid is None:
            try:
                tid = parse.thread_ref(first.url)[0]
            except ArgumentError as error:
                raise RuntimeError(f"{ctx.site.name}: unrecognized canonical thread URL {first.url!r} (the layout may have changed)") from error
            if tid is None:
                raise RuntimeError(f"{ctx.site.name}: canonical URL {first.url!r} names no thread (the layout may have changed)")
    else:
        if tid is None:
            raise ArgumentError(f"{id!r} names a post, not a thread: pass it as --post or give the thread id")
        first = thread_page(ctx, tid, page or 1)
    if page and page > 1 and not first.posts:
        raise RuntimeError(f"{ctx.site.name}: no posts on thread page {page} (the layout may have changed)")
    if page and page > first.pages:   # vBulletin serves the last page instead
        raise ArgumentError(f"thread {tid} has no page {page} (it has {first.pages})")
    total, per_page = first.total, first.per_page
    wanted = set(range((page - 1) * per_page + 1, min(page * per_page, total) + 1)) if page else set(
        select_numbers(total, mode, from_, to))
    read = read_pages(lambda n: thread_page(ctx, tid, n), {first.page: first}, wanted,
                      lambda n: (n - 1) // per_page + 1, lambda p: ((f.number, f) for f in p.posts))
    # An insertion can move the same post forward one floor between page reads.
    by_id: dict[int, parse.RawPost] = {}
    for p in read.floors.values():
        by_id.setdefault(p.id, p)
    raw = list(by_id.values())
    posts: list[Post] = parse.to_posts([p for p in raw if p.number in wanted], raw)
    head = read.pages.get(1) or first
    wiki = head.wiki if 1 in wanted else None
    if wiki is not None:
        cap = parse.WIKI_CHAR_CAP if mode == "medium" and not ranged and not page else None
        wiki.text = parse.clip(wiki.text, cap, "; --mode full shows all")
        posts.insert(0, wiki)
    view = thread_view(posts, read_spans(wanted, total), count=total + int(wiki is not None))
    title = head.title or f"Thread {tid}"
    url = head.url if head.page == 1 else ctx.http.url(f"showthread.php?t={tid}")
    if post and any(p.id == post for p in first.posts):
        where = f" (holds post {post})"
    elif post:   # vBulletin serves the page where the post would be, even when it's gone
        where = f" (post {post} not found on page {page}: deleted, hidden or moved)"
    else:
        where = ""
    line = " · ".join(b for b in (head.forum and f"Forum: {head.forum}",
                                  f"{total} posts in {first.pages} page(s) of {per_page}",
                                  head.closed and "closed", head.zone and f"Times: {head.zone}",
                                  page and f"page {page}{where}") if b)
    data = {"id": tid, "title": title, "url": url, "forum": head.forum, "total": total, "pages": first.pages,
            "per_page": per_page, "page": page, "closed": head.closed}
    result = view.result(title, url=url, data=data, notes=[line],
                         next=view.range_steps({"id": str(tid), "page": None, "post": None}))
    # The text keeps the forum's display time (labelled `Times: America/Chicago`); thread text would convert
    # an aware date to UTC, so the offset is attached only after rendering, for `--json`.
    for p in posts:
        p.date = parse.in_zone(p.date, head.zone)
    return result
