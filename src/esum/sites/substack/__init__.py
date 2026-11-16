"""Substack: any publication's archive, search, and a post with its whole comment thread."""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit

from esum import ArgumentError, HttpError, Listing, parse_date, parse_since, subtree, largest_subthreads, reply_counts, is_top_level, Mode, Next, Tier, Transport, action, thread_view, read_spans, select_numbers, site

from .parse import Ref, article_from, article_row, check_cursor, post_ref, publication_ref, thread_from

FORMAT = 1
PORTED_FROM = "xgoose apps/chrome/skills/substack.xgs.js (publication archive, search, post and comment endpoints)"

substack = site(
    name="substack",
    base_url="https://substack.com",
    transport=Transport.DIRECT,
    label={"en": "Substack: newsletters and their comment threads; any publication, on substack.com "
                 "or its own domain",
           "zh": "Substack：独立作者的 newsletter 与评论区"},
    extras={"min_interval": 1.0},   # 0.5 s drew HTTP 429s on runs of `thread` calls
)

READER = "https://substack.com"   # profiles and share links resolve here, not on a publication


PUBLICATION_HELP = ("Substack has no search across publications, so ESUM needs the publication's own "
                    "domain: find it with a web search, then run `esum substack archive --publication <domain>`")
POST_HELP = ("Check the post's slug or URL: `esum substack archive --publication <domain>` lists the "
             "publication's posts")


def _json(ctx, url: str, params: dict | None = None, *, missing: str = PUBLICATION_HELP):
    """A Substack API response. A site that isn't Substack answers with a web page: its HTML is
    replaced by one line (D-72). A 404 with Substack's JSON error is the item (`missing` says what
    to check); an empty 404 is a `<name>.substack.com` that has no publication."""
    resp = ctx.http.get(url, params=params, check=False, headers={"Accept": "application/json"})
    body = resp.text().strip()
    page = body.startswith("<")
    if not resp.ok:
        host = urlsplit(resp.url).hostname
        detail = (f"(a web page, not Substack's API: {host} is not a Substack publication)" if page
                  else f"(no Substack publication at {host})" if resp.status == 404 and not body else body)
        error = HttpError(resp.status, resp.url, detail)
        if resp.status == 404:
            error.help = missing if body and not page else PUBLICATION_HELP
        raise error
    try:
        return resp.json()
    except ValueError:
        error = RuntimeError(f"{resp.url} answered with {'a web page' if page else 'text that is not JSON'}, "
                             f"not Substack's API: is it a Substack publication?")
        error.help = PUBLICATION_HELP
        raise error from None


def _origin(ctx, ref: Ref) -> str:
    """The publication's origin; an author's `@handle` resolves to the publication they write."""
    if ref.origin is not None:
        return ref.origin
    profile = _json(ctx, f"{READER}/api/v1/user/{ref.handle}/public_profile",
                    missing=f"Check the handle: substack.com/@{ref.handle} has no profile")
    pub = profile.get("primaryPublication") or {}
    if pub.get("custom_domain"):
        return f"https://{pub['custom_domain']}"
    if pub.get("subdomain"):
        return f"https://{pub['subdomain']}.substack.com"
    raise ArgumentError(f"@{ref.handle} has no publication of their own",
                        help="Pass the publication's name or domain instead")


def _page(ctx, publication: str, sort: str, cursor: str | None, limit: int, query: str | None) -> Listing:
    """One page of the archive API, a search when `query` is set.

    The API pages with an opaque cursor: each page carries `nextCursor` (null on the last page),
    sent back as `cursor`. It rejects `offset` outright."""
    cursor = cursor or None
    if cursor:
        check_cursor(cursor, "post_date" if query is None and sort == "new" else "offset")
    origin = _origin(ctx, publication_ref(publication))
    data = _json(ctx, f"{origin}/api/v1/archive", params={
        "search": query, "sort": "new" if query is not None else sort, "cursor": cursor, "limit": limit})
    rows = data.get("posts") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"{origin} did not return a post list; is it a Substack publication?")
    articles = [article_from(p, origin) for p in rows]
    if query is not None:
        articles.sort(key=lambda a: (a.date.isoformat() if a.date else "") if sort == "new" else (a.likes or 0),
                      reverse=True)
    empty = f"no posts match {query!r}" if query else "no posts"
    following = data.get("nextCursor") if rows else None
    return Listing(name="posts", rows=[article_row(a) for a in articles],
                   fields=("slug", "title", "date", "comments"),
                   about={"publication": origin, "query": query, "sort": sort, "cursor": cursor, "times": "UTC",
                          **({"sort_scope": "returned search page"} if query is not None else {}),
                          **({"page": f"short: Substack returned {len(rows)} of --limit {limit}; "
                                      "the next page may still hold more"}
                             if following and len(rows) < limit else {})},
                   more={"cursor": following} if following else None,
                   empty=empty + (" past this cursor" if cursor else ""),
                   next=[Next("thread", {"post": "<slug>", "publication": origin}, "read a post with its comments")])


WINDOW_PAGES = 8   # pages of 25 one `--since`/`--until` call walks (200 posts) before handing over a cursor


def _bound(value: str | None, flag: str, *, end_of_day: bool = False) -> datetime | None:
    """A date bound in UTC; a bare date as `--until` means the end of that day, and may be in the future."""
    if end_of_day and value and (fixed := parse_date(value.strip())) is not None:
        fixed = fixed if fixed.tzinfo else fixed.replace(tzinfo=timezone.utc)
        return fixed + timedelta(days=1) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()) else fixed
    try:
        when = parse_since(value)
    except ArgumentError as e:
        raise ArgumentError(str(e).replace("--since", flag), help=e.help) from None
    return when


def _date_cursor(when: datetime) -> str:
    """A cursor that starts the newest-first archive just before `when` (Substack's own cursor shape)."""
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return base64.b64encode(json.dumps({"post_date": stamp}, separators=(",", ":")).encode()).decode()


def _window(ctx, publication: str, sort: str, since: datetime | None, until: datetime | None,
            cursor: str | None, labels: dict) -> Listing:
    """Every post in [since, until), walking the newest-first archive from `until` (or `cursor`) down to
    `since`, then ordered by date, likes or comments. Substack can't sort by comments or filter by
    date, so ESUM collects the window and orders it."""
    if cursor:
        check_cursor(cursor, "post_date")
    origin = _origin(ctx, publication_ref(publication))
    cursor = cursor or (_date_cursor(until) if until else None)
    articles, reached = [], False
    for _ in range(WINDOW_PAGES):
        data = _json(ctx, f"{origin}/api/v1/archive", params={"sort": "new", "cursor": cursor, "limit": 25})
        rows = data.get("posts") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError(f"{origin} did not return a post list; is it a Substack publication?")
        for a in (article_from(p, origin) for p in rows):
            if a.date and since and a.date < since:
                reached = True
            elif not (a.date and until and a.date >= until):
                articles.append(a)
        cursor = data.get("nextCursor") if rows else None
        if reached or not cursor:
            break
    more = {"cursor": cursor, **labels} if cursor and not reached else None
    order = {"new": lambda a: a.date.isoformat() if a.date else "", "top": lambda a: a.likes or 0,
             "comments": lambda a: a.comments or 0}[sort]
    articles.sort(key=order, reverse=True)
    since_, until_ = labels.get("since"), labels.get("until")
    window = f"{since_} to {until_}" if since_ and until_ else f"since {since_}" if since_ else f"until {until_}"
    return Listing(name="posts", rows=[article_row(a) for a in articles],
                   fields=("slug", "title", "date", "comments"), total=None if more else len(articles),
                   about={"publication": origin, "sort": sort, "window": window, "times": "UTC",
                          **({"window_scope": f"the newest {len(articles)} posts of the window, walked "
                                              f"{WINDOW_PAGES} pages deep; the next command continues"} if more else {})},
                   more=more, empty=f"no posts {window}",
                   next=[Next("thread", {"post": "<slug>", "publication": origin}, "read a post with its comments")])


@action(substack, tier=Tier.READ)
def archive(ctx, publication: str, sort: Literal["new", "top", "comments"] = "new", since: str | None = None,
            until: str | None = None, cursor: str | None = None, limit: int = 20):
    """One page of a publication's posts, or every post in a date window.

    Columns: slug (for `thread --post`), title, date and comments; `--fields` adds likes,
    subtitle, author, words, paid and url. With `--since` and/or `--until`, the result is every post
    in that window (up to 200 per call; the next command continues) and `--limit` does not apply:
    `--sort comments` finds the most-discussed posts of a period, `--sort top` the most liked.

    Args:
        publication: The publication: its name (`noahpinion`), domain (`www.noahpinion.blog`), any URL on it, or its author's `substack.com/@handle`.
        sort: new (newest first); top (most liked in a window, else Substack's popularity ranking, which is not strictly by likes); comments (most commented; needs --since or --until).
        since: Oldest post to include: YYYY-MM-DD, an ISO time, or a relative 30d / 2w (UTC).
        until: Posts before this time; a bare date includes that whole day (UTC).
        cursor: Where the next page starts: the value a previous page's next-page command names.
        limit: Posts per page (max 25); not used with --since/--until.
    """
    if since or until:
        lo, hi = _bound(since, "--since"), _bound(until, "--until", end_of_day=True)
        if lo and hi and lo >= hi:
            raise ArgumentError(f"--since {since} is not before --until {until}",
                                help="Give --since an earlier time than --until")
        return _window(ctx, publication, sort, lo, hi, cursor, {"since": since, "until": until})
    if sort == "comments":
        raise ArgumentError("--sort comments needs a window: Substack can't sort by comments, so ESUM "
                            "orders the posts of a period", help="Add --since (e.g. --since 30d), and --until if needed")
    return _page(ctx, publication, sort, cursor, limit, None)


@action(substack, tier=Tier.READ)
def search(ctx, publication: str, query: str, sort: Literal["new", "top"] = "new", cursor: str | None = None,
           limit: int = 20):
    """Search one publication's posts by keyword.

    This searches a single publication, not all of Substack. Columns as for `archive`.
    Substack returns matches by relevance a page at a time (its `top` order ignores the query), so
    `--sort` orders each returned page by date or likes, not all matches: page through with
    `--cursor` for more.

    Args:
        publication: The publication: its name (`noahpinion`), domain (`www.noahpinion.blog`), any URL on it, or its author's `substack.com/@handle`.
        query: Search words.
        sort: new (newest first on this page) or top (most liked on this page).
        cursor: Where the next page starts: the value a previous page's next-page command names.
        limit: Posts per page (max 25).
    """
    return _page(ctx, publication, sort, cursor, limit, query)


def _load_thread(ctx, post: str, publication: str | None):
    """The post and its whole comment tree, from a post URL, share link or slug."""
    ref, slug = post_ref(post, publication)
    if ref.post_id is not None:   # an app share link: Substack knows the post's own URL
        shared = _json(ctx, f"{READER}/api/v1/posts/by-id/{ref.post_id}",
                       missing="Check the share link: Substack has no post with that id")
        url = (shared.get("post") or shared).get("canonical_url")
        if not url:
            raise RuntimeError(f"Substack returned no URL for post {ref.post_id}")
        ref, slug = post_ref(url, None)
    origin = _origin(ctx, ref)
    data = _json(ctx, f"{origin}/api/v1/posts/{slug}", missing=POST_HELP)
    pid = (data.get("post") or data).get("id")
    comments = ctx.http.get_json(f"{origin}/api/v1/post/{pid}/comments",
                                 params={"all_comments": "true", "sort": "oldest_first"})
    return thread_from(data, comments.get("comments") or [], origin)


OUTLINE_WIDTH = 120   # characters of a comment's first line in `outline`
OUTLINE_AFTER = 100   # posts above which `thread` offers `outline` first


@action(substack, tier=Tier.READ)
def outline(ctx, post: str, publication: str | None = None,
            sort: Literal["replies", "likes", "date"] = "replies", limit: int = 50):
    """Every top-level comment of a post, one row each, without the replies' text: who wrote it,
    how many replies sit under it (any depth), its likes and its first line. Read a big comment
    section's shape in one call, then `thread --root N` the arguments worth reading.

    Args:
        post: The post's URL (…/p/<slug>, or an app share link …/p-<id>), or its slug together with --publication.
        publication: The publication, when --post is a bare slug.
        sort: replies (largest sub-threads first), likes, or date (thread order).
        limit: Rows to show (max 500).
    """
    th = _load_thread(ctx, post, publication)
    replies = reply_counts(th.posts)
    tops = [p for p in th.posts if is_top_level(p)]
    order = {"replies": lambda p: (-replies[p.number], p.number), "likes": lambda p: (-(p.likes or 0), p.number),
             "date": lambda p: p.number}[sort]
    tops.sort(key=order)
    rows = [{"number": p.number, "user": p.user, "replies": replies[p.number], "likes": p.likes or 0,
             "date": p.date.strftime("%Y-%m-%d %H:%M") if p.date else "",
             "text": " ".join(p.text.split())[:OUTLINE_WIDTH]} for p in tops[:limit]]
    comments = len(th.posts) - 1
    return Listing(name="comments", rows=rows, fields=("number", "user", "replies", "likes", "text"),
                   about={"post": th.title, "url": th.url, "sort": sort, "times": "UTC",
                          "comments": f"{comments} in all, {len(tops)} top-level; replies counts every reply under a comment"},
                   total=len(tops), empty="no comments",
                   next=[Next("thread", {"post": th.url, "publication": None, "root": "<number>"},
                              "read one comment and every reply under it"),
                         *([Next("outline", {"limit": min(len(tops), 500)}, "show every top-level comment")]
                           if len(tops) > limit else [])])


@action(substack, tier=Tier.READ)
def thread(ctx, post: str, publication: str | None = None, mode: Mode = "medium",
           from_: int | None = None, to: int | None = None, root: int | None = None):
    """A post and its whole comment thread in compact-post format.

    The post is #1 and comments follow in time order, so numbers stay stable as comments arrive;
    `↩#M` names the comment a reply answers. A paid post the session can't read shows only its
    free preview, and says so. `medium` shows the first and last 30 posts, `full` everything.
    `--root N` shows one sub-thread: comment #N and every reply under it, however deep, so one
    argument can be read without the rest of the thread.

    Args:
        post: The post's URL (…/p/<slug>, or an app share link …/p-<id>), or its slug together with --publication.
        publication: The publication, when --post is a bare slug.
        mode: medium (head + tail) or full.
        from_: First post number to show (overrides mode).
        to: Last post number to show (overrides mode).
        root: Show only this comment and the replies under it (overrides mode; --from/--to narrow it).
    """
    th = _load_thread(ctx, post, publication)
    total = len(th.posts)
    notes = ["Times: UTC"]
    if root is not None:
        branch = set(subtree(th.posts, root))
        if from_ is not None or to is not None:
            branch &= set(select_numbers(total, mode, from_, to))
        view = thread_view([p for p in th.posts if p.number in branch], [(1, total, f"outside #{root}'s sub-thread")],
                           count=total)
        steps = []
    else:
        wanted = set(select_numbers(total, mode, from_, to))
        view = thread_view([p for p in th.posts if p.number in wanted], read_spans(wanted, total), count=total)
        steps = view.range_steps()
        big = largest_subthreads(th.posts)
        if big:
            notes.append("Largest sub-threads (replies under a top-level comment, any depth): "
                         + ", ".join(f"#{number} {user} ({replies})" for number, user, replies in big))
            steps += [Next("thread", {"mode": None, "from_": None, "to": None, "root": number}, f"read #{number}'s sub-thread ({replies} replies)")
                      for number, _, replies in big[:2]]
        if total > OUTLINE_AFTER:
            steps.insert(0, Next("outline", {"post": th.url, "publication": None},
                              "see every top-level comment and its reply count, without the text"))
    return view.result(th.title, url=th.url, data={"id": th.id, "title": th.title, "url": th.url, "preview": th.preview,
                        "total": total, **({"root": root} if root is not None else {})},
                       notes=notes + ([f"Sub-thread of #{root}: {len(branch)} posts"] if root is not None else []),
                       next=steps)
