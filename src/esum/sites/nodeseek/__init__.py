"""NodeSeek (www.nodeseek.com): a Chinese forum on VPS, servers, hosting deals and networking.

Cloudflare refuses plain HTTP clients, so the site reads through T2 (curl_cffi with Chrome's
fingerprint). Every page embeds its data as base64 JSON (`window.__config__`); post pages carry
their comments there in Markdown, list pages are server-rendered HTML. Guests can read the lists
and most posts; posts with a read level (lock 1+) and search are for members. T2 borrows the ESUM
browser's cookies, so once the user has logged in there, those posts read as that member. Search
is not built: guests are redirected to a Google `site:` search.
"""

from __future__ import annotations

import re
from typing import Literal

from esum import (NoSuchItem, SiteRefused, ArgumentError, HttpError, Listing, Mode, Next, Tier, Transport,
                  action, thread_view, read_pages, read_spans, select_numbers, site)

from . import parse

FORMAT = 1
PORTED_FROM = "original work (no external source)"
EXTRAS = ()

nodeseek = site(
    name="nodeseek",
    base_url="https://www.nodeseek.com",
    transport=Transport.BROWSER_HTTP,   # T2 with the browser's login: posts with a read level are for members
    fallback=Transport.IN_PAGE,         # T3 when Cloudflare challenges T2 (D-40)
    label={"en": "NodeSeek (nodeseek.com): Chinese forum on VPS, servers, hosting deals and networking; "
                 "provider reviews, offers, buy/sell posts and outage reports",
           "zh": "NodeSeek：VPS、服务器、主机优惠与网络技术论坛"},
    extras={"min_interval": 1.0},
)

Order = Literal["last-reply", "newest"]
_CATEGORY = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def get_page(ctx, path: str, what: str, params: dict | None = None) -> tuple[str, dict]:
    """A page's HTML and its embedded config. A post a guest may not open stops with the
    login-needed message: the old members-only notice, or now the site's 404 error page, which a guest
    also gets for a removed post. The site's other message pages and HTTP errors pass through."""
    resp = ctx.http.get(path, params=params, check=False)
    body = resp.text()
    config = parse.page_config(body)
    if resp.ok and config is not None:
        return body, config
    msg = parse.page_message(body) if config is not None else None
    if msg and parse.MEMBERS_ONLY in msg and parse.is_guest(config):
        raise ctx.login_needed(detail=f"nodeseek {what}: {msg} (for registered members; log in to nodeseek.com "
                                      "in that window, then retry)")
    if resp.status == 404 and config is not None and config.get("pageType") == "error":
        # A guest gets the same error page for a members-only post as for a removed one.
        said = f"{msg}, " if msg else ""
        if parse.is_guest(config):
            raise ctx.login_needed(detail=f"nodeseek {what}: {said}HTTP 404: the post is for members or was "
                                          "removed (log in to nodeseek.com in that window, then retry)")
        raise NoSuchItem(f"nodeseek {what}: {said}HTTP 404: the post was removed or is closed to this account")
    if msg:
        raise SiteRefused(f"nodeseek {what}: {msg}")
    if resp.status == 404:
        raise NoSuchItem(f"nodeseek {what}: not found (HTTP 404)")
    if not resp.ok:
        raise resp.http_error(body[:2000])
    raise RuntimeError(f"nodeseek {what}: no page data in {resp.url} (the layout may have changed)")


@action(nodeseek, tier=Tier.READ)
def categories(ctx):
    """The category list: key (for `listing --category`), name, and whether the front page shows it.

    Categories with `in_site_list` false are left out of the front page; read them with
    `listing --category`. `--fields all` adds in_nav and admin_only (staff only).
    """
    _, config = get_page(ctx, "/", "home page")
    found = parse.categories(config)
    if not found:
        raise RuntimeError("nodeseek: no categories in the home page data (the layout may have changed)")
    return Listing(name="categories", rows=found, fields=("key", "name", "in_site_list"),
                   next=[Next("listing", {"category": "<key>"}, "list a category's posts")],
                   data={"categories": found})


@action(nodeseek, tier=Tier.READ)
def listing(ctx, category: str | None = None, page: int = 1, order: Order = "last-reply"):
    """One page of posts (up to about 100 rows), site-wide or for one category.

    Columns: post id (for `thread`), title, replies and flags; `--fields` adds category (its key),
    author, views, last_reply (UTC), last_reply_by and url. Pinned posts come first. Flags:
    `pinned`, `read level N` (the site's lock: guests can't open the post; members need level N)
    and `read-only`. The site shows the start date of a post only on the post itself. The site's
    pager stops at page 100.

    Args:
        category: A category key (e.g. daily, tech, info, review, trade; see `categories`).
        page: Page of the list.
        order: last-reply (latest reply first) or newest (newest posts first).
    """
    if category is not None and not _CATEGORY.match(category):
        raise ArgumentError(f"not a category key: {category!r} (see `esum nodeseek categories`)")
    page = max(page, 1)
    prefix = f"/categories/{category}" if category else ""
    path = f"{prefix}/page-{page}" if page > 1 else (prefix or "/")
    params = {"sortBy": "postTime"} if order == "newest" else None
    html, _ = get_page(ctx, path, f"category {category}" if category else "post list", params)
    data = parse.parse_list_page(html)
    if data["page"] != page and not data["threads"]:
        raise RuntimeError(f"nodeseek: no list rows or matching page {page} (the layout may have changed)")
    if data["page"] != page:   # past the last page the site redirects to its home page
        raise ArgumentError(f"{category or 'the site-wide list'} has no page {page}",
                            help="the site serves about 100 pages of a list; use a lower --page")
    data.update(category=category, order=order, url=ctx.http.url(path, params))
    return Listing(name="threads", rows=data["threads"], fields=("id", "title", "replies", "flags"),
                   about={"category": category or "all", "sort": order, "page": data["page"], "url": data["url"]},
                   more={"page": data["page"] + 1} if data["has_next"] else None,
                   empty=f"no posts on page {page} of {category or 'the site-wide list'}",
                   next=[Next("thread", {"id": "<id>"}, "read a post with its comments")], data=data)


@action(nodeseek, tier=Tier.READ)
def thread(ctx, id: str, mode: Mode = "medium", from_: int | None = None, to: int | None = None):
    """A post and its comments in compact-post format, numbered by the site's floors.

    The post is #0 and comment floor N is #N, so the `#N` links in comments match. Pages hold 10
    floors; only the pages holding wanted floors are fetched (plus the last page, which gives the
    floor count). `↩#M` marks a comment that starts with the site's reply link to floor M. 👍 is
    the site's free like (点赞); a `[🍗N chicken legs]` line counts paid likes (加鸡腿). Text is
    the author's Markdown. Missing floors are deleted comments.

    Args:
        id: Post id or post URL (nodeseek.com/post-<id>-<page>).
        mode: medium (the post plus the first and last 30 comments) or full.
        from_: First comment floor to read (overrides mode; 0 includes the post).
        to: Last comment floor to read (overrides mode).
    """
    pid = parse.thread_ref(id)

    def load(n: int) -> parse.PostPage:
        _, config = get_page(ctx, f"/post-{pid}-{n}", f"post {pid}" + (f" page {n}" if n > 1 else ""))
        return parse.parse_post_page(config, pid)

    first = load(1)
    pages = {1: first}
    if first.pages > 1:
        pages[first.pages] = load(first.pages)
    total = parse.last_floor(pages[first.pages])
    wanted = set(select_numbers(total, mode, from_, to))
    read = read_pages(lambda n: load(n) if n <= first.pages else None, pages, wanted,
                      parse.page_of, lambda p: ((f.floor, f) for f in p.floors))
    if 0 in read.floors and (from_ is None or from_ <= 0):
        wanted.add(0)
    view = thread_view([parse.to_post(f, pid) for n, f in read.floors.items() if n in wanted],
                       read_spans(wanted, total), count=total + 1)
    url = parse.floor_url(pid, 0)
    line = " · ".join(b for b in (first.category and f"Category: {first.category_name or first.category} "
                                                     f"[{first.category}]",
                                  f"{total} comments in {first.pages} page(s) of {parse.PER_PAGE}", "Times: UTC",
                                  f"{first.views} views", first.locked and "locked") if b)
    data = {"id": pid, "title": first.title, "url": url, "category": first.category,
            "category_name": first.category_name, "comments": total, "pages": first.pages,
            "views": first.views, "locked": first.locked}
    return view.result(first.title, url=url, data=data, notes=[line], next=view.range_steps({"id": str(pid)}))
