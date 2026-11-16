"""Reddit: search, subreddit listings and comment threads from Reddit's JSON API, plus comment
search, profiles and wiki pages from the HTML pages Reddit serves to a guest.

Reddit refuses plain HTTP clients (HTTP 403 "blocked by network security"), so the site reads
through T2: Chrome's network fingerprint (curl_cffi) plus the browser session's cookie jar. With
that fingerprint a guest still gets 403 from the JSON API, so `search`, `listing` and `thread` then
read the page fragments Reddit serves to a guest. Reddit's normal pages answer a new guest with a
JavaScript check: the call moves to a real browser tab, whose requests get the real pages, and later
calls read with the cookies Reddit gave that browser (D-40, D-50). A check that still shows after
the wait is CHECK NEEDED for the host agent to tick (D-54), LOGIN NEEDED in native mode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from esum import (command_line, NoSuchItem, SiteRefused, ArgumentError, HttpError, Listing, LoginNeeded, Mode, Next, Result, Tier, Transport, action,
                  thread_view, read_spans, select_numbers, site, one_line)
from esum.transport import ChallengeError

from . import parse
from .parse import CommentTree, build_thread, link_from, link_row, links_from_listing, missing_note, more_things, \
    post_data

FORMAT = 1
PORTED_FROM = ("xgoose skills/reddit.source.xgs.js (github.com/xGoose-dev/xgoose); "
               "nitan_plugin My.user.js (reddit adapter: limit=500, sort=old, time-ordered numbering); "
               "comment search endpoint from xgoose skills/reddit.xgs.js; "
               "recent comments, duplicates, wiki page list and revisions, redd.it links from xgoose "
               "skills/reddit.xgs.js")

reddit = site(
    name="reddit",
    base_url="https://www.reddit.com",
    transport=Transport.BROWSER_HTTP,
    fallback=Transport.IN_PAGE,         # a real tab reads the pages T2 gets a check for (D-40, D-50)
    extras={"challenge_text": 'name="js_challenge"'},
    label={"en": "Reddit: communities (subreddits) with threaded discussions, e.g. r/churning",
           "zh": "Reddit：按版块 (subreddit) 组织的社区论坛"},
)

Time = Literal["hour", "day", "week", "month", "year", "all"]
_SUBREDDIT = re.compile(r"^[A-Za-z0-9_]+(\+[A-Za-z0-9_]+)*$")
_POST_ID = re.compile(r"^[a-z0-9]+$")
_USER = re.compile(r"^[A-Za-z0-9_-]+$")
_WIKI_PAGE = re.compile(r"^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$")
EMBED = "https://embed.reddit.com"
_LOGIN_DETAIL = "open reddit.com in that window, log in if asked, then retry"
GUEST_CALLS = 10   # one guest "load more" call brings a few comments, an `--expand` batch up to 100


def _sub(subreddit: str | None) -> str:
    if not subreddit:
        return ""
    name = subreddit.removeprefix("/").removeprefix("r/")
    if not _SUBREDDIT.match(name):
        raise ArgumentError(f"not a subreddit name: {subreddit!r}",
                            help="Pass a subreddit name such as churning, or churning+awardtravel for several")
    return f"/r/{name}"


def _one_sub(subreddit: str) -> str:
    name = subreddit.removeprefix("/").removeprefix("r/")
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ArgumentError(f"not one subreddit: {subreddit!r}", help="Name one subreddit, e.g. --subreddit churning")
    return name


def _post_id(id: str) -> str:
    """Accept `abc123`, `t3_abc123`, a post URL or a redd.it short link."""
    m = re.search(r"/comments/([a-z0-9]+)", id) or re.match(r"(?:https?://)?redd\.it/([a-z0-9]+)/?(?:[?#]|$)", id)
    value = m.group(1) if m else id.removeprefix("t3_")
    if not _POST_ID.match(value):
        raise ArgumentError(f"not a Reddit post id: {id!r}",
                            help="Pass a post id such as 1xlwpqv (or t3_1xlwpqv), a post URL with /comments/<id>/ in it, "
                                 "or a redd.it/<id> link")
    return value


def _cursor(after: str | None, kind: Literal["t1", "t3"]) -> str | None:
    """Reddit answers a cursor it can't read with page 1 (or nothing), so check its shape first."""
    if after is None or re.fullmatch(rf"{kind}_[a-z0-9]+", after):
        return after
    raise ArgumentError(f"not a Reddit cursor: --after {after!r}",
                        help=f"Pass the {kind}_… value from the previous page's \"next page\" hint, or leave out --after")


@dataclass(frozen=True)
class GuestAllowed:
    """This read was refused, but the caller has another guest path to try."""

    detail: str


def _read(ctx, path: str, params: dict | None = None, *, kind: Literal["json", "page"] = "page",
          guest_allowed: bool = False) -> dict | list | str | int | float | bool | None | GuestAllowed:
    """Read JSON or HTML; return data or a refusal when another guest path is open.

    Only a closed read becomes LOGIN NEEDED. Transport challenges and check pages share
    this boundary, preserving Reddit's D-50/D-61 wording and exit code.
    """
    api = kind == "json"

    def refused(status, url, *, check=False, reason=""):
        detail = (f"reddit answered {url} with a check the browser didn't clear; " if check else
                  f"reddit returned HTTP {status}{reason} for {url}; ") + _LOGIN_DETAIL
        if guest_allowed:
            return GuestAllowed(detail)
        raise ctx.login_needed(detail=detail)

    try:
        resp = ctx.http.get(path, params={**(params or {}), "raw_json": 1} if api else params or {},
                            headers={"Accept": "application/json" if api else "text/html"}, check=False)
    except LoginNeeded as e:
        if guest_allowed:
            return GuestAllowed(e.detail or "a browser login is required")
        raise
    except ChallengeError as e:
        return refused(e.status, e.url, check=not api and e.status not in (401, 403))
    is_json = "json" in resp.headers.get("content-type", "")
    if resp.status in (401, 403) or (api and resp.ok and not is_json):
        reason = ""
        if api and is_json:
            body = resp.json()
            reason = f" ({body.get('reason') or body.get('message')})" if isinstance(body, dict) else ""
        return refused(resp.status, resp.url, reason=reason)
    if not resp.ok:
        raise resp.http_error()
    if api:
        return resp.json()
    html = resp.text()
    if parse.is_check_page(html):
        return refused(resp.status, resp.url, check=True)
    return html


def _more_comments(ctx, path: str, cursor: str) -> str | None:
    """The POST a page makes for "load more". It reads; None when Reddit refuses this one."""
    try:
        resp = ctx.http.request("POST", path, data={"cursor": cursor}, headers={"Accept": "text/html"}, check=False)
    except ChallengeError:
        return None
    return resp.text() if resp.ok else None


def _require_community(ctx, sub: str) -> None:
    """The guest fragments answer alike for a subreddit with nothing to show and for one that doesn't
    exist or that a guest can't see. Only the subreddit's own page says which."""
    html = _read(ctx, f"{sub}/")
    if parse.no_such_community(html):
        raise NoSuchItem(f"{sub[1:]}: Reddit couldn't find that community")
    if said := parse.community_block(html):
        raise SiteRefused(said)


def _unique(rows: list[dict]) -> list[dict]:
    """Rows by id, first copy kept: a page can repeat a row when Reddit's list shifts under it."""
    first: dict[str, dict] = {}
    for row in rows:
        first.setdefault(row["id"], row)
    return list(first.values())


def _guest_feed(ctx, sub: str, sort: str, time: str | None, limit: int, after: str | None):
    html = _read(ctx, f"/svc/shreddit/community-more-posts/{sort}/", {"name": sub[3:], "t": time, "after": after})
    links = [link_from(post) for post in parse.post_cards(html)][:limit]
    if not links and not after:
        _require_community(ctx, sub)
    return links, f"t3_{links[-1].id}" if links and parse.feed_has_more(html) else None


def _guest_search(ctx, sub: str, query: str, sort: str, time: str, after: str | None):
    html = _read(ctx, f"/svc/shreddit{sub}/search/", {"q": query, "type": "posts", "sort": sort, "t": time, "cursor": after})
    links = parse.search_links(html)
    if not links and sub and not after:
        _require_community(ctx, sub)
    if not links and not parse.no_search_hits(html):
        raise RuntimeError(f"reddit: no post cards in the search page for {query!r}; its layout may have changed")
    return links, parse.search_cursor(html) if links else None


def _listing(ctx, path: str, params: dict, sub: str, about: dict, empty: str, guest) -> Listing:
    """A page of posts. `--json` keeps `{after, posts}`; one subreddit leaves its column out. `guest`
    reads the same page without the API, and is None where a guest has no such page."""
    single = bool(sub) and "+" not in sub
    data = _read(ctx, path, params, kind="json", guest_allowed=guest is not None)
    links, after = guest() if isinstance(data, GuestAllowed) else links_from_listing(data)
    rows = [link_row(link) for link in links]
    return Listing(name="posts", rows=rows,
                   fields=("id", "title", "score", "comments") if single else ("id", "title", "subreddit", "score",
                                                                              "comments"),
                   about={"subreddit": sub[1:] or None, **about}, more={"after": after} if after else None,
                   empty=empty, next=[Next("thread", {"id": "<id>"}, "read a post with its comments")],
                   data={"after": after, "posts": rows})


@action(reddit, tier=Tier.READ)
def search(ctx, query: str, subreddit: str | None = None, sort: Literal["relevance", "hot", "top", "new", "comments"] = "relevance",
           time: Time = "all", limit: int = 25, after: str | None = None):
    """Search posts, site-wide or in one subreddit.

    Search matches words in titles and post text, not comments (`search-comments` covers those);
    read promising threads with `thread --id`. Columns: post id, title, subreddit (unless one subreddit) and comments;
    `--fields` adds score, date, author, flair, text (first 300 characters), permalink and url. A guest
    gets Reddit's page size, about 7 results, with no flair or text, and only all of Reddit or one
    subreddit.

    Reddit matches loosely and returns posts even for gibberish, so a result doesn't prove the words
    occur (check titles and text) and the number of results says little about how often a term is
    discussed.

    Args:
        query: Search words (Reddit search syntax, e.g. `title:sapphire`).
        subreddit: Only this subreddit (e.g. churning; join several with +).
        sort: relevance, hot, top, new or comments.
        time: Time window: hour, day, week, month, year or all. Reddit applies it only to relevance,
            top and comments, so new and hot need `all`.
        limit: Results per page (max 100).
        after: Cursor from the previous page's "next page" hint.
    """
    if time != "all" and sort in ("new", "hot"):
        same = {"query": query, "subreddit": subreddit, "limit": limit}
        in_window = command_line(ctx.site, ctx.action, {**same, "time": time}, placeholders=False)
        no_window = command_line(ctx.site, ctx.action, {**same, "sort": sort}, placeholders=False)
        raise ArgumentError(f"Reddit ignores --time {time} with --sort {sort}",
                            help=f"Run `{in_window}` for the most relevant posts in that window (or --sort top / "
                                 f"comments), or `{no_window}` without the window")
    sub = _sub(subreddit)
    params = {"q": query, "sort": sort, "t": time, "limit": limit, "after": after,
              "type": "link", "restrict_sr": 1 if sub else None, "include_over_18": "on"}
    return _listing(ctx, f"{sub}/search.json", params, sub, {"query": query, "sort": sort, "time": time},
                    f"no posts match {query!r} in {sub[1:] or 'all of Reddit'} ({time})",
                    None if "+" in sub else lambda: _guest_search(ctx, sub, query, sort, time, after))


@action(reddit, tier=Tier.READ)
def listing(ctx, subreddit: str | None = None, sort: Literal["hot", "new", "top", "rising", "controversial"] = "hot",
            time: Time = "day", limit: int = 25, after: str | None = None):
    """List a subreddit's posts (or the front page) by hot, new, top, rising or controversial.

    Columns as for `search`: post id (for `thread`), title, subreddit (unless one subreddit) and
    comments; `--fields score` adds the score. A guest can list one subreddit at a time, up to about 25 posts a page; several
    subreddits or the front page need a login.

    Args:
        subreddit: Subreddit name, e.g. churning (several joined with +); omit for the front page.
        sort: hot, new, top, rising or controversial.
        time: Window for top and controversial: hour, day, week, month, year or all.
        limit: Posts per page (max 100).
        after: Cursor from the previous page's "next page" hint.
    """
    sub, after = _sub(subreddit), _cursor(after, "t3")
    params = {"limit": limit, "after": after,
              "t": time if sort in ("top", "controversial") else None}
    return _listing(ctx, f"{sub}/{sort}.json", params, sub,
                    {"sort": sort, "time": params["t"]}, f"no {sort} posts in {sub[1:] or 'the front page'}",
                    (lambda: _guest_feed(ctx, sub, sort, params["t"], limit, after)) if sub and "+" not in sub else None)


@action(reddit, tier=Tier.READ)
def search_comments(ctx, subreddit: str, query: str, sort: Literal["relevance", "top", "new"] = "relevance",
                    time: Time = "all", after: str | None = None):
    """Search the text of comments in one subreddit. Works without a login.

    Reddit has refused guests here before (HTTP 403); a refusal is LOGIN NEEDED, and then `search`
    still covers titles and post text without one.

    Columns: post id (for `thread`), author, score and text; `--fields` adds date, id, post (its
    title), subreddit and url. The table shortens the text; `--json` has all of it. Reddit sets the
    page size, usually 10.

    Args:
        subreddit: One subreddit, e.g. churning.
        query: Words to find in comments.
        sort: relevance, top or new.
        time: Time window: hour, day, week, month, year or all.
        after: Cursor from the previous page's "next page" hint.
    """
    name = _one_sub(subreddit)
    html = _read(ctx, f"/svc/shreddit/r/{name}/search/",
                 {"q": query, "type": "comments", "sort": sort, "t": time, "cursor": after})
    rows = parse.search_comments(html)
    if not rows and not parse.no_search_hits(html):
        raise RuntimeError(f"reddit: no comment cards in the search page for {query!r}; its layout may have changed")
    cursor = parse.search_cursor(html) if rows else None
    return Listing(name="results", rows=rows, fields=("post_id", "author", "score", "text"),
                   about={"subreddit": name, "query": query, "sort": sort, "time": time},
                   more={"after": cursor} if cursor else None,
                   empty=f"no comments match {query!r} in r/{name} ({time})",
                   next=[Next("thread", {"id": "<post_id>"}, "read the post with its comments")])


@action(reddit, tier=Tier.READ)
def user(ctx, name: str, show: Literal["comments", "posts"] = "comments",
         sort: Literal["new", "hot", "top", "controversial"] = "new", time: Time = "all", after: str | None = None):
    """A user's public comments or posts. Works without a login.

    Comment columns: post id (for `thread`), subreddit, score and text; `--fields` adds date, id,
    post (its title), author and url. The table shortens the text; `--json` has all of it. Post
    columns: post id, title, subreddit and comments; `--fields` adds score, date, author, url and
    link, and only `--sort new` is available. Posts have no next page: `coverage` says so. Reddit
    sets the page size, usually 25.

    Args:
        name: Username, with or without `u/`.
        show: comments or posts.
        sort: new, hot, top or controversial.
        time: Window for top and controversial: hour, day, week, month, year or all.
        after: Cursor from the previous page's "next page" hint.
    """
    who = name.removeprefix("/").removeprefix("user/").removeprefix("u/").rstrip("/")
    if not _USER.match(who):
        raise ArgumentError(f"not a Reddit username: {name!r}", help="Use the name alone, e.g. --name spez")
    if show == "posts" and sort != "new":   # the site answers with an empty feed, not an error
        raise ArgumentError(f"--show posts can't use --sort {sort}", help="Use --sort new for posts")
    # the feed reads `sort` from the query; the sort in its path changes nothing
    html = _read(ctx, f"/svc/shreddit/profiles/profile_{show}-more-posts/new/",
                 {"name": who, "sort": sort, "t": time if sort in ("top", "controversial") else None, "after": after})
    if parse.no_such_user(html):
        raise NoSuchItem(f"u/{who} doesn't exist or is banned")
    about = {"user": who, "show": show, "sort": sort}
    cursor = parse.profile_cursor(html)
    more = {"after": cursor} if cursor else None
    empty = f"no {show} from u/{who}"
    if show == "posts":
        if not more:   # the post feed gives no cursor, so a short list isn't proof of the end
            about["coverage"] = "first page only: older posts may exist"
        return Listing(name="posts", rows=parse.profile_posts(html), fields=("id", "title", "subreddit", "comments"),
                       about=about, more=more, empty=empty,
                       next=[Next("thread", {"id": "<id>"}, "read a post with its comments")])
    return Listing(name="comments", rows=parse.profile_comments(html, who),
                   fields=("post_id", "subreddit", "score", "text"), about=about, more=more, empty=empty,
                   next=[Next("thread", {"id": "<post_id>"}, "read the post with its comments")])


@action(reddit, tier=Tier.READ)
def recent_comments(ctx, subreddit: str | None = None, limit: int = 25, after: str | None = None):
    """The newest comments in a subreddit (or all of Reddit), newest first.

    Columns: post id (for `thread`), author, score and text; `--fields` adds date, id, post (its
    title), subreddit and url. The table shortens the text; `--json` has all of it. A score is null
    while the subreddit still hides it. A guest can read one subreddit or several joined with +; all
    of Reddit needs a login.

    Args:
        subreddit: Subreddit name, e.g. churning (several joined with +); omit for all of Reddit.
        limit: Comments per page (max 100).
        after: Cursor from the previous page's "next page" hint.
    """
    sub, after = _sub(subreddit), _cursor(after, "t1")
    params = {"limit": limit, "after": after}
    data = _read(ctx, f"{sub}/comments.json", params, kind="json", guest_allowed=bool(sub))
    if isinstance(data, GuestAllowed):
        html = _read(ctx, f"{sub}/comments/", params)
        rows, cursor = parse.old_comments(html), parse.old_cursor(html)
        if not rows and not after and "+" not in sub:
            _require_community(ctx, sub)
    else:
        page = data.get("data") or {}
        rows = [parse.comment_row(c["data"]) for c in page.get("children") or [] if c.get("kind") == "t1"]
        cursor = page.get("after")
    return Listing(name="comments", rows=_unique(rows), fields=("post_id", "author", "score", "text"),
                   about={"subreddit": sub[1:] or None}, more={"after": cursor} if cursor and rows else None,
                   empty=f"no comments in {sub[1:] or 'all of Reddit'}",
                   next=[Next("thread", {"id": "<post_id>"}, "read the post with its comments")])


@action(reddit, tier=Tier.READ)
def duplicates(ctx, id: str, sort: Literal["num_comments", "new"] = "num_comments", limit: int = 25,
               after: str | None = None):
    """Other posts of the same link (Reddit's "other discussions"), in any subreddit.

    Finds where else a news article or deal page was posted and discussed. The original post is in
    the header; columns as for `search`: post id (for `thread`), title, subreddit and comments
    (`--fields score` adds the score). A text post has no link to share, so it has none.

    Args:
        id: Post id, `t3_` fullname, post URL or redd.it link.
        sort: num_comments (most discussed first) or new.
        limit: Posts per page (max 100).
        after: Cursor from the previous page's "next page" hint.
    """
    pid = _post_id(id)
    params = {"sort": sort, "limit": limit, "after": _cursor(after, "t3")}
    try:
        data = _read(ctx, f"/duplicates/{pid}.json", params, kind="json", guest_allowed=True)
        if isinstance(data, GuestAllowed):
            html = _read(ctx, f"/duplicates/{pid}/", params)
            found = [link_from(post) for post in parse.old_links(html)]
            original = next((link for link in found if link.id == pid), None)
            links, cursor = [link for link in found if link.id != pid], parse.old_cursor(html)
        else:
            original = link_from(post_data(data[0]))
            links, cursor = links_from_listing(data[1])
    except HttpError as e:
        if e.status == 404:
            raise NoSuchItem(f"no Reddit post {pid}") from e
        raise
    if original is None:
        raise RuntimeError(f"reddit: post {pid} is missing from its other-discussions page; its layout may have changed")
    rows = _unique([link_row(link) for link in links])
    about = {"post": pid, "title": one_line(original.title), "subreddit": original.subreddit, "link": original.url,
             "sort": sort}
    return Listing(name="posts", rows=rows, fields=("id", "title", "subreddit", "comments"), about=about,
                   more={"after": cursor} if cursor and rows else None,
                   empty=f"no other posts share the link of {pid}" if original.url else
                   f"{pid} is a text post: it has no link to share",
                   next=[Next("thread", {"id": "<id>"}, "read a post with its comments")])


@action(reddit, tier=Tier.READ)
def wiki(ctx, subreddit: str, page: str = "index", show: Literal["page", "history", "pages"] = "page",
         limit: int = 25, after: str | None = None):
    """A subreddit's wiki: one page with who last revised it, the page's revisions, or its page list.

    `--show page` (the default) prints the page; links to other wiki pages are in its text.
    `--show history` lists the page's revisions: id, author, date and reason; `--fields` adds page
    and url (the page as of that revision). A guest sees only the latest 3 revisions. `--show pages`
    lists every page of the wiki and needs a login: Reddit shows a guest no page list. Reddit's
    JavaScript check runs in a browser tab the first time.

    Args:
        subreddit: One subreddit, e.g. churning.
        page: Wiki page, e.g. index or faq/cards.
        show: page, history (its revisions) or pages (the wiki's page list).
        limit: Revisions per page for `--show history` (max 100).
        after: Cursor from the previous page's "next page" hint (`--show history`).
    """
    name = _one_sub(subreddit)
    if not _WIKI_PAGE.match(page):
        raise ArgumentError(f"not a wiki page: {page!r}", help="Use the path after /wiki/, e.g. --page index")
    if after and show != "history":
        raise ArgumentError(f"--after pages through --show history, not --show {show}",
                            help="Add --show history, or leave out --after")
    if show == "pages":
        return _wiki_pages(ctx, name)
    if show == "history":
        return _wiki_history(ctx, name, page, limit, after)
    found = _wiki_page(ctx, name, page)
    url = ctx.http.url(f"/r/{name}/wiki/{page}/")
    lines = [f"# r/{name} wiki/{page}", url]
    if found.revised_at:
        lines.append(f"Last revised by {found.revised_by} on {found.revised_at[:16].replace('T', ' ')} UTC")
    return Result(data={"subreddit": name, "page": page, "url": url, "revised_by": found.revised_by,
                        "revised_at": found.revised_at, "text": found.text},
                  text="\n".join([*lines, "", found.text]), summary="\n".join(lines))


def _wiki_page(ctx, name: str, page: str) -> parse.WikiPage:
    found = parse.wiki_page(_read(ctx, f"/r/{name}/wiki/{page}/"))
    if found is None:
        raise NoSuchItem(f"r/{name} has no readable wiki/{page}")
    return found


def _wiki_pages(ctx, name: str) -> Listing:
    """A guest gets no page list (Reddit answers "Page does not exist"), so a refusal is LOGIN NEEDED."""
    data = _read(ctx, f"/r/{name}/wiki/pages.json", kind="json")
    rows = [{"page": page, "url": f"{parse.SITE}/r/{name}/wiki/{page}"} for page in data.get("data") or []]
    return Listing(name="pages", rows=rows, fields=("page",), about={"subreddit": name},
                   empty=f"r/{name} has no wiki pages",
                   # a step on this same action repeats its own arguments, so it names a real page
                   next=[Next("wiki", {"subreddit": name, "page": rows[0]["page"], "show": "page"},
                              "read that page (or another from the list)")] if rows else [])


def _wiki_history(ctx, name: str, page: str, limit: int, after: str | None) -> Listing:
    about = {"subreddit": name, "page": page}
    data = _read(ctx, f"/r/{name}/wiki/revisions/{page}.json", {"limit": limit, "after": after}, kind="json",
                 guest_allowed=True)
    if isinstance(data, GuestAllowed):   # the page a guest gets shows the latest 3, with no next page
        if after:
            raise ctx.login_needed(detail=f"a guest sees only the latest revisions, not older pages; {data.detail}")
        rows = parse.page_revisions(name, page, _read(ctx, f"/r/{name}/wiki/revisions/{page}/"))[:limit]
        cursor = None
        about["coverage"] = "a guest sees only the latest revisions; a login shows them all"
    else:
        rows, cursor = parse.api_revisions(name, page, data)
    if not rows and not after:   # Reddit lists no revisions alike for a page that isn't there
        _wiki_page(ctx, name, page)
    return Listing(name="revisions", rows=rows, fields=("id", "author", "date", "reason"), about=about,
                   more={"after": cursor} if cursor and rows else None,
                   empty=f"Reddit lists no revisions of r/{name} wiki/{page}",
                   next=[Next("wiki", {"subreddit": name, "page": page, "show": "page", "limit": None, "after": None},
                             "read the page as it is now", when_empty=True)])


def _api_thread(ctx, pid: str, expand: int) -> tuple[dict, CommentTree, str | None] | GuestAllowed:
    data = _read(ctx, f"/comments/{pid}.json", {"limit": 500, "sort": "old"}, kind="json", guest_allowed=True)
    if isinstance(data, GuestAllowed):
        return data
    post_listing, comments = data
    tree = CommentTree()
    tree.add((comments.get("data") or {}).get("children"))
    refusal = None
    for _ in range(max(0, expand)):
        batch = tree.next_batch()
        if not batch:
            break
        data = _read(ctx, "/api/morechildren.json", {
            "api_type": "json", "link_id": f"t3_{pid}", "children": ",".join(batch), "sort": "old"},
            kind="json", guest_allowed=True)
        if isinstance(data, GuestAllowed):
            refusal = one_line(data.detail)
            if refusal.endswith(_LOGIN_DETAIL):
                refusal = refusal.removesuffix(_LOGIN_DETAIL) + (
                    "open reddit.com in the browser ESUM uses, log in if asked, then retry")
            break
        tree.add(more_things(data))
        tree.drop(batch)  # ids Reddit did not return are not retried
    return post_data(post_listing), tree, refusal


def _guest_post(ctx, pid: str, head: dict) -> dict:
    """The post's own page, or its embed page when a check keeps that from a guest."""
    page = _read(ctx, head["permalink"], guest_allowed=True)
    if isinstance(page, GuestAllowed):
        page = ""
    found = next((card for card in parse.post_cards(page) if card["id"] == pid), None)
    return found or parse.embed_post(_read(ctx, EMBED + head["permalink"]), head)


def _guest_thread(ctx, pid: str, expand: int) -> tuple[dict, CommentTree]:
    """The comment fragment answers for any subreddit in its path, and names the real one."""
    html = _read(ctx, f"/svc/shreddit/comments/r/all/t3_{pid}", {"sort": "old"})
    head = parse.comment_tree(html)
    if head is None:
        raise NoSuchItem(f"no Reddit post {pid}")
    post = _guest_post(ctx, pid, head)
    tree = CommentTree()

    def take(page: str) -> None:
        tree.add(parse.comment_things(page))
        tree.stubs.extend(parse.comment_stubs(page))
        tree.stubs.sort(key=lambda stub: "top-level=1" not in stub[0])   # they bring the most comments

    take(html)
    for _ in range(max(0, expand) * GUEST_CALLS):
        if not tree.stubs:
            break
        page = _more_comments(ctx, *tree.stubs[0])
        if page is None:
            break
        tree.stubs.pop(0)
        take(page)
    return post, tree


@action(reddit, tier=Tier.READ)
def thread(ctx, id: str, mode: Mode = "medium", from_: int | None = None, to: int | None = None,
           expand: int = 3):
    """A post and its comments in compact-post format.

    The post is #1 and comments follow in time order; `↩#M` names the comment a reply answers.
    Reddit returns at most about 500 comments per request and hides the rest behind "load more"
    stubs; `--expand N` fetches up to N more batches of 100 of them. A guest starts with about 25,
    and `--expand N` makes up to 10×N smaller calls. Whatever is still missing is reported on the
    last line. `medium` shows the first and last 30 loaded posts and any stickied comment, `full` all.

    Args:
        id: Post id (the part after /comments/ in the URL), `t3_` fullname, post URL or redd.it link.
        mode: medium (head + tail) or full.
        from_: First post number to show (overrides mode).
        to: Last post number to show (overrides mode).
        expand: Extra requests for comments behind "load more" stubs (100 comments each; 0 = none).
    """
    pid = _post_id(id)
    data = _api_thread(ctx, pid, expand)
    if isinstance(data, GuestAllowed):
        post, tree = _guest_thread(ctx, pid, expand)
        refusal = None
    else:
        post, tree, refusal = data
    th = build_thread(post, tree)
    loaded = len(th.posts)
    wanted = set(select_numbers(loaded, mode, from_, to))
    if mode == "medium" and from_ is None and to is None:
        wanted.update(th.stickied)   # Reddit pins it first; time order can leave it mid-thread
    view = thread_view([p for p in th.posts if p.number in wanted], read_spans(wanted, loaded), count=th.total)
    note = missing_note(th, refusal=refusal)
    return view.result(th.title, url=th.link.permalink, data={"id": pid, "title": th.link.title, "subreddit": th.link.subreddit,
                        "url": th.link.permalink, "link": th.link.url, "total": th.total,
                        "loaded": loaded, "not_loaded": {"more_ids": th.pending, "deep_branches": th.deep,
                                                         "stubs": th.stubs, **({"refused": refusal} if refusal else {})}}, suffix="\n" + note if note else "",
                       next=view.range_steps({"id": pid}))
