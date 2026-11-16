"""一亩三分地 (1point3acres.com): the JSON API behind the site's new pages.

www.1point3acres.com answers plain clients with a Cloudflare challenge, but its API hosts
(api.1point3acres.com, and trpc.1point3acres.com for search) answer directly. They serve the
Discuz! data the forum keeps (tid, fid, pid, unix dates) with BBCode bodies. Requests go through
curl_cffi with the ESUM browser's cookies (T2), so once the user has logged in there the API
answers as that member: content locked behind points (积分, `[hide=N]`) and member-only lists then
read as the site shows them. The API says `请先登录` (log in first) where a guest isn't allowed.
"""

from __future__ import annotations

import json
from typing import Literal

from esum import (ArgumentError, Listing, Mode, Next, NoSuchItem, SiteRefused, Tier, Transport, action, read_pages,
                  read_spans, select_numbers, site, thread_view)

from . import parse

FORMAT = 1
PORTED_FROM = "xgoose apps/chrome/skills/onepoint3acres.xgs.js (github.com/xGoose-dev/xgoose)"
EXTRAS = ()

API = "https://api.1point3acres.com"
TRPC = "https://trpc.1point3acres.com/trpc/"
LIST_SIZE = 30   # threads per list page (the API's ceiling)

onepoint3acres = site(
    name="onepoint3acres",
    base_url=API,
    transport=Transport.BROWSER_HTTP,   # T2 with the browser's login: points-locked posts read as the member
    label={
        "en": "一亩三分地 (1point3acres.com): Chinese community for studying, working and living in the US; "
              "interview reports (面经), offers and salaries, job hunting, visas and green cards, admissions",
        "zh": "一亩三分地：留学、求职、面经、工作签证与移民、海外生活的华人社区",
    },
    extras={"min_interval": 1.0},
)

View = Literal["hot", "new"]
SearchType = Literal["hybrid", "keywords", "semantic"]


def get(ctx, path: str, what: str, params: dict | None = None) -> dict:
    """One API GET; `errno` != 0 becomes LOGIN NEEDED (请先登录), NoSuchItem (不存在) or a raw refusal."""
    resp = ctx.http.request("GET", path, params=params, headers={"Accept": "application/json"}, check=False)
    try:
        data = resp.json()
    except ValueError:
        raise resp.http_error() if resp.status >= 400 else RuntimeError(
            f"1point3acres {what}: the API sent no JSON (the site may have changed)") from None
    if isinstance(data, dict) and data.get("errno"):
        msg = str(data.get("msg") or f"errno {data['errno']}")
        if "登录" in msg:
            raise ctx.login_needed(detail=f"{what}: {msg}")
        if "不存在" in msg or resp.status == 404:
            raise NoSuchItem(f"1point3acres {what}: {msg}")
        raise SiteRefused(f"1point3acres {what}: {msg}")
    if resp.status >= 400:
        raise resp.http_error()
    return data


def catalog(ctx) -> list[dict]:
    return parse.forum_rows(get(ctx, "/api/forums", "board list").get("forums") or [])


def forum_id(ctx, ref: str, rows: list[dict] | None = None) -> tuple[int, dict | None]:
    """A board id, or part of a board name looked up in the catalog -> (fid, its row when looked up)."""
    ref = str(ref).strip()
    if ref.isdigit() and rows is None:
        return int(ref), None
    rows = rows if rows is not None else catalog(ctx)
    if ref.isdigit():
        return int(ref), next((r for r in rows if r["id"] == int(ref)), None)
    if not rows:
        raise RuntimeError("1point3acres: the board list came back empty (the API may have changed)")
    want = ref.lower()
    hits = [r for r in rows if want and want in r["name"].lower()]
    exact = [r for r in hits if r["name"].lower() == want]
    for pick in (hits, exact):
        if len(pick) == 1:
            return pick[0]["id"], pick[0]
    if not hits:
        raise NoSuchItem(f"unknown board {ref!r}; run `esum onepoint3acres forums`")
    names = ", ".join(f"{r['id']} {r['name']}" for r in hits[:8])
    raise ArgumentError(f"ambiguous board {ref!r}: {names}; pass the board id")


def threads(name: str, rows: list[dict], about: dict, page: int, empty: str, fields=None) -> Listing:
    return Listing(name=name, rows=rows, fields=fields or ("id", "title", "replies", "last_post"), about=about,
                   more={"page": page + 1} if len(rows) >= LIST_SIZE else None, empty=empty,
                   next=[Next("thread", {"id": "<id>"}, "read a thread with its replies")])


# --- boards --------------------------------------------------------------------------------------------

@action(onepoint3acres, tier=Tier.READ)
def forums(ctx, query: str | None = None):
    """The board catalog: board id (for `forum --id`), name and group; `--fields parent,today,types`
    adds the parent of a sub-board, today's new posts and the board's thread categories
    (`typeid name; …`, for `forum --type`).

    Args:
        query: Only boards whose name or group contains this (e.g. 面经, 签证, 租房).
    """
    rows = catalog(ctx)
    if query:
        q = query.lower()
        rows = [r for r in rows if q in r["name"].lower() or q in r["group"].lower()]
    return Listing(name="forums", rows=rows, fields=("id", "name", "group"), about={"query": query},
                   empty=f"no board name or group contains {query!r}" if query else "the board list is empty",
                   next=[Next("forum", {"id": "<id>"}, "list a board's threads")])


@action(onepoint3acres, tier=Tier.READ)
def forum(ctx, id: str, page: int = 1, type: str | None = None):
    """One page of a board's threads, latest reply first: thread id (for `thread`), title, replies
    and the latest reply's date (UTC); `--fields type,author,date,views,url` adds more. `type`
    (the thread's category) exists only on boards with categories, and `views` only on boards that
    show view counts (the 面经 and 晒工资 boards hide them); `--fields all` lists what a page has.

    Args:
        id: Board id, or part of a board name from `forums` (海外面经, 签证).
        page: Page of the list (30 threads a page).
        type: Only threads of this category: its typeid or name, from `forums --fields types`.
    """
    fid, row = forum_id(ctx, id, None if type is None or type.isdigit() else catalog(ctx))
    typeid = None
    if type is not None:
        if type.isdigit():
            typeid = int(type)
        else:
            types = parse.split_types((row or {}).get("types") or "")
            found = [k for k, v in types.items() if type.lower() in v.lower()]
            if len(found) != 1:
                listed = ", ".join(f"{k} {v}" for k, v in types.items()) or "none"
                raise ArgumentError(f"board {fid} has {'several' if found else 'no'} categories matching {type!r} "
                                    f"(categories: {listed})")
            typeid = found[0]
    data = get(ctx, f"/api/forums/{fid}/threads", f"board {fid}", {"pg": page, "ps": LIST_SIZE, "typeid": typeid})
    rows = [parse.thread_row(t) for t in data.get("threads") or []]
    name = row["name"] if row else (rows[0]["forum"] if rows else None)
    about = {"id": fid, "name": name, "type": type, "page": page,
             "url": f"{parse.SITE}/bbs/forum-{fid}-1.html"}
    if any(r["views"] is None for r in rows):
        about["views"] = "null where the site hides a thread's view count"
    return threads("threads", rows, about, page, f"no threads on page {page} of board {fid}")


@action(onepoint3acres, tier=Tier.READ)
def latest(ctx, view: View = "hot", page: int = 1):
    """Site-wide threads from the home page: `hot` (the current hot list) or `new` (newest threads).
    Each row shows its board; `--fields last_post` adds the latest reply's date.

    Args:
        view: hot or new.
        page: Page of the list (30 threads a page).
    """
    data = get(ctx, "/api/threads", f"{view} threads",
               {"type": view, "pg": page, "ps": LIST_SIZE, "includes": "forum_name"})
    rows = [parse.thread_row(t) for t in data.get("threads") or []]
    return threads("threads", rows, {"view": view, "page": page}, page, f"no {view} threads on page {page}",
                   fields=("id", "title", "forum", "replies"))


# --- search --------------------------------------------------------------------------------------------

@action(onepoint3acres, tier=Tier.READ)
def search(ctx, query: str, type: SearchType = "hybrid", sort: Literal["relevance", "newest"] = "relevance",
           forum: list[int] | None = None, days: int | None = None, cursor: int | None = None):
    """The site's own search over threads: thread id (for `thread`), title, board and date;
    `--fields replies,views,author,excerpt` adds more (`excerpt` is the opening post's start).

    `keywords` matches words, `semantic` matches meaning, `hybrid` (the site's default) mixes
    both. The site gives no hit count; 20 hits a page, and the next-page hint carries the cursor.
    For a guest the search leaves out the interview-report boards (面经: 145 海外面经, 259, 335,
    289); list those with `forum --id 145` instead.

    Args:
        query: Search words (租车, H1B 抽签, Meta onsite).
        type: hybrid, keywords or semantic.
        sort: relevance, or newest first.
        forum: Only these board ids (see `forums`).
        days: Only threads from the last N days.
        cursor: Where to continue, from a next-page hint.
    """
    if days is not None and days < 1:   # the site reads 0 as no limit and rejects less
        raise ArgumentError(f"--days must be 1 or more, got {days}",
                            help="Pass --days N for the last N days, or leave it out to search every date")
    if cursor is not None and cursor < 0:
        raise ArgumentError(f"--cursor must be 0 or more, got {cursor}",
                            help="Pass the --cursor value from a search's next-page hint")
    payload = {"query": query, "type": type, "days": days, "fids": ",".join(map(str, forum)) if forum else None,
               "sortBy": "dateline" if sort == "newest" else "relevance", "direction": "forward"}
    if cursor is not None:
        payload["cursor"] = cursor
    # tRPC's batch form: null fields travel as `undefined` in meta, as the site's own client sends them
    item = {"json": payload, "meta": {"values": {k: ["undefined"] for k, v in payload.items() if v is None}}}
    data = get(ctx, f"{TRPC}search.search", f"search {query!r}",
               {"batch": 1, "input": json.dumps({"0": item}, ensure_ascii=False, separators=(",", ":"))})
    entry = data[0] if isinstance(data, list) and data else {}
    if "error" in entry:
        err = entry["error"].get("json") or {}
        raise SiteRefused(f"1point3acres search {query!r}: {err.get('message') or entry['error']}")
    found = ((entry.get("result") or {}).get("data") or {}).get("json")
    if not isinstance(found, dict):
        raise RuntimeError("1point3acres search: unexpected answer (the API may have changed)")
    rows = [parse.search_row(t) for t in found.get("data") or []]
    nxt = found.get("cursor")
    about = {"query": query, "type": type, "sort": sort, "forum": ",".join(map(str, forum)) if forum else None,
             "days": days, "cursor": cursor}
    steps = [Next("thread", {"id": "<id>"}, "read a thread with its replies")]
    empty = f"no threads match {query!r}"
    if forum:   # guest search leaves some boards out (the 面经 boards): the board list still has them
        empty += f" in board(s) {about['forum']}; search leaves some boards out for guests (the 面经 boards)"
        steps += [Next("forum", {"id": str(f)}, f"list board {f}'s threads instead", when_empty=True) for f in forum[:2]]
    return Listing(name="threads", rows=rows, fields=("id", "title", "forum", "date"), about=about,
                   more={"cursor": nxt} if nxt is not None and rows else None, empty=empty, next=steps)


# --- the thread ----------------------------------------------------------------------------------------

def reply_page(ctx, tid: int, page: int) -> tuple[int, list[parse.Floor]]:
    """One page of top-level floors -> (top-level floor total, floors)."""
    data = get(ctx, f"/api/threads/{tid}/nested-posts", f"thread {tid} replies",
               {"pg": page, "ps": parse.PER_PAGE, "order": "time_asc", "sub_limit": 50, "with_total": 1})
    return int(data.get("total") or 0), parse.floors(data, page)


def reply_count(replies: int, floors: int) -> str:
    """`48 replies (26 floors, the rest nested)`: the API's reply count takes in the nested replies."""
    said = f"{replies} repl{'y' if replies == 1 else 'ies'}"
    return f"{said} ({floors} floor{'' if floors == 1 else 's'}, the rest nested)" if replies > floors else said


@action(onepoint3acres, tier=Tier.READ)
def thread(ctx, id: str, mode: Mode = "medium", from_: int | None = None, to: int | None = None):
    """A thread in compact-post format: the opening post is #1 and the replies (top-level floors,
    oldest first) are #2 on.

    Each floor's nested replies (楼中楼) follow its text as `↳ user (date · 👍N): text` lines
    (up to 50 per floor). `↩#M` marks a floor that quotes floor #M. `👍` counts upvotes. A post
    locked behind points (积分) shows `[hidden: needs more than N points]` unless the logged-in
    member has them. `medium` reads the first and last 30 floors, `full` everything (20 floors a
    request). Times are UTC.

    Args:
        id: Thread id or thread URL (…/bbs/thread-1196058-1-1.html or …/home/thread/1196058).
        mode: medium (first and last 30 floors) or full.
        from_: First floor number to read (overrides mode).
        to: Last floor number to read (overrides mode).
    """
    tid = parse.thread_ref(id)
    head = get(ctx, f"/api/v3/home-threads/{tid}", f"thread {tid}")
    t = head.get("thread")
    if not isinstance(t, dict) or not t.get("tid"):
        raise RuntimeError(f"1point3acres thread {tid}: no thread in the answer (the API may have changed)")
    first = reply_page(ctx, tid, 1)
    replies = first[0]
    total = replies + 1
    wanted = set(select_numbers(total, mode, from_, to))
    read = read_pages(lambda n: reply_page(ctx, tid, n), {1: first}, {n for n in wanted if n >= 2},
                      lambda n: (n - 2) // parse.PER_PAGE + 1,
                      lambda loaded: ((f.number, f) for f in loaded[1]))
    op = parse.opening_post(t)
    shown = [f for n, f in read.floors.items() if n in wanted]
    posts = ([op] if 1 in wanted else []) + parse.to_posts(
        shown, op.user, lambda pid: f"{parse.SITE}/bbs/forum.php?mod=redirect&goto=findpost&ptid={tid}&pid={pid}")
    view = thread_view(posts, read_spans(wanted, total), count=total)
    row = parse.thread_row(t)
    title = row["title"] or f"Thread {tid}"
    if row["type"]:
        title = f"[{row['type']}] {title}"
    board, seen = row["forum"], row["views"]
    tags = ", ".join(parse.text_of(x.get("displayname") or x.get("tagname")) for x in t.get("tags") or [])
    line = " · ".join(x for x in (board and f"Board: {board} ({t.get('fid')})", seen and f"{seen} views",
                                  reply_count(int(t.get("replies") or 0), replies),
                                  tags and f"Tags: {tags}") if x)
    notes = [line, "Times: UTC"]
    if (need := parse.locked_points(t)) is not None:
        notes.append(f"The opening post is locked: it needs more than {need} points (积分). A member with "
                     f"enough points can read it after logging in themselves in {ctx.login_needed().where}.")
    data = {"id": tid, "title": row["title"], "type": row["type"] or None, "url": parse.thread_url(tid),
            "forum": board or None, "forum_id": t.get("fid"), "views": seen, "replies": t.get("replies"),
            "floors": total, "tags": [parse.text_of(x.get("tagname")) for x in t.get("tags") or []]}
    return view.result(title, url=parse.thread_url(tid), data=data, notes=notes,
                       next=view.range_steps({"id": str(tid)}))

