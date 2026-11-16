"""Discuz! engine: Discuz! X 3.x forums read from their HTML pages.

Many Discuz! sites turn off the mobile JSON API (`api/mobile/index.php`), so this engine reads
the desktop templates through the query-string URLs every Discuz! site answers
(`forum.php?mod=forumdisplay&fid=…`, `forum.php?mod=viewthread&tid=…&page=…`), whether or not the
site rewrites them to `forum-24-1.html` / `thread-…-1-1.html`. Post numbers are the Discuz! floor
numbers (#1 is the OP). Errors arrive as a 200 notice page; one that asks to log in becomes LOGIN
NEEDED, any other passes through raw.

The site's `base_url` is the folder holding `forum.php` (Discuz! may live under a sub-path such
as `https://stage1st.com/2b`), e.g. `https://www.52pojie.cn`. Query strings go out in the site's
declared `charset`, so a GBK forum's search gets a GBK-encoded query. A missing thread or board
can come back as HTTP 404 that still carries the notice, which is read like a 200 notice.
"""

from __future__ import annotations

from typing import Literal

import time
from urllib.parse import urlsplit

from esum import (NoSuchItem, ArgumentError, SiteRefused, format_post, Listing, Mode, Next, Post, Tier, action, engine,
                  thread_view, read_pages, read_spans, select_numbers, to_spans)

from . import parse

FORMAT = 1
PORTED_FROM = ("original work; search, thread-type filter, anti-copy (jammer) cleanup, lazy images and "
               "404 notices ported from xgoose apps/chrome/skills/discuz-forum.xgs.js "
               "(github.com/xGoose-dev/xgoose)")

EXTRAS = ()  # this engine reads no site extras besides SITE_EXTRAS
discuz = engine("discuz", description="Discuz! X 3.x forums from their HTML pages: board catalog, "
                                      "thread lists, title search, threads in compact-post format")

Order = Literal["lastpost", "dateline", "replies", "views", "heats"]
SearchOrder = Literal["lastpost", "dateline", "replies", "views"]
SEARCH_WAIT_MAX = 15   # seconds: a longer flood-control wait is returned to the agent instead
View = Literal["new", "replied", "hot", "digest"]
VIEWS = {"new": "newthread", "replied": "new", "hot": "hot", "digest": "digest"}
FILTERS = {"dateline": "author", "replies": "reply", "views": "reply", "heats": "heat", "lastpost": "lastpost"}


def check_page(ctx, html: str, what: str) -> None:
    """A notice page that asks to log in becomes LOGIN NEEDED; one saying the item doesn't exist is
    NoSuchItem; any other notice (closed board, flood control, maintenance) is refused, raw."""
    if msg := parse.needs_login(html):
        raise ctx.login_needed(detail=f"{what}: {msg}")
    if msg := parse.message(html):
        error = NoSuchItem if parse.not_found(msg) else SiteRefused
        raise error(f"{ctx.site.name} {what}: {msg}")


def fetch(ctx, path: str, params: dict, dedupe: bool | None = None):
    """GET a page; a 404 or 403 that carries a Discuz notice is returned for reading like a 200."""
    resp = ctx.http.request("GET", path, params=params, check=False, dedupe=dedupe)
    if resp.status >= 400 and parse.message(resp.text()) is None:
        if resp.status == 404:   # the site's own 404 page, not a Discuz notice
            raise NoSuchItem(f"{ctx.site.name}: HTTP 404 from {resp.url}")
        raise resp.http_error()
    return resp


def get_page(ctx, path: str, params: dict, what: str) -> str:
    html = fetch(ctx, path, params).text()
    check_page(ctx, html, what)
    return html


def catalog(ctx) -> list[dict]:
    return parse.parse_forums(get_page(ctx, "forum.php", {}, "board index"))


def bare(name: str) -> str:
    """A board name as typed or matched: no 『』 brackets, no trailing 区, lower case."""
    return name.strip().strip("『』【】 ").removesuffix("区").lower()


def forum_id(ctx, ref: str) -> int:
    """A board id, board URL, or (looked up in the live catalog) part of a board name -> fid."""
    if (fid := parse.forum_ref(ref)) is not None:
        return fid
    want = bare(ref)
    found = catalog(ctx)
    if not found:
        raise RuntimeError(f"{ctx.site.name}: no boards found in the index (the layout may have changed)")
    hits = [r for r in found if want and want in r["name"].lower()]
    exact = [r for r in hits if bare(r["name"]) == want]
    top = [r for r in hits if r["parent"] is None]
    for pick in (hits, exact, top):
        if len(pick) == 1:
            return pick[0]["id"]
    names = ", ".join(f"{r['id']} {r['name']}" for r in hits[:8])
    error = ArgumentError if hits else NoSuchItem
    raise error(f"{'ambiguous' if hits else 'unknown'} board {ref!r}"
                     f"{': ' + names if hits else ''}; run `esum {ctx.site.name} forums`")


# --- board catalog -------------------------------------------------------------------------------------

@action(discuz, tier=Tier.READ)
def forums(ctx, query: str | None = None):
    """The board catalog from the forum index: board id (for `forum --id`), name and group;
    `--fields threads,posts,today,parent` adds thread and post counts, today's new posts and the
    parent board of a sub-board (sub-boards follow their parent).

    Boards hidden from guests are missing until you are logged in.

    Args:
        query: Only boards whose name or group contains this.
    """
    rows = catalog(ctx)
    if query:
        q = query.lower()
        rows = [r for r in rows if q in r["name"].lower() or q in r["group"].lower()]
    return Listing(name="forums", rows=rows, fields=("id", "name", "group"), about={"query": query},
                   empty=f"no board name or group contains {query!r}" if query else "no boards on the forum index",
                   next=[Next("forum", {"id": "<id>"}, "list a board's threads")])


# --- thread lists --------------------------------------------------------------------------------------

def listing(data: dict, about: dict, total: int | None = None, fields=("id", "title", "replies", "last_post")):
    return Listing(name="threads", rows=data["threads"], fields=fields, about=about, total=total,
                   more={"page": data["page"] + 1} if data["page"] < data["pages"] else None,
                   empty=f"no threads on page {data['page']}",
                   next=[Next("thread", {"id": "<id>"}, "read a thread")])


@action(discuz, tier=Tier.READ)
def forum(ctx, id: str, page: int = 1, order: Order = "lastpost", digest: bool = False, type: int | None = None):
    """One page of a board's thread list: stickies first (page 1), then threads.

    Each thread shows its id (for `thread`), title, replies and the latest reply's date;
    `--fields type,type_id,starter,views,pages` (or `--fields all`) adds the category tag and its
    id (for `--type`), starter, views, page count and more.

    Args:
        id: Board id, board URL, or part of a board name from `forums`.
        page: Page of the thread list.
        order: lastpost (latest reply first), dateline (newest threads), replies, views, or heats.
        digest: Only digest (精华) threads.
        type: Only threads of this category (the `type_id` column; the [tag] before a title).
    """
    fid = forum_id(ctx, id)
    if digest and type is not None:
        raise ArgumentError("pass --digest or --type, not both: the board filters by one of them")
    params = {"mod": "forumdisplay", "fid": fid, "page": page if page > 1 else None,
              "filter": "digest" if digest else "typeid" if type is not None else
              FILTERS[order] if order != "lastpost" else None,
              "typeid": type, "digest": 1 if digest else None, "orderby": order if order != "lastpost" else None}
    html = get_page(ctx, "forum.php", params, f"board {fid}")
    data = {"id": fid, **parse.parse_forum_page(html, ctx.http.url("/")),
            "url": ctx.http.url("forum.php", {"mod": "forumdisplay", "fid": fid})}
    if page > data["pages"] and not data["threads"]:
        raise RuntimeError(f"board {fid} has no page {page} (it has {data['pages']})")
    if page > data["pages"]:
        raise ArgumentError(f"board {fid} has no page {page} (it has {data['pages']})")
    about = {"id": fid, "name": data["name"], "order": order, "digest": digest or None, "type": type,
             "page": data["page"], "pages": data["pages"], "url": data["url"]}
    filtered = digest or type is not None
    return listing(data, about, total=None if filtered else data["threads_total"])   # the header counts the whole board


@action(discuz, tier=Tier.READ)
def latest(ctx, view: View = "new", page: int = 1):
    """Site-wide thread lists across all boards (the forum's 导读 pages).

    `new` lists the newest threads, `replied` the threads with the latest replies, `hot` the
    current hot threads and `digest` the newest digest (精华) threads. Each row shows its board;
    `--fields last_post` adds the latest reply's date.

    Args:
        view: new, replied, hot, or digest.
        page: Page of the list.
    """
    params = {"mod": "guide", "view": VIEWS[view], "page": page if page > 1 else None}
    html = get_page(ctx, "forum.php", params, f"{view} threads")
    cur, pages = parse.page_count(html)
    data = {"page": cur, "pages": pages, "threads": parse.parse_thread_rows(html, ctx.http.url("/"))}
    about = {"view": view, "page": cur, "pages": pages, "url": ctx.http.url("forum.php", params)}
    return listing(data, about, fields=("id", "title", "forum", "replies"))


# --- search --------------------------------------------------------------------------------------------

def search_page(ctx, params: dict, what: str):
    """One search request -> (html, final URL), waiting out one short flood-control notice."""
    for attempt in range(2):
        resp = fetch(ctx, "search.php", params, dedupe=False if attempt else None)   # a retry is sent again
        host = urlsplit(resp.url).hostname
        if host and host != urlsplit(ctx.site.base_url).hostname:
            raise SiteRefused(f"{ctx.site.name} sends searches to another site ({host}), not to its own "
                              f"search; search the web with `site:{urlsplit(ctx.site.base_url).hostname}` instead")
        html = resp.text()
        msg = parse.message(html)
        wait = parse.search_wait(msg) if msg else None
        if wait is not None and attempt == 0 and wait <= SEARCH_WAIT_MAX:
            time.sleep(wait + 1)
            continue
        if wait is not None:
            raise SiteRefused(f"{ctx.site.name} {what}: {msg} (wait {wait} s, then retry)")
        check_page(ctx, html, what)
        return html, resp.url
    raise AssertionError("unreachable")


@action(discuz, tier=Tier.READ)
def search(ctx, query: str, forum: list[str] | None = None, author: str | None = None,
           order: SearchOrder = "lastpost", page: int = 1, search_id: int | None = None):
    """The forum's own thread search: thread id (for `thread`), title, replies and date;
    `--fields author,forum,views,excerpt` adds the starter, board, views and the opening post's
    first lines.

    Discuz matches words in thread titles. The site counts each new search against a per-user
    limit (a guest may get one per 10 seconds or so; ESUM waits out a short one once), and caps the
    hits it keeps (often 100). Later pages reuse the first search's `--search-id`, which costs no
    new search; the next-page hint carries it.

    Args:
        query: Words in the thread title (Discuz splits on spaces).
        forum: Only these boards: ids, URLs or name parts from `forums`.
        author: Only threads started by this user name.
        order: lastpost (latest reply first), dateline (newest threads), replies or views.
        page: Result page.
        search_id: The id of an earlier identical search, from a next-page hint.
    """
    what = f"search {query!r}"
    html = None
    if search_id is None:
        fids = [forum_id(ctx, f) for f in forum or []]
        params = {"mod": "forum", "srchtxt": query, "searchsubmit": "yes", "srchfid[]": fids or None,
                  "srchuname": author, "orderby": order, "ascdesc": "desc"}
        html, url = search_page(ctx, params, what)
        search_id = parse.search_id(url)
        if page > 1 and search_id is None:
            raise SiteRefused(f"{ctx.site.name} {what}: the site gave no search id, so page {page} can't be read")
    if html is None or page > 1:
        html, url = search_page(ctx, {"mod": "forum", "searchid": search_id, "orderby": order, "ascdesc": "desc",
                                      "searchsubmit": "yes", "page": page if page > 1 else None}, what)
    data = parse.parse_search(html, ctx.http.url("/"))
    if page > data["pages"] and not data["threads"]:
        raise ArgumentError(f"{what} has no page {page} (it has {data['pages']})")
    about = {"query": query, "forum": ",".join(forum) if forum else None, "author": author, "order": order,
             "page": data["page"], "pages": data["pages"], "search_id": search_id}
    return Listing(name="threads", rows=data["threads"], fields=("id", "title", "replies", "date"), about=about,
                   total=data["total"],
                   more={"page": data["page"] + 1, "search_id": search_id} if data["page"] < data["pages"] else None,
                   empty=f"no thread title matches {query!r}",
                   next=[Next("thread", {"id": "<id>"}, "read a thread")])


# --- the thread ----------------------------------------------------------------------------------------

def thread_page(ctx, tid: int, page: int) -> parse.ThreadPage | None:
    """One thread page; None when the site wants a login for it (page 1 raises instead)."""
    params = {"mod": "viewthread", "tid": tid, "page": page if page > 1 else None}
    html = fetch(ctx, "forum.php", params).text()
    if page > 1 and parse.needs_login(html):
        return None
    check_page(ctx, html, f"thread {tid}")
    tp = parse.parse_thread_page(html)
    if page == 1 and not tp.posts:
        raise RuntimeError(f"{ctx.site.name}: no posts found on {ctx.http.url('forum.php', params)}")
    return tp


@action(discuz, tier=Tier.READ)
def thread(ctx, id: str, mode: Mode = "medium", from_: int | None = None, to: int | None = None):
    """A thread in compact-post format.

    Posts are numbered by floor as on the site (#1 is the OP; deleted floors leave gaps).
    `↩#M` marks a post that quotes floor #M (when that floor was read), and quotes are shortened
    to one `> @user: …` line. `👍` counts the members who rated the post (评分). Posts the site
    lifts above the thread have no floor number in the page, so they follow the thread on their
    own. The floor total is an estimate until the last page is read. `medium` reads
    the first and last 30 floors, `full` reads everything (one request per page, usually 10
    floors per page). Pages the site shows only to members are left out with a note saying so.

    Args:
        id: Thread id or thread URL (a URL of page 2 or later reads that page only).
        mode: medium (first and last 30 floors) or full.
        from_: First floor number to read (overrides mode).
        to: Last floor number to read (overrides mode).
    """
    tid, url_page = parse.thread_ref(str(id))
    first = thread_page(ctx, tid, 1)
    assert first is not None   # page 1 raises instead
    per_page = max((p.number for p in first.posts), default=1) if first.pages > 1 else None
    if per_page:   # floors are paged by position, so the last floor sits on the last page
        total = min(max((first.replies or 0) + 1, (first.pages - 1) * per_page + 1), first.pages * per_page)
    else:
        total = max(p.number for p in first.posts)
    wanted = set(select_numbers(total, mode, from_, to))
    if url_page and url_page > 1 and from_ is None and to is None:
        if url_page > first.pages:
            raise ArgumentError(f"thread {tid} has no page {url_page} (it has {first.pages})")
        wanted = set(range((url_page - 1) * per_page + 1, url_page * per_page + 1)) if per_page else set()
    login_wall = False

    def load(n: int) -> parse.ThreadPage | None:
        nonlocal login_wall
        page = None if login_wall else thread_page(ctx, tid, n)
        login_wall = page is None
        return page

    read = read_pages(load, {1: first}, wanted,
                      lambda n: (n - 1) // per_page + 1 if per_page else 1,
                      lambda p: ((r.number, r) for r in p.posts))
    pages, locked = read.pages, read.unavailable
    raw = [r for n, r in read.floors.items() if n in wanted]
    if locked and not raw:
        raise ctx.login_needed(detail=f"thread {tid}: the site shows page(s) {spans(locked)} to members only")
    if last := pages.get(first.pages):
        total = max([total, *(r.number for r in last.posts)])
    op = next((r.user for r in first.posts if r.number == 1), None)
    url = ctx.http.url("forum.php", {"mod": "viewthread", "tid": tid})
    posts: list[Post] = parse.to_posts(raw, op, lambda pid: ctx.http.url(
        "forum.php", {"mod": "redirect", "goto": "findpost", "ptid": tid, "pid": pid}))
    lost = {n for p in locked for n in range((p - 1) * per_page + 1, p * per_page + 1)}
    lifted = first.featured if any(r.number in wanted for r in first.posts) else []
    coverage_spans = [(a, b, "members only") for a, b in to_spans(lost & wanted)]
    if lifted:   # lifted implies page 1 has floors
        top = max(r.number for r in first.posts)
        coverage_spans += [(a, b, "deleted, or among the recommended posts below")
                           for a, b in to_spans(n for n in wanted if n <= top)]
    view = thread_view(posts, coverage_spans + read_spans(wanted, total), count=total)
    title = first.title or f"Thread {tid}"
    if first.type:
        title = f"[{first.type}] {title}"
    board = first.forum and f"Board: {first.forum}" + (f" ({first.forum_id})" if first.forum_id else "")
    line = " · ".join(b for b in (board, first.views is not None and f"{first.views} views",
                                  f"{'about ' if first.pages not in pages else ''}{total} floors in {first.pages} page(s)")
                     if b)
    notes = [line]
    if locked:
        notes.append(f"Page(s) {spans(locked)} not read: the site shows them to members only. Ask the user to "
                     f"log in themselves in {ctx.login_needed().where}, then retry.")
    suffix = ""
    featured = parse.to_posts(lifted, op, lambda pid: ctx.http.url(
        "forum.php", {"mod": "redirect", "goto": "findpost", "ptid": tid, "pid": pid}))
    if featured:
        suffix += "\n\nRecommended posts (the site lifts these above the thread, without their floor numbers):\n"
        suffix += "\n".join(format_post(p).replace(" #0]", "]", 1) for p in featured)
    data = {"id": tid, "title": first.title, "type": first.type, "url": url, "forum": first.forum,
            "forum_id": first.forum_id, "views": first.views, "replies": first.replies, "total": total,
            "pages": first.pages, "per_page": per_page, "members_only_pages": locked,
            "featured": featured}
    return view.result(title, url=url, data=data, notes=notes, suffix=suffix,
                       next=view.range_steps({"id": str(tid)}))


def spans(numbers: list[int]) -> str:
    """[2, 3, 4, 9] -> "2–4, 9"."""
    return ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in to_spans(numbers))
