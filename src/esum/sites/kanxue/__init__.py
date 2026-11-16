"""看雪 Kanxue (bbs.kanxue.com): a heavily customized Xiuno BBS read from its HTML pages.

Guests get the board catalog, board thread lists, whole threads and the hot-thread ranking.
Search, the site-wide latest/digest lists and some boards need a logged-in account, so reads go
over T2 with the ESUM browser's cookies: a "请先登录" page is LOGIN NEEDED, and after the user
logs in there once, ESUM reads what that account sees. A board the account's user group may not
open ("无权访问") stays the site's own error: logging in helps only if the account has access.
"""

from __future__ import annotations

from typing import Literal

from esum import (NoSuchItem, SiteRefused, Listing, Mode, Next, Post, Tier, Transport, action,
                  thread_view, read_pages, read_spans, select_numbers, site)

from . import parse

FORMAT = 1
PORTED_FROM = "original work (no external source)"
EXTRAS = ()

kanxue = site(
    name="kanxue",
    base_url="https://bbs.kanxue.com",
    transport=Transport.BROWSER_HTTP,  # T2 with the browser's cookies: login unlocks more
    label={"en": "看雪 Kanxue (bbs.kanxue.com): Chinese software-security and reverse-engineering forum "
                 "(Android/iOS, Windows internals, unpacking, CTF, AI security)",
           "zh": "看雪论坛：软件安全与逆向工程社区"},
    extras={"min_interval": 1.0},
)

Order = Literal["last-reply", "newest"]
Period = Literal["week", "month", "quarter", "year"]
PERIODS = {"week": 3, "month": 0, "quarter": 1, "year": 2}   # the site's hotlist tab numbers


def get_page(ctx, path: str, what: str) -> str:
    """A page's HTML; the site's message pages ("主题不存在！", "请先登录") become errors."""
    html = ctx.http.get_text(path)
    if parse.is_verification_page(html):
        raise ctx.login_needed(detail=f"kanxue {what}: the site asks guests to pass a check here")
    msg = parse.message_of(html)
    if msg is None:
        return html
    if parse.LOGGED_OUT_MESSAGES.search(msg):
        raise ctx.login_needed(detail=f"kanxue {what}: {msg}")
    if parse.LOGIN_MESSAGES.search(msg):
        raise SiteRefused(f"kanxue {what}: {msg} (the account's user group can't open it; as a "
                              f"guest, logging in in the ESUM browser window may help)")
    raise NoSuchItem(f"kanxue {what}: {msg}")


@action(kanxue, tier=Tier.READ)
def forums(ctx):
    """The board catalog from the home page: id (for `forum --id`), name and topic count;
    `--fields category,description,parent` adds the rest.

    Sub-boards follow their parent board (`parent` holds its id); some of them (KCTF submissions,
    staff) are closed to guests.
    """
    found = parse.parse_forums(get_page(ctx, "/", "home page"))
    if not found:
        raise RuntimeError("kanxue: no boards found on the home page (the layout may have changed)")
    return Listing(name="forums", rows=found, fields=("id", "name", "topics"),
                   next=[Next("forum", {"id": "<id>"}, "list a board's threads")])


def forum_id(ctx, ref: str) -> int:
    """A board id, board URL, or board name (from the home page catalog) -> id."""
    if (fid := parse.forum_ref(ref)) is not None:
        return fid
    want = ref.strip().casefold()
    found = parse.parse_forums(get_page(ctx, "/", "home page"))
    if not found:
        raise RuntimeError("kanxue: no boards found on the home page (the layout may have changed)")
    for f in found:
        if f["name"].casefold() == want:
            return f["id"]
    raise NoSuchItem(f"unknown board {ref!r}: pass a board id, board URL or exact name (see `esum kanxue forums`)")


@action(kanxue, tier=Tier.READ)
def forum(ctx, id: str, page: int = 1, order: Order = "last-reply", digest: bool = False):
    """One page of a board's thread list (30 threads; stickies first on page 1).

    Each row shows the thread id (for `thread`), title, replies and the latest reply's date;
    `--fields author,date,views,pages,digest` (or `--fields all`) adds the starter and start date,
    views, page count and flags: `sticky`, `digest` (the site's 关注 / 优秀 / 精华 marks) and
    `attachment`. Recent dates are relative on the site and are resolved to China time (UTC+8).

    Args:
        id: Board id (e.g. 4 for 逆向工程, 161 for Android安全), board URL, or exact board name.
        page: Page of the thread list.
        order: last-reply (latest reply first) or newest (newest threads first).
        digest: Only digest (精华) threads.
    """
    fid = forum_id(ctx, id)
    path = f"/forum-{fid}-{max(page, 1)}.htm?orderby={'tid' if order == 'newest' else 'lastpid'}&digest={int(digest)}"
    data = parse.parse_forum_page(get_page(ctx, path, f"board {fid}"))
    data["id"] = fid
    data["url"] = ctx.http.url(path)
    if page > data["pages"] and not data["threads"]:
        raise RuntimeError(f"kanxue board {fid} has no page {page} (it has {data['pages']})")
    about = {"id": fid, "name": data["name"], "order": order, "digest": digest or None, "page": data["page"],
             "pages": data["pages"], "times": "UTC+8", "url": data["url"]}
    return Listing(name="threads", rows=data["threads"], fields=("id", "title", "replies", "last_date"), about=about,
                   total=None if digest else data["topics"], more={"page": data["page"] + 1} if data["page"] < data["pages"] else None,
                   empty=f"board {fid} has no threads on page {data['page']}",
                   next=[Next("thread", {"id": "<id>"}, "read a thread")])


@action(kanxue, tier=Tier.READ)
def hot(ctx, period: Period = "week", limit: int = 50):
    """The site-wide hot-thread ranking (热帖排行榜) by the site's heat index.

    Args:
        period: week, month, quarter or year.
        limit: Rows to show (the site ranks up to about 50; longer periods often fewer).
    """
    rows = parse.parse_hot(get_page(ctx, f"/thread-hotlist-all-{PERIODS[period]}.htm", "hot list"))
    shown = rows[:limit]
    return Listing(name="threads", rows=shown, fields=("rank", "id", "title", "heat"), about={"period": period},
                   total=len(rows), empty=f"the {period} hot list is empty",
                   next=[Next("thread", {"id": "<id>"}, "read a thread")])


def thread_page(ctx, tid: int, page: int) -> parse.ThreadPage:
    path = f"/thread-{tid}.htm" if page == 1 else f"/thread-{tid}-{page}.htm"
    return parse.parse_thread_page(get_page(ctx, path, f"thread {tid}"), tid, page)


@action(kanxue, tier=Tier.READ)
def thread(ctx, id: str, mode: Mode = "medium", from_: int | None = None, to: int | None = None):
    """A thread in compact-post format, numbered by floor (#1 is the opening post).

    Pages hold 25 floors and only the pages holding wanted floors are fetched. `↩#M` marks a reply
    that quotes floor #M (matched by the quoted user and text among the floors read); quotes are
    shortened to one `> @user: …` line. Missing floors are posts the site deleted or hides. When
    the site cuts the opening post for guests (full text only for members who reply or like), the
    post ends with a `[hidden: …]` note; obfuscated links show as `[link hidden from guests]` and
    attachments by name only.

    Args:
        id: Thread id or thread URL (thread-<id>.htm, thread-<id>-<page>.htm).
        mode: medium (first and last 30 floors) or full.
        from_: First floor to read (overrides mode).
        to: Last floor to read (overrides mode).
    """
    tid = parse.thread_ref(id)
    first = thread_page(ctx, tid, 1)
    total = first.total
    wanted = set(select_numbers(total, mode, from_, to))
    read = read_pages(lambda n: thread_page(ctx, tid, n) if n <= first.pages else None,
                      {1: first}, wanted, lambda n: (n - 1) // parse.PER_PAGE + 1,
                      lambda p: ((f.floor, f) for f in ([p.op] if p.op else []) + p.posts))
    raw = [f for n, f in read.floors.items() if n in wanted]
    posts: list[Post] = parse.to_posts(raw, tid, first.op.user if first.op else None)
    view = thread_view(posts, read_spans(wanted, total), count=total)
    url = parse.floor_url(tid, 1)
    line = " · ".join(b for b in (first.forum and f"Board: {first.forum} [id {first.forum_id}]",
                                  f"{total} floors in {first.pages} page(s) of {parse.PER_PAGE}",
                                  first.views is not None and f"{first.views} views",
                                  "Times: UTC+8", first.op_truncated and "opening post cut for guests") if b)
    data = {"id": tid, "title": first.title, "url": url, "forum": first.forum, "forum_id": first.forum_id,
            "total": total, "pages": first.pages, "views": first.views, "op_truncated": first.op_truncated}
    return view.result(first.title, url=url, data=data, notes=[line], next=view.range_steps({"id": str(tid)}))
