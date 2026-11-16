"""NGA (bbs.nga.cn): a large Chinese forum on games (World of Warcraft, mobile and console games)
with a busy general section (网事杂谈: news, life, tech, deals).

Reads NGA's own JSON pages (`thread.php`, `read.php`, `app_api.php` with `__output=11`). Guests can
open almost nothing ("访客不能直接访问"; the guest pass is a click on the page), so reads go over T2
with the ESUM browser's cookies: the user logs in there once, and ESUM reads what that account
sees. Only the forum directory opens without an account. NGA has no Cloudflare in front of it, so
there is no T3 fallback.
"""

from __future__ import annotations

import json
import math
import re
from typing import Literal

from esum import (ArgumentError, NoSuchItem, SiteRefused, HttpError, Listing, Mode, Next, Tier, Transport,
                  action, thread_view, read_pages, read_spans, select_numbers, site)

from . import parse

FORMAT = 1
PORTED_FROM = ("xgoose apps/chrome/skills/nga.xgs.js (github.com/xGoose-dev/xgoose): "
               "the JSON endpoints and parameters, BBCode quote/reply headers and attachment URLs")
EXTRAS = ()

nga = site(
    name="nga",
    base_url=parse.BASE,
    transport=Transport.BROWSER_HTTP,   # T2 with the browser's login: guests can't read
    label={"en": "NGA (bbs.nga.cn): large Chinese forum on games (World of Warcraft, mobile and console "
                 "games) with a busy general section (网事杂谈: news, life, tech, deals)",
           "zh": "NGA 玩家社区：游戏论坛（魔兽世界、手游、主机游戏），以及网事杂谈等综合版面"},
    extras={"min_interval": 1.5},
)

Order = Literal["last-reply", "newest"]
Period = Literal["day", "week", "month"]
DAYS = {"day": 1, "week": 7, "month": 30}
_FIDS = re.compile(r"^-?\d+(,-?\d+)*$")


def get_json(ctx, path: str, what: str, params: dict) -> dict:
    """One NGA JSON page. The site's messages become errors: the guest and login messages are
    LOGIN NEEDED, "找不到…" / "…不存在" is not found, the rest pass through."""
    params = {**params, "__output": 11}
    for attempt in (1, 2):
        # never reuse a reply: a cut-short one would come back again (no action reads a URL twice)
        resp = ctx.http.get(path, params=params, check=False, dedupe=False)
        body = resp.text()
        try:
            payload = json.loads(body, strict=False)   # posts may hold raw control characters
            break
        except ValueError:
            if not resp.ok:
                raise resp.http_error(body[:2000])
            if attempt == 2:   # NGA sometimes cuts a reply short; one fresh read, then give up
                raise RuntimeError(f"nga {what}: the reply is not complete JSON twice in a row "
                                   f"({len(body)} characters from {resp.url})")
    msg = parse.error_of(payload) if isinstance(payload, dict) else None
    if msg and parse.LOGIN_MESSAGES.search(msg):
        raise ctx.login_needed(detail=f"nga {what}: {msg} (log in to bbs.nga.cn in that window, then retry)")
    if msg and parse.NOT_FOUND.search(msg):
        error = NoSuchItem(f"nga {what}: {msg}")
        if parse.NO_BOARD.search(msg):
            error.help = "No such board or collection: `esum nga forums --query <name>` lists their fid and stid"
        raise error
    if msg:
        raise SiteRefused(f"nga {what}: {msg}")
    if not resp.ok:
        raise resp.http_error(body[:2000])
    if not isinstance(payload, dict):
        raise RuntimeError(f"nga {what}: unexpected reply from {resp.url}")
    return payload


@action(nga, tier=Tier.READ)
def forums(ctx, query: str | None = None):
    """The forum directory: every board (fid) and collection (stid) by category and group.

    Use a fid or stid with `listing`, and fids with `search`. Without a query the list is long
    (about 700 boards); a query matches the name, description, category or group.

    Args:
        query: Text to look for, e.g. 魔兽, 原神, 杂谈.
    """
    found = parse.forums(get_json(ctx, "/app_api.php", "forum directory", {"__lib": "home", "__act": "category"}))
    if not found:
        raise RuntimeError("nga: the forum directory is empty (the format may have changed)")
    hits = parse.match_forums(found, query)
    return Listing(name="forums", rows=hits, fields=("fid", "stid", "name", "group"),
                   about={"query": query}, total=len(found), empty=f"no board matches {query!r}",
                   next=[Next("listing", {"fid": "<fid>"}, "list a board's topics (or `--stid <stid>` for a collection)")])


TOPIC_NEXT = [Next("thread", {"id": "<id>"}, "read a topic")]


def topics(data: dict, page: int, about: dict, url: str, fields: tuple[str, ...], empty: str) -> Listing:
    """A page of topic rows; `--json` keeps the page's own keys (total, per_page, forum, fid, page, pages, url)."""
    pages = math.ceil(data["total"] / data["per_page"]) if data["total"] else page
    data.update(page=page, pages=pages, url=url)
    return Listing(name="threads", rows=data["threads"], fields=fields,
                   about={**about, "page": f"{page} of {pages}", "times": "UTC", "url": url}, total=data["total"] or None,
                   more={"page": page + 1} if page < pages else None, empty=empty, next=TOPIC_NEXT, data=data)


@action(nga, tier=Tier.READ)
def listing(ctx, fid: int | None = None, stid: int | None = None, page: int = 1, order: Order = "last-reply",
            recommend: bool = False):
    """One page of topics of a board or a collection: 35 a page, plus the sub-boards' topics the site
    mixes in, so a board's page often has about 50 rows.

    Columns: topic id (for `thread`), title, board, replies and start date (UTC); `--fields` adds
    author, fid, last_reply, last_reply_by and url. A board's page also lists its sub-boards'
    topics; those rows name their board in `forum`. A row the account may not open has the title
    `(hidden from this account: …)`.

    Args:
        fid: Board id from `forums` (may be negative, e.g. -7 for 网事杂谈).
        stid: Collection (合集) id from `forums`, instead of fid.
        page: Page of the list.
        order: last-reply (latest reply first) or newest (newest topics first).
        recommend: Only the board's featured topics (精华).
    """
    if (fid is None) == (stid is None):
        raise ArgumentError("pass one of --fid (a board) or --stid (a collection); see `esum nga forums`")
    page = max(page, 1)
    params = {"fid": fid} if fid is not None else {"stid": stid}
    params.update(page=page, order_by="postdatedesc" if order == "newest" else "lastpostdesc")
    if recommend:
        params["recommend"] = 1
    what = f"board {fid}" if fid is not None else f"collection {stid}"
    data = parse.topic_page(get_json(ctx, "/thread.php", what, params))
    about = {"board": data["forum"] or what, "featured": "yes" if recommend else None, "sort": order}
    return topics(data, page, about, ctx.http.url("/thread.php", params), ("id", "title", "forum", "replies", "date"),
                  f"no topics on page {page} of {what}")


@action(nga, tier=Tier.READ)
def search(ctx, query: str, fid: str | None = None, content: bool = False, page: int = 1):
    """Search topic titles, site-wide or in some boards, newest first; 35 rows a page.

    Needs a logged-in account with reputation (威望) above 0. Rows show the board's fid, not its
    name.

    Args:
        query: Words to look for.
        fid: Board id, or several comma-separated (write `--fid=-7,7` when it starts with a minus).
        content: Search the opening posts' text too, not just titles (needs a fid).
        page: Page of results.
    """
    if fid is not None and not _FIDS.match(fid.replace(" ", "")):
        raise ArgumentError(f"not a board id or a comma-separated list of them: {fid!r}")
    if content and fid is None:
        raise ArgumentError("--content needs --fid: NGA searches post text only within boards")
    page = max(page, 1)
    params: dict = {"key": query, "page": page, "__inchst": "UTF8"}
    if fid is not None:
        params["fid"] = fid.replace(" ", "")
    if content:
        params["content"] = 1
    try:
        payload = get_json(ctx, "/thread.php", f"search {query!r}", params)
    except SiteRefused as e:
        if parse.MEMBERS_ONLY not in str(e):
            raise
        # NGA says the same to a guest and to a member with 威望 0. A board page tells them apart:
        # a guest gets LOGIN NEEDED here; a member gets the site's message.
        get_json(ctx, "/thread.php", "login check", {"fid": -7})
        raise
    data = parse.topic_page(payload)
    about = {"query": query, "fid": params.get("fid", "site-wide"), "in": "titles and text" if content else "titles"}
    url = ctx.http.url("/thread.php", {k: v for k, v in params.items() if k != "__inchst"})
    return topics(data, page, about, url, ("id", "title", "fid", "replies", "date"),
                  f"no topics match {query!r}" + (f" in fid {fid}" if fid else ""))


@action(nga, tier=Tier.READ)
def hot(ctx, fid: int, period: Period = "day"):
    """A board's hot topics over the last day, week or month.

    Args:
        fid: Board id from `forums`.
        period: day, week or month.
    """
    rows = parse.hot_topics(get_json(ctx, "/app_api.php", f"hot topics of board {fid}",
                                     {"__lib": "subject", "__act": "hot", "fid": fid, "days": DAYS[period]}))
    url = f"{parse.BASE}/thread.php?fid={fid}"
    return Listing(name="threads", rows=rows, fields=("id", "title", "replies", "date"),
                   about={"fid": fid, "period": period, "times": "UTC", "url": url},
                   empty=f"no hot topics in board {fid} for the last {period}", next=TOPIC_NEXT,
                   data={"fid": fid, "period": period, "threads": rows, "url": url})


@action(nga, tier=Tier.READ)
def thread(ctx, id: str, mode: Mode = "medium", from_: int | None = None, to: int | None = None):
    """A topic and its replies in compact-post format, numbered by NGA's floors (楼).

    The opening post is #0 and floor N is #N. Pages hold 20 floors; only the pages holding wanted
    floors are fetched (plus the last one). 👍 is the post's score (赞). A quote is one
    `> @user #N: …` line (300 characters; read #N for the rest); `#N` is known only when floor N
    is on a fetched page, otherwise the line names the quoted post's page. `↩#M` marks a reply to
    floor M; a reply to a floor not fetched starts with `↩ @user (page P)`. Pictures are
    `[image: URL]`; stickers (`[s:ac:…]`) show as their name, `:name:`. Missing floors are hidden or
    deleted. NGA's JSON names authors `UID:<n>`; real names come from the topic row and from quote
    headers, where known.

    Args:
        id: Topic id (tid), a read.php URL (bbs.nga.cn, ngabbs.com, nga.178.com), or pid:<n> for
            the topic holding one post.
        mode: medium (the opening post plus the first and last 30 replies) or full.
        from_: First floor to read (overrides mode; 0 includes the opening post).
        to: Last floor to read (overrides mode).
    """
    tid, pid = parse.thread_ref(id)
    if tid is None:
        tid = parse.thread_page(get_json(ctx, "/read.php", f"post {pid}", {"pid": pid})).tid
        if not tid:
            raise RuntimeError(f"nga: post {pid} names no topic (the site data may have changed)")

    def load(n: int) -> parse.ThreadPage:
        return parse.thread_page(get_json(ctx, "/read.php", f"topic {tid}" + (f" page {n}" if n > 1 else ""),
                                          {"tid": tid, "page": n}))

    first = load(1)
    last_floor = max(first.total - 1, 0)
    last_page = parse.page_of(last_floor)
    pages = {1: first}
    if last_page > 1:
        pages[last_page] = load(last_page)
    wanted = set(select_numbers(last_floor, mode, from_, to))
    read = read_pages(load, pages, wanted, parse.page_of, lambda p: ((f.floor, f) for f in p.floors))
    names: dict[int, str] = {}
    for n in sorted(read.pages):
        names.update(read.pages[n].names)
    floors = {f.pid: f.floor for f in read.floors.values()} | {0: 0}
    if 0 in read.floors and (from_ is None or from_ <= 0):
        wanted.add(0)
    view = thread_view([parse.to_post(f, tid, first.author_uid, names, floors)
                        for n, f in read.floors.items() if n in wanted],
                       read_spans(wanted, last_floor), count=last_floor + 1)
    url = parse.topic_url(tid)
    line = " · ".join(b for b in (first.forum and f"Board: {first.forum} [fid {first.fid}]",
                                  f"{last_floor} replies in {last_page} page(s) of {parse.PER_PAGE}",
                                  first.author and f"OP {first.author}", "Times: UTC") if b)
    data = {"id": tid, "title": first.title, "url": url, "fid": first.fid, "forum": first.forum,
            "replies": last_floor, "pages": last_page}
    return view.result(first.title, url=url, data=data, notes=[line], next=view.range_steps({"id": str(tid)}))
