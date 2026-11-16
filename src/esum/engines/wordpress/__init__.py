"""WordPress engine: blogs whose posts and comments are readable through the public WP REST API
(`/wp-json/wp/v2/…`), with the comment section as the thread.

Comments come from WP REST by default (this covers wpDiscuz, which stores ordinary WordPress
comments but shows only the newest few dozen on the page). A site whose comments live in Disqus sets
`extras={"comments": "disqus"}`; the thread then reads the Disqus embed instead, and pages a long
comment section through the Disqus API with the public key from the embed's own script.

Site extras:
    op_user     name shown for the article (#0), e.g. "DoC"
    op_aliases  commenter names that count as the blog's own staff (marked OP)
    comments    "wp" (default) or "disqus"
"""

from __future__ import annotations

import re
from urllib.parse import quote

from esum import (ArgumentError, NoSuchItem, Gap, HttpError, Listing, Mode, Next, Post, Tier, action, engine,
                  thread_view, read_spans, before_span, select_numbers, to_spans)
from esum.transport import TRANSIENT_ERRORS

from . import parse
from .parse import COMMENTS_PER_PAGE

FORMAT = 1
PORTED_FROM = ("xgoose skills/uscreditcardguide.source.xgs.js (github.com/xGoose-dev/xgoose); "
               "nitan_plugin My.user.js (WordPress adapter: WP REST + wpDiscuz comments, republished-post "
               "dating, linked-source selection)")

EXTRAS = ("op_user", "op_aliases", "comments")  # site extras the actions read, besides SITE_EXTRAS
wordpress = engine("wordpress", description="WordPress blogs over the public WP REST API: posts, search, "
                                            "categories, and each post's full comment section")

POST_FIELDS = "id,link,title,date_gmt,modified_gmt,content,comment_status"
LIST_FIELDS = "id,link,title,date_gmt,excerpt,categories,lang"
API = "/wp-json/wp/v2"
DISQUS_EMBED = "https://disqus.com/embed/comments/"
DISQUS_GAP = "Disqus serves only its oldest and newest 50 threads without an API key"
DISQUS_API = "https://disqus.com/api/3.0/posts/list.json"
DISQUS_PER_PAGE = 100   # the API's largest page
DISQUS_API_GAP = "the Disqus API didn't serve these comments"
_API_KEY_PARAM = re.compile(r"api_key=[^&\s]*")


# --- listings ----------------------------------------------------------------------------------------

def list_posts(ctx, *, query: str | None, since: str | None, category: str | None, limit: int,
               page: int, extra: dict | None = None, keep: dict | None = None) -> Listing:
    """Shared by `search` and `recent` (and site overrides that add parameters, e.g. a language).
    `extra` adds API parameters; `keep` holds the override's own flags that a hint to `recent` repeats."""
    after = parse.parse_since(since)
    params = {
        "search": query or None,
        "orderby": "relevance" if query else "date",
        "per_page": limit,
        "page": page,
        "after": parse.wp_after(after) if after else None,
        "categories": category_id(ctx, category) if category else None,
        "_fields": LIST_FIELDS,
        **(extra or {}),
    }
    resp = ctx.http.get(f"{API}/posts", params=params, check=False)
    past_end = resp.status == 400 and page > 1   # WP answers 400 past the last page
    if past_end:
        rows, total = [], None
    else:
        if not resp.ok:
            raise resp.http_error(resp.text())
        rows = parse.parse_post_list(resp.json())
        total = _int(resp.headers.get("x-wp-total"))
        if rows and _shows(ctx, "categories"):
            category_slugs(ctx, rows)
    # the cutoff as applied: a day stays a day, a relative or ISO time keeps its exact (UTC) time
    cutoff = None
    if after:
        cutoff = f"{after:%Y-%m-%d}" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", since.strip()) else parse.wp_after(after)
    about = {"query": query, "since": cutoff, "category": category,
             **(extra or {}), "page": page, "times": "UTC"}
    steps = [Next("thread", {"id": "<id>"}, "read a post with its comments")]
    running = ctx.action.name
    if past_end:
        empty = f"page {page} is past the last page"
        steps.append(Next(running, {"page": 1}, "start from the first page", when_empty=True))
    else:
        empty = "no posts" + (f" match {query!r}" if query else "") + (
            f" since {cutoff}" if after else "") + (f" in category {category}" if category else "")
        if running == "search":
            steps.append(Next("recent", dict(keep or {}), "list the newest posts instead", when_empty=True))
        elif since or category:
            steps.append(Next(running, {"since": None, "category": None, "page": None},
                              "list the newest posts without the filters", when_empty=True))
    return Listing(name="posts", rows=rows,
                   fields=("id", "date", "title", "excerpt") if query else ("id", "date", "title"),
                   about=about, total=total,
                   more={"page": page + 1} if rows and total and page * params["per_page"] < total else None,
                   empty=empty, next=steps)


def _shows(ctx, column: str) -> bool:
    """Whether the output includes a non-default list column (`--fields`, or every field in `--json`)."""
    fields = {f.strip() for f in (ctx.options.fields or "").split(",")}
    return ctx.options.json or "all" in fields or column in fields


def category_slugs(ctx, rows: list[dict]) -> None:
    """Replace the rows' category ids with slugs, in one request for the ids on this page."""
    ids = sorted({i for r in rows for i in r["categories"]})
    if not ids:
        return
    found = ctx.http.get_json(f"{API}/categories", params={"include": ",".join(map(str, ids)),
                                                           "per_page": 100, "_fields": "id,slug"})
    slugs = {c.get("id"): c.get("slug") for c in found or [] if c.get("slug")}
    for r in rows:
        r["categories"] = [slugs.get(i, i) for i in r["categories"]]


def category_id(ctx, category: str) -> int:
    """A category id or slug -> id."""
    if category.isdigit():
        return int(category)
    found = ctx.http.get_json(f"{API}/categories", params={"slug": category, "_fields": "id,slug"})
    if not isinstance(found, list):
        raise RuntimeError(f"{ctx.site.name}: category lookup returned no list (the site data may have changed)")
    if not found:
        raise ArgumentError(f"no category with slug {category!r} on {ctx.site.base_url}",
                            help=f"Run `esum {ctx.site.name} categories` to list the category slugs")
    return int(found[0]["id"])


@action(wordpress, tier=Tier.READ)
def search(ctx, query: str, since: str | None = None, category: str | None = None, limit: int = 20,
           page: int = 1):
    """Search posts by keyword, most relevant first.

    Each row has the post id (for `thread`), date, title and a short excerpt (`--fields url` adds
    the URL). Comment counts are not available from the search API.

    Args:
        query: Search words.
        since: Only posts dated on or after this (UTC): YYYY-MM-DD, an ISO time, or relative like 2w / 7d
            / 12h (counted from now to the minute).
        category: Only this category (id or slug; see `categories`).
        limit: Results per page (max 100).
        page: Result page.
    """
    return list_posts(ctx, query=query, since=since, category=category, limit=limit, page=page)


@action(wordpress, tier=Tier.READ)
def recent(ctx, since: str | None = None, category: str | None = None, limit: int = 20, page: int = 1):
    """Recent posts, newest first, optionally since a date or in one category.

    A republished post (re-dated when the blog updates it) shows its new date here, and its excerpt
    can still be the old text. The API keeps no original date (`modified` is only the last edit), so
    a list can't mark these posts: `thread` says `Republished:` and gives the first comment's date.

    Args:
        since: Only posts dated on or after this (UTC): YYYY-MM-DD, an ISO time, or relative like 2w / 7d
            / 12h (counted from now to the minute).
        category: Only this category (id or slug; see `categories`).
        limit: Results per page (max 100).
        page: Result page.
    """
    return list_posts(ctx, query=None, since=since, category=category, limit=limit, page=page)


@action(wordpress, tier=Tier.READ)
def categories(ctx, query: str | None = None):
    """Post categories with their ids, slugs and post counts (for `--category`).

    Args:
        query: Only categories whose name or slug contains this.
    """
    rows: list[dict] = []
    for page in range(1, 6):
        resp = ctx.http.get(f"{API}/categories", params={"per_page": 100, "page": page, "orderby": "count",
                                                        "order": "desc", "_fields": "id,name,slug,count,parent"})
        rows += parse.parse_categories(resp.json())
        if page >= (_int(resp.headers.get("x-wp-totalpages")) or 1):
            break
    if query:
        q = query.lower()
        rows = [r for r in rows if q in r["name"].lower() or q in r["slug"].lower()]
    return Listing(name="categories", rows=rows, fields=("id", "slug", "count", "name"),
                   about={"query": query}, total=len(rows),
                   empty=f"no category name or slug contains {query!r}" if query else "the blog has no categories",
                   next=[Next("recent", {"category": "<slug>"}, "list a category's recent posts")])


# --- the thread --------------------------------------------------------------------------------------

@action(wordpress, tier=Tier.READ)
def thread(ctx, id: str, mode: Mode = "medium", from_: int | None = None, to: int | None = None,
           since: str | None = None):
    """A post and its full comment section in compact-post format.

    The article is #0 and comments are #1… oldest first; `↩#M` marks a reply. Comments written by
    the blog's own staff are marked OP. `medium` shows the article (cut at 8,000 characters) plus
    the first and last 30 comments; `full` shows everything. When a post was republished (re-dated
    on update), the header says so and the article is dated by its first comment. The pages the
    article and comments link to most (minus referral links) are listed at the end as external
    sources. On a long thread, `--since 30d` reads the article plus the recent comments only, which
    is where the current data points are when an old post keeps being republished.

    Args:
        id: Post id, slug, or post URL.
        mode: medium (article + first/last 30 comments) or full.
        from_: First comment number to read (overrides mode; 0 includes the article).
        to: Last comment number to read (overrides mode).
        since: Only comments after this (article included): YYYY-MM-DD, or relative like 2w / 30d / 12h.
    """
    after = parse.parse_since(since)
    post = load_post(ctx, id)
    url = post.get("link") or ctx.http.url(f"/?p={post.get('id')}")
    extras = ctx.site.extras
    aliases = tuple(extras.get("op_aliases") or ())
    if extras.get("comments") == "disqus":
        total, posts, first_seen, coverage = disqus_comments(ctx, url, aliases, mode, from_, to, after)
        source = "Disqus"
    else:
        total, posts, first_seen, coverage = wp_comments(ctx, post, url, aliases, mode, from_, to, after)
        source = ""
    meta = parse.thread_meta(post, first_seen)
    ranged = from_ is not None or to is not None
    article = None
    if from_ is None or from_ <= 0:
        cap = parse.ARTICLE_CHAR_CAP if mode == "medium" and not ranged else None
        article = parse.article_post(post, extras.get("op_user") or ctx.site.name, meta, cap)
    if article is not None:
        posts.insert(0, article)
    view = thread_view(posts, coverage, count=total + 1)
    title = parse.rendered_label(post.get("title")) or f"Post {post.get('id')}"
    line = parse.meta_line(meta, source)
    shown = [p for p in view.items if isinstance(p, Post)]
    links = parse.pick_links(shown, url)
    suffix = parse.render_links(links)
    steps = view.range_steps({"id": str(post.get("id"))})
    if after is None and not ranged and mode == "medium" and any(isinstance(i, Gap) for i in view.items):
        steps.append(Next("thread", {"id": str(post.get("id")), "since": "30d"},
                          "read the article and the last 30 days of comments"))
    data = {
        "id": post.get("id"), "title": title, "url": url, "comments": total,
        "published": meta.published, "republished": meta.republished, "first_comment": meta.first_comment,
        "updated": meta.updated, "comments_closed": meta.closed, "comment_source": source or "wordpress",
        "since": after, "linked_sources": links,
    }
    notes = [" · ".join(b for b in (line, "Times: UTC") if b)]
    if after is not None and not any(isinstance(p, Post) and p is not article for p in shown):
        notes.append(_none_since(after, first_seen, total))
    result = view.result(title, url=url, data=data, notes=notes, suffix=suffix, next=steps)
    if article and "[cut at" in article.text:
        result.summary += "\nThe article is cut: --from 0 --to 0 reads all of it"
    return result


def _none_since(after, seen: list, total: int) -> str:
    """Says a `--since` read found no comment, and when the newest comment ESUM saw was written."""
    dated = [c for c in seen if getattr(c, "when", None)]
    if not dated:
        return f"No comments after {after:%Y-%m-%d %H:%M} UTC; the post has {total} comments"
    newest = max(dated, key=lambda c: c.when)
    number = getattr(newest, "number", None)
    return (f"No comments after {after:%Y-%m-%d %H:%M} UTC; the newest is "
            f"{f'#{number}, ' if number else ''}{newest.when:%Y-%m-%d} (of {total})")


def load_post(ctx, ref: str) -> dict:
    kind, value = parse.post_ref(str(ref))
    if kind == "id":
        resp = ctx.http.get(f"{API}/posts/{value}", params={"_fields": POST_FIELDS},
                            headers={"Accept": "application/json"}, check=False)
        if resp.ok:
            return resp.json()
        if resp.status == 404 and _error_code(resp) == "rest_post_invalid_id":
            raise _no_post(ctx, f"no post with id {value} on {ctx.site.base_url}")
        raise resp.http_error()
    found = ctx.http.get_json(f"{API}/posts", params={"slug": value, "_fields": POST_FIELDS})
    if not isinstance(found, list):
        raise RuntimeError(f"{ctx.site.name}: post lookup returned no list (the site data may have changed)")
    if not found:
        raise _no_post(ctx, f"no post with slug {value!r} on {ctx.site.base_url}")
    return found[0]


def _error_code(resp) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body.get("code") if isinstance(body, dict) else None


def _no_post(ctx, message: str) -> NoSuchItem:
    error = NoSuchItem(message)
    site = ctx.site.name
    error.help = f"Find the post's id with `esum {site} search --query <words>` or `esum {site} recent`"
    return error


def wp_comments(ctx, post: dict, url: str, aliases: tuple[str, ...], mode: str, from_: int | None,
                to: int | None, after=None):
    """-> (total, posts, comments seen for dating, coverage spans). Fetches only the pages holding the
    wanted comment numbers, plus page 1 (its first comment dates a republished post). With `after`,
    the comments before it are counted (one request) and skipped, so numbers stay the thread's."""
    pid = post.get("id")

    def page(n: int, per_page: int = COMMENTS_PER_PAGE, **extra):
        resp = ctx.http.get(f"{API}/comments", check=False, params={
            "post": pid, "per_page": per_page, "page": n, "orderby": "date_gmt", "order": "asc",
            "_fields": "id,parent,author,author_name,date_gmt,content", **extra})
        if resp.status == 400 and n > 1:
            return resp, []   # past the last page
        if not resp.ok:
            raise resp.http_error(resp.text())
        return resp, resp.json() or []

    first_resp, first_items = page(1)
    total = _int(first_resp.headers.get("x-wp-total"))
    if total is None:
        total = len(first_items)
    cutoff_start = None
    if after is not None:
        select_numbers(total, mode, from_, to)   # the caller's own range must still make sense
        resp, _ = page(1, per_page=1, before=parse.wp_after(after))
        older = _int(resp.headers.get("x-wp-total")) or 0
        cutoff_start = older + 1
        from_ = max(from_ or 1, cutoff_start)
    # after a cutoff the start is ESUM's, not the caller's: it may pass the end or --to
    wanted = set(select_numbers(total, mode, from_, to)) if after is None else set(range(from_, min(total, to or total) + 1))
    comments = parse.parse_wp_comments(first_items, url, aliases, start=1)
    pages = sorted({(n - 1) // COMMENTS_PER_PAGE + 1 for n in wanted} - {1})
    for n in pages:
        _, items = page(n)
        comments += parse.parse_wp_comments(items, url, aliases, start=(n - 1) * COMMENTS_PER_PAGE + 1)
    number_of = {c.id: c.number for c in comments}
    keep = [c for c in comments if c.number in wanted]
    spans = read_spans(wanted, total)
    if after is not None:
        spans.insert(0, before_span(after, min(total, cutoff_start - 1)))
    return total, parse.to_posts(keep, number_of), comments, spans


def disqus_comments(ctx, url: str, aliases: tuple[str, ...], mode: str, from_: int | None, to: int | None,
                    after=None):
    """-> (total, posts, comments seen, coverage spans). The WordPress Disqus plugin names the thread in
    the post page's `embedVars`; the Disqus embed page then serves the oldest (asc) or newest (desc)
    50 threads with their replies. Up to ~100 comments the thread is complete. Beyond that the
    Disqus API, with the public key from the embed's own script, pages through the comments the
    call needs; if that fails, the middle shows as a gap that says why."""
    cfg = parse.disqus_config(ctx.http.get_text(url))
    if cfg is None:
        raise RuntimeError(f"no Disqus thread configured on {url} (the layout may have changed)")
    shortname, ident = cfg

    def embed(order: str) -> parse.DisqusPage:
        q = f"?base=default&f={quote(shortname)}&t_i={quote(ident)}&t_u={quote(url, safe='')}&s_o={order}"
        return parse.parse_disqus_embed(ctx.http.get_text(DISQUS_EMBED + q), url, aliases)

    oldest = embed("asc")
    gap, via_api = DISQUS_GAP, False
    head, tail, exhausted = oldest.comments, [], not oldest.has_next
    if oldest.has_next:
        wanted0 = None if after is not None else set(select_numbers(oldest.total, mode, from_, to))
        try:
            head, tail, exhausted = disqus_api_read(ctx, oldest, url, aliases, wanted0, after)
            gap, via_api = DISQUS_API_GAP, True
        except DisqusApiUnavailable as e:
            gap = f"{DISQUS_GAP}, and the API route failed: {e}"
            tail = embed("desc").comments
    seen = {c.id for c in head}
    newest = [c for c in tail if c.id not in seen]
    api_ends_met = via_api and len(newest) < len(tail)
    complete = exhausted or api_ends_met or len(head) + len(newest) >= oldest.total
    if complete:
        everything = head + newest
        parse.number_comments(everything)
        total = len(everything)
    else:
        total = oldest.total
        parse.number_comments(head, 1)
        parse.number_comments(newest, max(len(head) + 1, total - len(newest) + 1))
        everything = head + newest
    number_of = {c.id: c.number for c in everything}
    requested_from = from_
    cutoff_start = None
    if after is not None:
        select_numbers(total, mode, from_, to)   # the caller's own range must still make sense
        # the middle can't be read, so a date filter can only cover the comments that were served
        dated = [c for c in everything if c.when and c.when > after]
        cutoff_start = min((c.number for c in dated), default=total + 1)
        from_ = max(from_ or 1, cutoff_start)
        first_new = min((c.when for c in newest if c.when), default=None)
        if not complete and first_new and first_new > after:
            cutoff_start = min(cutoff_start, len(head) + 1)
            from_ = max(requested_from or 1, cutoff_start)   # comments since the date may sit in the middle
    # after a cutoff the start is ESUM's, not the caller's: it may pass the end or --to
    wanted = set(select_numbers(total, mode, from_, to)) if after is None else set(range(from_, min(total, to or total) + 1))
    keep = [c for c in everything if c.number in wanted and (after is None or (c.when and c.when > after))]
    unreachable = wanted - set(number_of.values())
    spans = [(a, b, gap) for a, b in to_spans(unreachable)] + read_spans(wanted, total)
    if after is not None:
        spans.insert(0, before_span(after, min(total, cutoff_start - 1)))
    return total, parse.to_posts(keep, number_of), oldest.comments + everything, spans


class DisqusApiUnavailable(Exception):
    pass


def disqus_pages(total: int, wanted: set[int], per_page: int = DISQUS_PER_PAGE) -> tuple[int, int]:
    """How many `posts/list` pages to read oldest-first and newest-first so that every number in
    `wanted` (1-based, time order, highest `total`) is on one of them -> (asc pages, desc pages).

    Oldest-first page k holds #k*per_page+1 … #(k+1)*per_page; newest-first page k holds
    #total-(k+1)*per_page+1 … #total-k*per_page. An empty `wanted` needs no pages.

    Fewest requests wins; on a tie the oldest end does more, because its numbers are exact while
    newest-first numbers count down from Disqus's total, which can differ from the real count.
    """
    nums = sorted(n for n in wanted if 1 <= n <= total)
    if not nums:
        return 0, 0
    best = None
    for split in range(len(nums) + 1):
        asc = -(-nums[split - 1] // per_page) if split else 0
        desc = -(-(total - nums[split] + 1) // per_page) if split < len(nums) else 0
        if best is None or asc + desc <= sum(best):
            best = (asc, desc)
    return best


def disqus_api_read(ctx, first: parse.DisqusPage, url: str, aliases: tuple[str, ...],
                    wanted: set[int] | None, after) -> tuple[list, list, bool]:
    """-> (oldest-first comments, newest-first comments, whether a walk reached the end). With
    `after`, reads newest-first until a page reaches back past it; otherwise the pages
    `disqus_pages` picks for `wanted`. The key comes from the embed's script on every call and is
    scrubbed from any error, because errors print the request URL."""
    try:
        if not first.thread_id or not first.key_script_url:
            raise DisqusApiUnavailable("the embed page names no thread id or script")
        key = parse.disqus_api_key(ctx.http.get_text(first.key_script_url))
        if not key:
            raise DisqusApiUnavailable("no public API key in the Disqus embed script")

        def walk(order: str, pages: int | None) -> tuple[list, bool]:
            out, cursor, n = [], None, 0
            while pages is None or n < pages:
                data = ctx.http.get_json(DISQUS_API, params={"api_key": key, "thread": first.thread_id,
                                                             "limit": DISQUS_PER_PAGE, "order": order,
                                                             "cursor": cursor})
                got, cursor = parse.parse_disqus_posts(data, url, aliases)
                out += got
                n += 1
                if cursor is None:
                    return out, True
                if after is not None and any(c.when and c.when <= after for c in got):
                    break
            return out, False

        if after is not None:
            tail, done = walk("desc", None)
            return [], tail, done
        asc, desc = disqus_pages(first.total, wanted or set())
        head, done = walk("asc", asc) if asc else ([], False)
        if done:
            return head, [], True
        tail, done = walk("desc", desc) if desc else ([], False)
        return head, tail, done
    except (HttpError, ValueError, *TRANSIENT_ERRORS) as e:
        msg = str(e) if isinstance(e, HttpError) else f"{type(e).__name__}: {e}"
        raise DisqusApiUnavailable(_API_KEY_PARAM.sub("api_key=<api_key>", msg)) from None


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
