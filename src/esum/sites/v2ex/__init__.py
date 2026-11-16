"""V2EX: the public v1 API for topics, replies and nodes, and the sov2ex index for full-history search."""

from __future__ import annotations

import time
from typing import Literal

from esum import (NoSuchItem, ArgumentError, HttpError, Listing, Mode, Next, Tier, Transport, action,
                  one_line, thread_view, read_spans, select_numbers, site)

from .parse import (TOPIC_URL, add_supplements, match_nodes, num, search_row, supplements, thread_from,
                    topic_from_api, topic_from_hit, topic_row)

FORMAT = 1
PORTED_FROM = "xgoose apps/chrome/skills/v2ex.xgs.js (v1 read API and sov2ex search; v2 token API not ported)"

API = "https://www.v2ex.com/api"
SOV2EX = "https://www.sov2ex.com/api/search"

v2ex = site(
    name="v2ex",
    base_url="https://www.v2ex.com",
    transport=Transport.DIRECT,
    label={"en": "V2EX: Chinese-language community for developers and creators; topics by node "
                 "(programming, jobs, deals, life) with long reply threads",
           "zh": "V2EX：程序员与创意工作者社区，按节点分区"},
)


@action(v2ex, tier=Tier.READ)
def search(ctx, query: str, node: str | None = None, user: str | None = None,
           sort: Literal["relevance", "created"] = "relevance", days: int | None = None,
           page: int = 0, limit: int = 20):
    """Full-history topic search through sov2ex, a third-party index of V2EX.

    Columns: topic id (for `thread --id`), title and date; `--fields` adds author, url and text (the
    opening post's first 300 characters, though a hit may be in a reply). The index has no usable
    reply counts, and it lags the site by hours, so for recent topics use `topics --feed latest`.

    Args:
        query: Search words (any of them may match).
        node: Only these nodes (`python,go`), or all but these (`-jobs,-deals`); not both at once.
        user: Only topics by this username.
        sort: relevance, or created (newest first).
        days: Only topics from the last N days.
        page: Result page, starting at 0.
        limit: Results per page (max 50).
    """
    wanted = [n.strip() for n in (node or "").split(",") if n.strip()]
    if any(n.startswith("-") for n in wanted) and not all(n.startswith("-") for n in wanted):
        raise ArgumentError(f"--node {node!r} mixes included and excluded nodes",
                            help="sov2ex ignores the included ones then: use only names or only -names")
    data = ctx.http.get_json(SOV2EX, params={
        "q": query, "from": page * limit, "size": limit, "sort": "created" if sort == "created" else "sumup",
        "order": 0, "node": node, "username": user,
        "gte": int(time.time()) - days * 86400 if days is not None else None})
    topics = [topic_from_hit(h) for h in data.get("hits") or []]
    total = data.get("total")
    total = num(total.get("value") if isinstance(total, dict) else total, len(topics))
    return Listing(name="results", rows=[search_row(t) for t in topics], fields=("id", "title", "date"),
                   about={"query": query, "node": node, "user": user, "sort": sort, "page": page}, total=total,
                   more={"page": page + 1} if (page + 1) * limit < total else None,
                   empty=f"no topics match {query!r} in the sov2ex index",
                   next=[Next("thread", {"id": "<id>"}, "read a topic with every reply")],
                   data={"total": total, "page": page, "results": [search_row(t) for t in topics]})


@action(v2ex, tier=Tier.READ)
def topics(ctx, feed: Literal["latest", "hot"] = "latest", node: str | None = None,
           user: str | None = None, limit: int = 20):
    """Recent topics: the site-wide latest or hot list, or one node's or one member's latest.

    Each list is one fixed snapshot from the API (hot is about 10 topics, a node or member about
    10, latest a few dozen); there is no paging. For older topics use `search`. Columns: topic id
    (for `thread --id`), title, node and replies; `--fields` adds date, author, url and text.

    Args:
        feed: latest or hot (site-wide; ignored with --node or --user).
        node: A node's latest topics (its name, e.g. `python`; see `nodes`).
        user: A member's latest topics.
        limit: Rows to show.
    """
    if node and user:
        raise ArgumentError("pass --node or --user, not both")
    if node or user:
        rows = ctx.http.get_json(f"{API}/topics/show.json", params={"node_name": node, "username": user})
        scope = f"node {node}" if node else f"member {user}"
    else:
        rows = ctx.http.get_json(f"{API}/topics/{feed}.json")
        scope = f"{feed} topics"
    found = [topic_from_api(t) for t in rows or []]
    shown = found[:limit]
    return Listing(name="topics", rows=[topic_row(t) for t in shown], fields=("id", "title", "node", "replies"),
                   about={"scope": scope}, total=len(found), empty=f"the {scope} list is empty",
                   next=[Next("thread", {"id": "<id>"}, "read a topic with every reply")])


@action(v2ex, tier=Tier.READ)
def thread(ctx, id: int, mode: Mode = "medium", from_: int | None = None, to: int | None = None):
    """A topic and every reply in compact-post format.

    The topic is post #1, with its supplements (附言) after its text, and reply floor N is post
    #N+1. `↩#M` follows the `@user` / `@user #N` a reply starts with; a `#N` inside a reply is the
    site's floor. `medium` shows the first and last 30 posts, `full` shows everything; the whole
    thread is three requests either way. Thanks aren't in the public API, so `likes` is null.

    Args:
        id: Topic id (the number in v2ex.com/t/…).
        mode: medium (head + tail) or full.
        from_: First post number to show (overrides mode).
        to: Last post number to show (overrides mode).
    """
    found = ctx.http.get_json(f"{API}/topics/show.json", params={"id": id})
    if not isinstance(found, list):
        raise RuntimeError(f"V2EX topic {id}: expected a topic list (the site data may have changed)")
    if not found:
        raise NoSuchItem(f"V2EX topic {id} not found (deleted, or hidden from the public API)")
    replies = ctx.http.get_json(f"{API}/replies/show.json", params={"topic_id": id}) or []
    th = thread_from(found[0], replies)
    notes = ["Times: UTC"]
    try:
        add_supplements(th, supplements(ctx.http.get_text(f"{TOPIC_URL}{id}")))
    except HttpError as e:
        notes.append(f"Supplements not read (HTTP {e.status})")
    total = len(th.posts)
    wanted = set(select_numbers(total, mode, from_, to))
    view = thread_view([p for p in th.posts if p.number in wanted], read_spans(wanted, total), count=total)
    title = f"{th.title} [{th.node}]" if th.node else th.title
    return view.result(title, url=th.url, data={"id": th.id, "title": th.title, "node": th.node,
                                               "url": th.url, "total": total}, notes=notes,
                       next=view.range_steps())


@action(v2ex, tier=Tier.READ)
def nodes(ctx, query: str | None = None, limit: int = 30):
    """The node directory: name (for `--node`), title and topic count, busiest first.

    Args:
        query: Only nodes whose name or title contains this (e.g. `python`, `优惠`).
        limit: Rows to show.
    """
    found = match_nodes(ctx.http.get_json(f"{API}/nodes/all.json") or [], query)
    shown = found[:limit]
    rows = [{"name": one_line(n.get("name")), "title": one_line(n.get("title")), "topics": num(n.get("topics"))}
            for n in shown]
    return Listing(name="nodes", rows=rows, fields=("name", "title", "topics"), about={"query": query},
                   total=len(found), empty=f"no node matches {query!r}",
                   next=[Next("topics", {"node": "<name>"}, "list a node's latest topics")])
