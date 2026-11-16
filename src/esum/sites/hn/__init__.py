"""Hacker News: Algolia search, the official Firebase feeds, and whole comment trees from Algolia."""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from esum import (ArgumentError, HttpError, Listing, Mode, Next, Tier, Transport, action, parse_since, thread_view, read_spans,
                  select_numbers, site)
from esum.transport import TRANSIENT_ERRORS

from .parse import (ITEM_URL, UNREAD, comment_from_item, page_rows, story_from_hit, story_from_item, story_row,
                    thread_from_item, tree_comments)

FORMAT = 1
PORTED_FROM = ("xgoose skills/hacker-news.source.xgs.js (github.com/xGoose-dev/xgoose); xgoose "
               "apps/chrome/skills/hacker-news.xgs.js (search storyId, minComments, empty query); "
               "nitan_plugin My.user.js (hn adapter: Algolia item tree, time-ordered numbering)")

ALGOLIA = "https://hn.algolia.com/api/v1"
FIREBASE = "https://hacker-news.firebaseio.com/v0"
ITEM_WORKERS = 8   # Firebase item reads in flight at once in `stories`

# No request spacing (D-87): HN's official Firebase API states it has no rate limit, and Algolia allows
# 10,000 requests an hour per IP. A 429 still backs every ESUM process off (D-45).
hn = site(
    name="hn",
    base_url="https://news.ycombinator.com",
    transport=Transport.DIRECT,
    extras={"min_interval": 0},
    label={"en": "Hacker News: tech and startup stories with threaded discussions",
           "zh": "Hacker News：科技与创业社区"},
)

SearchType = Literal["story", "comment", "all", "ask_hn", "show_hn", "poll", "job"]
Feed = Literal["top", "new", "best", "ask", "show", "job"]


def _bound(value: str | None, flag: str) -> int | None:
    """A date bound as Unix seconds: YYYY-MM-DD, an ISO time, or a relative 7d / 2w / 12h."""
    try:
        when = parse_since(value)
    except ArgumentError as e:
        raise ArgumentError(str(e).replace("--since", flag), help=e.help) from None
    return int(when.timestamp()) if when else None


@action(hn, tier=Tier.READ)
def search(ctx, query: str = "", type: SearchType = "story", sort: Literal["relevance", "date"] = "relevance",
           author: str | None = None, story: str | None = None, days: int | None = None,
           after: str | None = None, before: str | None = None, min_points: int | None = None,
           min_comments: int | None = None, page: int = 0, limit: int = 20):
    """Search stories or comments with HN Algolia.

    Each row gives the discussion URL (read it with `thread --id`) and the linked article, which is
    an external page whose text is not included. The query may be empty, so the filters alone
    select: a user's comments, newest first, are `--type comment --author <name> --sort date`, and
    `--story <id>` keeps only items inside one story (its comments, with `--type comment`). Algolia
    also matches near spellings (`rust` finds "just" and "trust"), so quote a word for exact matches:
    `--query '"rust"'`.

    Args:
        query: Search words (optional: an empty query matches everything the filters allow).
        type: What to search: story, comment, all, ask_hn, show_hn, poll or job.
        sort: relevance, or date (newest first).
        author: Only items by this username.
        story: Only items inside this story: an item id or HN item URL.
        days: Only items from the last N days.
        after: Only items created after this time: YYYY-MM-DD, an ISO time, or a relative 7d / 2w.
        before: Only items created before this time: YYYY-MM-DD, an ISO time, or a relative 7d / 2w.
        min_points: Only items with at least this many points.
        min_comments: Only stories with at least this many comments (comments have no count, so
            this matches none of them).
        page: Result page, starting at 0.
        limit: Results per page (max 100).
    """
    story_id = item_id(story) if story is not None else None
    tags = [t for t in (type if type != "all" else None, f"author_{author}" if author else None,
                        f"story_{story_id}" if story_id else None) if t]
    for flag, count in (("--min-points", min_points), ("--min-comments", min_comments)):
        if count is not None and count < 0:
            raise ArgumentError(f"{flag} {count}: a count can't be negative", help=f"give {flag} 0 or more, or drop it")
    if days is not None and days < 1:
        raise ArgumentError(f"--days {days}: give 1 or more", help="--days N keeps the last N days; drop it to search all time")
    starts = [] if days is None else [(int(time.time()) - days * 86400, f"--days {days}")]
    if (start := _bound(after, "--after")) is not None:
        starts.append((start, f"--after {after}"))
    end = _bound(before, "--before")
    for since, flag in starts:
        if end is not None and since >= end:
            raise ArgumentError(f"{flag} starts at or after --before {before}, so no item falls between them",
                                help="give a start earlier than --before, or drop one of them")
    filters = [f"created_at_i>{since}" for since, _ in starts]
    if end is not None:
        filters.append(f"created_at_i<{end}")
    if min_points is not None:
        filters.append(f"points>={min_points}")
    if min_comments is not None:
        filters.append(f"num_comments>={min_comments}")
    data = ctx.http.get_json(f"{ALGOLIA}/{'search_by_date' if sort == 'date' else 'search'}", params={
        "query": query, "tags": ",".join(tags) or None, "numericFilters": ",".join(filters) or None,
        "page": page, "hitsPerPage": limit})
    stories = [story_from_hit(h) for h in data.get("hits", [])]
    pages = data.get("nbPages") or 0
    total = data.get("nbHits")
    per_page = data.get("hitsPerPage") or limit
    capped = bool(data.get("message"))                  # Algolia's own words name its index settings
    if capped and (served := re.search(r"fetch the (\d+) hits", data["message"])):
        pages = -(-int(served[1]) // per_page)          # past its hit limit Algolia answers nbPages 0
    available = pages * per_page
    beyond = pages > 0 and page >= pages                # past the last page of a search that has hits
    past_end = beyond or capped
    limited = total is not None and total > available
    about = {"query": query, "page": page, "pages": pages}
    if limited:
        about["limit"] = f"Algolia exposes only the first {available} hits for this query"
    comments = type in ("comment", "all")
    named = " ".join(f"--{flag.replace('_', '-')} {value}" for flag, value in (
        ("author", author), ("story", story), ("days", days), ("after", after), ("before", before),
        ("min_points", min_points), ("min_comments", min_comments)) if value is not None)
    next_steps = [Next("thread", {"id": "<id>"}, "read an item's discussion")]
    if beyond:
        next_steps.append(Next("search", {"page": pages - 1}, "read the last page", when_empty=True))
    if sort == "date":   # newest first: older items lie before the oldest row, however long ago
        if limited and stories and page + 1 >= pages and stories[-1].date:
            next_steps.append(Next("search", {"page": 0, "before": f"{stories[-1].date:%Y-%m-%dT%H:%M:%SZ}"},
                                   "read further back than Algolia's limit"))
    elif limited or capped:
        next_steps.append(Next("search", {"page": 0, "days": max(1, days // 2) if days else 30},
                               "narrow the query by creation time", when_empty=True))
    return Listing(name="results", rows=[story_row(s) for s in stories],
                   fields=("id", "title", "date", "text") if comments
                   else ("id", "title", "points", "comments", "date") if min_comments is not None
                   else ("id", "title", "points", "date"),
                   about=about, total=None if past_end else total,
                   more={"page": page + 1} if page + 1 < pages else None,
                   empty=f"nothing past page {pages - 1}: this search has {pages} page(s), numbered from 0"
                   if beyond else f"nothing on page {page}: Algolia serves no more of this search" if capped
                   else f"no {type} matches {query!r}" + (f" with {named}" if named else "") if query
                   else f"no {type} matches these filters: {named}" if named else f"HN has no {type} items",
                   next=next_steps)


@action(hn, tier=Tier.READ)
def stories(ctx, feed: Feed = "top", offset: int = 0, limit: int = 30):
    """List a front-page feed (top, new, best, ask, show, job) from the official HN API.

    Rows come in the feed's own order. Each row costs one request (sent a few at a time), so keep
    `limit` modest and page with `--offset`.

    Args:
        feed: top (the front page), new, best, ask, show or job.
        offset: Rows to skip.
        limit: Rows to read (max 100).
    """
    ids = ctx.http.get_json(f"{FIREBASE}/{feed}stories.json") or []
    selected = ids[offset:offset + limit]
    with ThreadPoolExecutor(max_workers=ITEM_WORKERS) as pool:   # map() keeps the feed's order
        items = list(pool.map(lambda item_id: ctx.http.get_json(f"{FIREBASE}/item/{item_id}.json"), selected))
    rows = [story_from_item(item) for item in items if item and not item.get("deleted") and not item.get("dead")]
    end = offset + len(selected)
    return Listing(name="stories", rows=[story_row(s) for s in rows], fields=("id", "title", "points", "comments"),
                   about={"feed": feed, "offset": offset}, total=len(ids),
                   more={"offset": end} if end < len(ids) else None,
                   empty=f"the {feed} feed has nothing past row {offset}",
                   next=[Next("thread", {"id": "<id>"}, "read a story with its comments")])


@dataclass
class Coverage:
    """What `thread` found beyond Algolia's tree. `missing` is None when it could not be counted."""

    comments: list[dict] = field(default_factory=list)   # restored, in the tree's shape
    missing: int | None = 0
    notes: list[str] = field(default_factory=list)


READ_FAILURES = (HttpError, *TRANSIENT_ERRORS)


def _failure(e: Exception) -> str:
    return f"HTTP {e.status}" if isinstance(e, HttpError) else type(e).__name__


def _unchecked(reason: str) -> Coverage:
    return Coverage(missing=None, notes=[f"Comments left out by Algolia were not checked: {reason}."])


def _restore(ctx, root: dict, story: dict | None) -> Coverage:
    """Find what the tree leaves out, using `story` (Firebase) to tell whether anything is.

    A failed read here costs the restoration, not the thread: the tree is still good, so the failure
    becomes a notice with a count.
    """
    tree = tree_comments(root)
    live = sum(1 for c in tree if c.get("author"))
    is_comment = root.get("type") == "comment"
    if not is_comment and story is None:
        try:
            story = ctx.http.get_json(f"{FIREBASE}/item/{root['id']}.json")
        except READ_FAILURES as e:
            return _unchecked(f"the story's count could not be read ({_failure(e)})")
        if not story:
            return _unchecked("Firebase returned nothing for the story")
    expected = None if is_comment or story is None else story.get("descendants")
    if not is_comment and (expected or 0) <= live:
        return Coverage()
    try:
        rows = page_rows(ctx.http.get_text("item", params={"id": root["id"]}), int(root["id"]))
    except READ_FAILURES as e:
        reason = f"the page could not be read ({_failure(e)})"
        if expected is None:
            return _unchecked(reason)
        return Coverage(missing=expected - live, notes=[
            f"Comments missing: {expected - live} (the story counts {expected} and Algolia has {live}; {reason})."])

    known = {int(c["id"]) for c in tree}
    comments, unread, restored_live, error = [], 0, 0, None
    for row in rows:
        if row["id"] in known:
            continue
        item = None
        if error is None:   # a failed read has already waited out its retries; don't repeat that per row
            try:
                item = ctx.http.get_json(f"{FIREBASE}/item/{row['id']}.json")
            except READ_FAILURES as e:
                error = e
        if item:
            comments.append(comment_from_item(item))
            restored_live += not (item.get("dead") or item.get("deleted"))
        else:
            comments.append({**row, "text": UNREAD})
            unread += 1

    notes = []
    read = len(comments) - unread
    if read:
        notes.append(f"Comments restored from the page and Firebase, left out of Algolia's tree: {read} "
                     f"({restored_live} live, {read - restored_live} dead or deleted).")
    have = live + restored_live
    missing = max(unread, expected - have) if expected is not None else unread
    if missing:
        reasons = [f"the story counts {expected} and this thread has {have}"] if expected is not None else []
        if unread:
            reasons.append(f"{unread} page rows could not be read from Firebase"
                           + (f" ({_failure(error)})" if error else ""))
        if expected is not None and expected - have > unread:
            reasons.append("the page lists no more")
        notes.append(f"Comments missing: {missing} ({'; '.join(reasons)}).")
    off_page = len(known - {row["id"] for row in rows})
    if rows and off_page:
        notes.append(f"Comments from Algolia that are not on the page: {off_page}.")
    return Coverage(comments=comments, missing=missing, notes=notes)


def item_id(value: str) -> int:
    """`50284255` or an item URL (`https://news.ycombinator.com/item?id=50284255`, scheme optional)."""
    text = number = str(value).strip()
    if not text.isdigit():
        try:
            url = urlsplit(text if "://" in text else f"https://{text}")
            host = (url.hostname or "").removeprefix("www.")
        except ValueError:   # a malformed URL, such as an unclosed [IPv6] host
            url, host = None, ""
        number = parse_qs(url.query).get("id", [""])[0] if url and host == "news.ycombinator.com" and url.path == "/item" else ""
    if not number.isdigit() or int(number) < 1:
        raise ArgumentError(f"not an HN item id or item URL: {text!r}",
                            help="Pass the number after item?id=, or the whole https://news.ycombinator.com/item?id=N URL")
    return int(number)


@action(hn, tier=Tier.READ)
def thread(ctx, id: str, mode: Mode = "medium", from_: int | None = None, to: int | None = None):
    """A story (or comment) and its whole comment tree in compact-post format.

    The story is post #1 and comments follow in time order, so numbers stay stable as the thread
    grows; `↩#M` names the comment a reply answers. `medium` shows the first and last 30 posts,
    `full` shows everything. The tree costs one Algolia request. Polls also read Firebase for the
    body and each option's label and votes; a single poll option costs one extra Firebase request.

    Algolia leaves out a dead comment and everything under it, so one more read compares the story's
    comment count with the tree (a comment's thread reads the page instead). When the tree falls
    short, the page gives the ids and Firebase gives each comment, one request per comment, so a
    large gap takes a while. They join the time order, and a dead parent stays as a `[dead comment]`
    placeholder. The header says how many were restored, how many are still missing, and why.

    Args:
        id: Item id or HN item URL (https://news.ycombinator.com/item?id=N).
        mode: medium (head + tail) or full.
        from_: First post number to show (overrides mode).
        to: Last post number to show (overrides mode).
    """
    id = item_id(id)
    try:
        root = ctx.http.get_json(f"{ALGOLIA}/items/{id}")
    except HttpError as e:
        if e.status == 404:
            e.help = f"Item {id} doesn't exist, or it is dead or deleted: Algolia has no page for those"
        raise
    firebase = None
    options = []
    if root.get("type") in ("poll", "pollopt"):
        firebase = ctx.http.get_json(f"{FIREBASE}/item/{id}.json")
        if root.get("type") == "poll":
            for option_id in firebase.get("parts", root.get("options") or []):
                options.append(ctx.http.get_json(f"{FIREBASE}/item/{option_id}.json"))
    coverage = Coverage() if root.get("type") == "pollopt" else _restore(ctx, root, firebase)
    th = thread_from_item(root, firebase=firebase, options=options, restored=coverage.comments)
    total = len(th.posts)
    wanted = set(select_numbers(total, mode, from_, to))
    view = thread_view([p for p in th.posts if p.number in wanted], read_spans(wanted, total), count=total)
    notes = [f"{label}: {ITEM_URL}{item_id}" for label, item_id in
             (("Story", th.story_id), ("Parent", th.parent_id)) if item_id is not None]
    return view.result(th.title, url=th.url, data={"id": th.id, "title": th.title, "url": th.url, "link": th.link,
                        "story_id": th.story_id, "parent_id": th.parent_id, "total": total,
                        "restored": len(coverage.comments), "missing": coverage.missing,
                        "notes": coverage.notes}, notes=notes + coverage.notes,
                       next=view.range_steps({"id": str(th.id)}))
