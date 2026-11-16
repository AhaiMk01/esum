"""Offline tests for the Reddit module.

The fixtures are hand-made in the shape of Reddit's JSON API (Listing / t3 / t1 / more and the
`api/morechildren` envelope) with invented content, because Reddit blocks plain HTTP from the
machine the module was written on (see the site doc's Quirks)."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from esum.core.registry import Registry
from esum.sites.reddit import parse
from esum.sites.reddit.parse import CommentTree, build_thread, links_from_listing, missing_note, more_things
from esum.transport import Response
from esum import Transport

FIX = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def test_listing_links_and_cursor():
    links, after = links_from_listing(load("search.json"))
    assert after == "t3_fix002"
    a, b = links
    assert a.id == "fix001" and a.subreddit == "r/churning" and a.flair == "Data Point"
    assert a.url is None and a.text.startswith("Called today")
    assert a.permalink.startswith("https://www.reddit.com/r/churning/comments/fix001/")
    assert b.url == "https://example.com/chase-portal-news"


def test_comment_tree_collects_nested_replies_and_stubs():
    post_listing, comments = load("comments.json")
    tree = CommentTree()
    tree.add(comments["data"]["children"])
    assert set(tree.comments) == {"ca1", "ca2", "cb1", "cb2"}
    assert tree.pending == ["cc1", "cc2"]
    assert tree.deep == 1


def test_thread_numbering_reply_links_and_missing_note():
    post_listing, comments = load("comments.json")
    tree = CommentTree()
    tree.add(comments["data"]["children"])
    th = build_thread(parse.post_data(post_listing), tree)
    assert th.title == "Sapphire Preferred retention offer DP (r/churning)"
    assert th.total == 10  # 9 comments counted by Reddit + the post
    got = [(p.number, p.user, p.reply_to, p.is_op) for p in th.posts]
    assert got == [(1, "example_op", None, True), (2, "user_a", None, False),
                   (3, "[deleted]", None, False), (4, "example_op", 2, True), (5, "user_b", 3, False)]
    assert th.posts[0].text.startswith("Flair: Data Point")
    note = missing_note(th)
    assert "2 comments still behind 'load more'" in note and "1 deeply nested" in note


def test_morechildren_expands_pending():
    post_listing, comments = load("comments.json")
    tree = CommentTree()
    tree.add(comments["data"]["children"])
    batch = tree.next_batch()
    tree.add(more_things(load("morechildren.json")))
    tree.drop(batch)
    th = build_thread(parse.post_data(post_listing), tree)
    assert tree.pending == []
    assert [p.user for p in th.posts][-2:] == ["user_c", "user_d"]
    assert th.posts[-1].reply_to == th.posts[-2].number
    assert "load more" not in missing_note(th)


def test_morechildren_errors_raise():
    with pytest.raises(RuntimeError):
        more_things({"json": {"errors": [["RATELIMIT", "slow down"]]}})


# --- whole actions on a fake backend standing in for the T2 browser transport ----------------------

SUBREDDIT_PAGES = {"nosuchsub": "guest_no_community.html", "bannedsub": "guest_banned.html",
                   "privatesub": "guest_private.html", "premiumsub": "guest_premium.html",
                   "quarantinesub": "guest_quarantined.html",
                   "checkedsub": '<form hidden method="GET"><input type="hidden" name="js_challenge" value="1"/></form>',
                   "quietsub": "<main><shreddit-feed></shreddit-feed></main>",
                   "gatedsub": "<main>Mature Content</main>"}


class PathBackend:
    def __init__(self, blocked: bool = False, guest: bool = False, broken: tuple[str, ...] = (), pages: dict | None = None):
        self.requests = []
        self.pages = pages or {}   # a test's own bodies by path; `embed:<path>` for the embed host
        self.blocked = blocked
        self.guest = guest         # the JSON API refuses, and the fragments a guest reads answer
        self.broken = broken       # paths that act up: see `page`
        self.in_page = False       # the call moved to a browser tab

    def send(self, request, *, before_send=lambda: None):
        before_send()
        self.requests.append(request)
        url = urlsplit(request.url)
        query = parse_qs(url.query)
        path = url.path
        if self.blocked or (self.guest and path.endswith(".json")):
            return Response(403, request.url, {"content-type": "text/html"}, b"<body>blocked</body>")
        if "more-comments" in path and "more-comments-check" in self.broken:
            return Response(200, request.url, {"content-type": "text/html"},
                            b'<input type="hidden" name="js_challenge" value="1"/>')
        if "more-comments" in path and "more-comments-cloudflare" in self.broken:
            return Response(403, request.url, {"cf-mitigated": "challenge"}, b"Just a moment")
        if "more-comments" in self.broken and "more-comments" in path:
            return Response(429, request.url, {"content-type": "text/html"}, b"slow down")
        if path.endswith(".json") and path in self.pages:
            return Response(200, request.url, {"content-type": "application/json"}, self.pages[path])
        if page := self.own_page(path, url.hostname) or self.page(path, query, url.hostname):
            return Response(200, request.url, {"content-type": "text/html; charset=utf-8"}, page)
        if path == "/r/nosuchsub/hot.json":
            return Response(200, request.url, {"content-type": "application/json"}, b'{"data": {"children": []}}')
        name = {"/r/churning/search.json": "search.json", "/comments/fix001.json": "comments.json",
                "/api/morechildren.json": "morechildren.json"}.get(path)
        if not name:
            return Response(404, request.url, {"content-type": "application/json"}, b'{"error": 404}')
        return Response(200, request.url, {"content-type": "application/json; charset=UTF-8"},
                        (FIX / name).read_bytes())

    def own_page(self, path, host):
        if host == "embed.reddit.com":
            return self.pages.get("embed:" + path)
        if "post" in self.broken and path in self.pages and "/svc/" not in path:
            return b'<form hidden method="GET"><input type="hidden" name="js_challenge" value="1"/></form>'
        return self.pages.get(path)

    def page(self, path, query, host="www.reddit.com"):
        """The HTML pages a guest can read. What a real page says when there is nothing to show."""
        check = b'<form hidden method="GET"><input type="hidden" name="js_challenge" value="1"/></form>'
        if path.startswith("/svc/shreddit/community-more-posts/"):
            return b"<main></main>" if query["name"][0] in SUBREDDIT_PAGES else (FIX / "guest_feed.html").read_bytes()
        if (m := re.fullmatch(r"/r/(\w+)/", path)) and m.group(1) in SUBREDDIT_PAGES:
            name = SUBREDDIT_PAGES[m.group(1)]
            return (FIX / name).read_bytes() if name.endswith(".html") else name.encode()
        if (m := re.fullmatch(r"/svc/shreddit/r/(\w+)/search/", path)) and m.group(1) in SUBREDDIT_PAGES:
            # a premium subreddit's search page has no words of its own, as on the real site
            return b"<main></main>" if m.group(1) == "premiumsub" else "<div>we couldn’t find any results</div>".encode()
        if path in ("/svc/shreddit/search/", "/svc/shreddit/r/awardtravel/search/"):
            words = query["q"][0]
            if words == "nomatch":
                return "<div>Hm...we couldn’t find any results for nomatch</div>".encode()
            return b"<div>a page in a new layout</div>" if words == "newlayout" else (FIX / "guest_search.html").read_bytes()
        if path.startswith("/svc/shreddit/comments/r/"):
            if "comments" in self.broken:
                return check
            return b"<main>no tree</main>" if path.endswith("t3_nopost") else (FIX / "guest_comments.html").read_bytes()
        if path.startswith("/svc/shreddit/more-comments/"):
            return (FIX / "guest_more.html").read_bytes()
        if path == "/r/churning/comments/fix001/sapphire/" and host == "embed.reddit.com":
            return b"<main>not embeddable</main>" if "embed" in self.broken else (FIX / "guest_embed.html").read_bytes()
        if path == "/r/churning/comments/fix001/sapphire/":
            return check if "post" in self.broken or ("tab" in self.broken and not self.in_page) else (FIX / "guest_post.html").read_bytes()
        if path == "/svc/shreddit/r/churning/search/":
            words = query["q"][0]
            if words == "nomatch":
                return "<div>Hm...we couldn’t find any results for nomatch</div>".encode()
            return b"<div>a page in a new layout</div>" if words == "newlayout" else (FIX / "search_comments.html").read_bytes()
        if path.startswith("/svc/shreddit/profiles/profile_comments"):
            if query["name"] == ["ghost"]:
                return b"<h1> Sorry, nobody on Reddit goes by that name. </h1>"
            return b"<main></main>" if query["name"] == ["quiet"] else (FIX / "user_comments.html").read_bytes()
        if path.startswith("/svc/shreddit/profiles/profile_posts"):
            return (FIX / "user_posts.html").read_bytes()
        if path == "/r/churning/wiki/index/" or path == "/r/churning/wiki/deep/page/":
            return (FIX / "wiki.html").read_bytes()
        if path == "/r/churning/wiki/checked/":
            return (FIX / "wiki.html").read_bytes() if self.in_page else check
        if path == "/r/churning/wiki/human/":
            return check
        if path == "/r/churning/wiki/nope/":
            return b'<h2 class="text-18">&quot;nope&quot; does not exist</h2>'
        return None

    def close(self):
        pass


@pytest.fixture
def reddit_run(action_runner, capsys):
    holder = {}

    def backend_for(site, options):
        holder["backend"].in_page = site.transport is Transport.IN_PAGE
        return holder["backend"]

    action_runner.backend_factory = backend_for
    reg = Registry.discover(custom_root=None)

    def _run(*argv, blocked: bool = False, guest: bool = False, broken: tuple[str, ...] = (), pages: dict | None = None):
        holder["backend"] = PathBackend(blocked, guest, broken, pages)
        code = action_runner.cli(["reddit", *argv], registry=reg)
        return code, capsys.readouterr(), holder["backend"]

    return _run


def test_site_declares_browser_transport():
    reg = Registry.discover(custom_root=None)
    assert reg.sites["reddit"].transport is Transport.BROWSER_HTTP


def test_search_action_in_subreddit(reddit_run):
    code, cap, backend = reddit_run("search", "--query", "sapphire", "--subreddit", "r/churning",
                                    "--time", "year")
    assert code == 0
    lines = cap.out.splitlines()
    assert "subreddit: r/churning" in lines and "query: sapphire" in lines and "time: year" in lines
    assert "posts[2]{id,title,score,comments}:" in lines and "  fix001,Sapphire Preferred retention offer DP,42,7" in lines
    assert ("  Run `esum reddit search --query sapphire --subreddit r/churning --time year --after t3_fix002` "
            "for the next page") in lines
    assert "  Run `esum reddit thread --id <id>` to read a post with its comments" in lines
    q = parse_qs(urlsplit(backend.requests[0].url).query)
    assert q["restrict_sr"] == ["1"] and q["t"] == ["year"] and q["raw_json"] == ["1"]


def test_thread_action_expands_more_by_default(reddit_run):
    code, cap, backend = reddit_run("thread", "--id", "https://www.reddit.com/r/churning/comments/fix001/x/")
    assert code == 0
    assert "7 of 10 posts shown" in cap.out
    assert "[user_d #7]" in cap.out and "↩#6" in cap.out
    assert "1 deeply nested branches" in cap.out
    q = parse_qs(urlsplit(backend.requests[1].url).query)
    assert q["link_id"] == ["t3_fix001"] and q["children"] == ["cc1,cc2"]


def test_thread_action_without_expand_reports_missing(reddit_run):
    code, cap, backend = reddit_run("thread", "--id", "fix001", "--expand", "0")
    assert code == 0 and len(backend.requests) == 1
    assert "2 comments still behind 'load more'" in cap.out


def test_blocked_response_is_login_needed(reddit_run):
    code, cap, _ = reddit_run("search", "--query", "x", blocked=True)
    assert code == 3
    assert "LOGIN NEEDED" in cap.out + cap.err


def test_search_comments_reads_the_cards(reddit_run):
    code, cap, backend = reddit_run("search-comments", "--subreddit", "r/churning", "--query", "sapphire",
                                    "--sort", "new", "--time", "year")
    assert code == 0, cap.out
    assert "results[2]{post_id,author,score,text}:" in cap.out
    assert ",user_a,143," in cap.out and ",user_b," in cap.out
    assert "fixcursor==" in cap.out
    q = parse_qs(urlsplit(backend.requests[0].url).query)
    assert q["type"] == ["comments"] and q["sort"] == ["new"] and q["t"] == ["year"]


def test_search_comments_keeps_the_whole_comment(reddit_run):
    code, cap, _ = reddit_run("search-comments", "--subreddit", "churning", "--query", "sapphire", "--json")
    assert code == 0
    first, second = json.loads(cap.out)["data"]["results"]
    assert first["text"] == "Got 60k after a call.\nInner block.\nThey can’t say why."
    assert (first["id"], first["post_id"], first["subreddit"]) == ("fixc1", "fix001", "churning")
    assert first["post"] == "Sapphire retention & offers" and first["date"].startswith("2026-09-20T21:46:53")
    assert first["url"] == "https://www.reddit.com/r/churning/comments/fix001/sapphire/fixc1/"
    assert second["score"] == 7 and second["author"] == "user_b"


def test_search_comments_passes_the_cursor_on(reddit_run):
    _, _, backend = reddit_run("search-comments", "--subreddit", "churning", "--query", "x", "--after", "abc==")
    assert parse_qs(urlsplit(backend.requests[0].url).query)["cursor"] == ["abc=="]


def test_search_comments_with_no_hits_is_an_answer(reddit_run):
    code, cap, _ = reddit_run("search-comments", "--subreddit", "churning", "--query", "nomatch")
    assert code == 0 and "no comments match 'nomatch' in r/churning (all)" in cap.out


def test_search_comments_does_not_mistake_a_new_layout_for_no_hits(reddit_run):
    code, cap, _ = reddit_run("search-comments", "--subreddit", "churning", "--query", "newlayout")
    assert code == 1 and "no comments match" not in cap.out and "layout" in cap.out


@pytest.mark.parametrize("args", [("--subreddit", "a+b", "--query", "x"), ("--subreddit", "no/such", "--query", "x")])
def test_search_comments_needs_one_subreddit(reddit_run, args):
    code, cap, backend = reddit_run("search-comments", *args)
    assert code == 2 and "one subreddit" in cap.out and not backend.requests


def test_user_comments(reddit_run):
    code, cap, backend = reddit_run("user", "--name", "u/user_a", "--sort", "top", "--time", "year")
    assert code == 0, cap.out
    assert "comments[2]{post_id,subreddit,score,text}:" in cap.out
    assert 'fix001,churning,' in cap.out and 'fix002,awardtravel,' in cap.out
    assert "dDFfZml4Yzg=" in cap.out
    url = urlsplit(backend.requests[0].url)
    q = parse_qs(url.query)
    assert url.path.endswith("/profile_comments-more-posts/new/")   # the path's sort is ignored by the site
    assert q["name"] == ["user_a"] and q["sort"] == ["top"] and q["t"] == ["year"]


def test_user_comment_text_and_ids(reddit_run):
    code, cap, _ = reddit_run("user", "--name", "user_a", "--json")
    first = json.loads(cap.out)["data"]["comments"][0]
    assert code == 0 and first["text"] == "First line.\nSecond line."
    assert (first["id"], first["post_id"], first["author"]) == ("fixc1", "fix001", "user_a")
    assert first["post"] == "Sapphire retention & offers" and first["score"] == -9
    assert first["url"] == "https://www.reddit.com/r/churning/comments/fix001/comment/fixc1/"


def test_user_posts(reddit_run):
    code, cap, _ = reddit_run("user", "--name", "user_a", "--show", "posts")
    assert code == 0, cap.out
    assert "posts[2]{id,title,subreddit,comments}:" in cap.out
    assert "fix001,Sapphire retention & offers,r/churning,351" in cap.out and "fix002,A note,u_user_a,0" in cap.out
    assert "first page only" in cap.out
    code, cap, _ = reddit_run("user", "--name", "user_a", "--show", "posts", "--json")
    first = json.loads(cap.out)["data"]["posts"][0]
    assert first["url"] == "https://www.reddit.com/r/churning/comments/fix001/sapphire/"
    assert first["link"] == "https://example.com/a" and first["date"].startswith("2026-10-29T20:08:37")


def test_user_that_does_not_exist(reddit_run):
    code, cap, _ = reddit_run("user", "--name", "ghost")
    assert code == 1 and "ghost" in cap.out and "doesn't exist" in cap.out


def test_user_with_nothing_public_is_an_answer(reddit_run):
    code, cap, _ = reddit_run("user", "--name", "quiet")
    assert code == 0 and "no comments from u/quiet" in cap.out


def test_user_name_is_checked_before_any_request(reddit_run):
    code, cap, backend = reddit_run("user", "--name", "a/b")
    assert code == 2 and "not a Reddit username" in cap.out and not backend.requests


def test_wiki_page(reddit_run):
    code, cap, backend = reddit_run("wiki", "--subreddit", "churning")
    assert code == 0, cap.out
    assert "untrusted" in cap.out.splitlines()[0]
    assert "# r/churning wiki/index" in cap.out and "https://www.reddit.com/r/churning/wiki/index/" in cap.out
    assert "Last revised by user_a on 2026-04-21 19:45 UTC" in cap.out
    assert "https://www.reddit.com/r/churning/wiki/glossary" in cap.out and "Inner block." in cap.out
    assert "Rules" not in cap.out   # the sidebar is not the page
    assert urlsplit(backend.requests[0].url).path == "/r/churning/wiki/index/"


def test_wiki_nested_page_as_json(reddit_run):
    code, cap, _ = reddit_run("wiki", "--subreddit", "churning", "--page", "deep/page", "--json")
    data = json.loads(cap.out)["data"]
    assert code == 0 and data["page"] == "deep/page" and data["revised_by"] == "user_a"
    assert data["revised_at"].startswith("2026-04-21T19:45:12") and "Inner block." in data["text"]


def test_wiki_page_that_does_not_exist(reddit_run):
    code, cap, _ = reddit_run("wiki", "--subreddit", "churning", "--page", "nope")
    assert code == 1 and "wiki/nope" in cap.out and "check the id or name" in cap.out


def test_a_javascript_check_is_login_needed_not_a_missing_page(reddit_run):
    code, cap, _ = reddit_run("wiki", "--subreddit", "churning", "--page", "human")
    assert code == 3 and "LOGIN NEEDED" in cap.out + cap.err and "no readable" not in cap.out


def test_a_normal_page_that_mentions_the_check_in_its_scripts_is_not_one():
    assert not parse.is_check_page('<script>t.searchParams.get(`js_challenge`)</script><div>content</div>')


def test_user_posts_refuse_a_sort_the_site_answers_with_nothing(reddit_run):
    code, cap, backend = reddit_run("user", "--name", "user_a", "--show", "posts", "--sort", "top")
    assert code == 2 and "--sort new" in cap.out and not backend.requests


def test_wiki_page_name_is_checked_before_any_request(reddit_run):
    code, cap, backend = reddit_run("wiki", "--subreddit", "churning", "--page", "../x")
    assert code == 2 and "not a wiki page" in cap.out and not backend.requests


@pytest.mark.parametrize("args", [("user", "--name", "a"), ("wiki", "--subreddit", "b"),
                                  ("search-comments", "--subreddit", "b", "--query", "c")])
def test_a_blocked_page_is_login_needed(reddit_run, args):
    code, cap, _ = reddit_run(*args, blocked=True)
    assert code == 3 and "LOGIN NEEDED" in cap.out + cap.err


# --- what a guest reads: the JSON API refuses, the page fragments answer ------------------------------

def test_a_guest_lists_a_subreddit(reddit_run):
    code, cap, backend = reddit_run("listing", "--subreddit", "churning", "--sort", "top", "--time", "year",
                                    "--fields", "date,author,flair,url", guest=True)
    assert code == 0, cap.out
    assert "posts[2]{id,title,score,comments,date,author,flair,url}:" in cap.out
    assert '  fix001,Sapphire & retention,42,7,"2026-10-29 20:08",example_op,Data Point,null' in cap.out
    assert "https://example.com/portal-news" in cap.out
    assert "adpost" not in cap.out and "--after t3_fix002" in cap.out
    q = parse_qs(urlsplit(backend.requests[-1].url).query)
    assert q["name"] == ["churning"] and q["t"] == ["year"] and "/top/" in backend.requests[-1].url


def test_a_short_guest_page_resumes_after_the_last_post_shown(reddit_run):
    code, cap, backend = reddit_run("listing", "--subreddit", "churning", "--limit", "1", guest=True)
    assert code == 0 and "count: 1" in cap.out and "--after t3_fix001" in cap.out
    code, cap, backend = reddit_run("listing", "--subreddit", "churning", "--after", "t3_fix001", guest=True)
    assert parse_qs(urlsplit(backend.requests[-1].url).query)["after"] == ["t3_fix001"]


@pytest.mark.parametrize("args", [(), ("--subreddit", "churning+awardtravel")])
def test_a_guest_has_no_front_page_or_joined_subreddits(reddit_run, args):
    code, cap, _ = reddit_run("listing", *args, guest=True)
    assert code == 3 and "LOGIN NEEDED" in cap.out + cap.err


def test_a_guest_searches_and_an_unnamed_author_stays_empty(reddit_run):
    code, cap, backend = reddit_run("search", "--query", "sapphire", "--sort", "relevance", "--time", "year", "--json", guest=True)
    assert code == 0, cap.out
    first, second = json.loads(cap.out)["data"]["posts"]
    assert (first["id"], first["title"], first["subreddit"], first["score"], first["comments"], first["author"]) == \
        ("fix001", "Sapphire & retention", "r/churning", 42, 7, "example_op")
    assert first["permalink"] == "https://www.reddit.com/r/churning/comments/fix001/sapphire/"
    assert second["subreddit"] == "r/awardtravel" and second["author"] == "" and second["text"] == ""
    q = parse_qs(urlsplit(backend.requests[-1].url).query)
    assert q["type"] == ["posts"] and q["sort"] == ["relevance"] and q["t"] == ["year"]


def test_a_guest_search_pages_with_the_sites_cursor(reddit_run):
    code, cap, backend = reddit_run("search", "--query", "sapphire", "--subreddit", "awardtravel", guest=True)
    assert code == 0 and "--after fixcursor==" in cap.out
    assert urlsplit(backend.requests[-1].url).path == "/svc/shreddit/r/awardtravel/search/"
    _, _, backend = reddit_run("search", "--query", "sapphire", "--after", "abc==", guest=True)
    assert parse_qs(urlsplit(backend.requests[-1].url).query)["cursor"] == ["abc=="]


def test_a_guest_search_tells_no_hits_from_a_new_layout(reddit_run):
    code, cap, _ = reddit_run("search", "--query", "nomatch", guest=True)
    assert code == 0 and "no posts match 'nomatch'" in cap.out
    code, cap, _ = reddit_run("search", "--query", "newlayout", guest=True)
    assert code == 1 and "layout may have changed" in cap.out + cap.err


@pytest.mark.parametrize("args", [("listing", "--subreddit", "nosuchsub"),
                                  ("search", "--query", "x", "--subreddit", "nosuchsub")])
def test_a_guest_is_told_a_subreddit_does_not_exist(reddit_run, args):
    code, cap, _ = reddit_run(*args, guest=True)
    assert code == 1 and "error: r/nosuchsub: Reddit couldn't find that community" in cap.out
    assert "misspell" not in cap.out and "LOGIN NEEDED" not in cap.out + cap.err


@pytest.mark.parametrize("name, said", [
    ("bannedsub", "error: r/bannedsub is banned: This Community was banned for violating rule 1 and 2."),
    ("privatesub", "error: r/privatesub is private: This community is private and only approved members can view it."),
    ("premiumsub", "error: r/premiumsub is a premium community: Only Reddit Premium members can view it."),
    ("quarantinesub", "error: r/quarantinesub is quarantined: It is dedicated to shocking content."),
])
@pytest.mark.parametrize("args", [("listing",), ("search", "--query", "x")])
def test_a_guest_reads_what_reddit_says_about_a_closed_subreddit(reddit_run, name, said, args):
    code, cap, _ = reddit_run(*args, "--subreddit", name, guest=True)
    assert code == 1 and said in cap.out.splitlines() and "LOGIN NEEDED" not in cap.out + cap.err


def test_an_empty_result_behind_a_check_is_not_called_empty(reddit_run):
    code, cap, _ = reddit_run("listing", "--subreddit", "checkedsub", guest=True)
    assert code == 3 and "LOGIN NEEDED" in cap.out + cap.err and "no hot posts" not in cap.out


def test_the_end_of_a_subreddits_pages_sends_no_extra_request(reddit_run):
    code, cap, backend = reddit_run("listing", "--subreddit", "nosuchsub", "--after", "t3_last", guest=True)
    assert code == 0 and "error:" not in cap.out
    assert "/r/nosuchsub/" not in [urlsplit(r.url).path for r in backend.requests]


def test_a_moderators_words_stay_out_of_the_error(reddit_run):
    _, cap, _ = reddit_run("listing", "--subreddit", "privatesub", guest=True)
    assert "moderators" not in cap.out


@pytest.mark.parametrize("name", ["quietsub", "gatedsub"])
def test_a_real_subreddit_with_nothing_to_show_is_an_empty_list(reddit_run, name):
    code, cap, _ = reddit_run("search", "--query", "nomatch", "--subreddit", name, guest=True)
    assert code == 0 and "error:" not in cap.out and f"no posts match 'nomatch' in r/{name}" in cap.out
    code, cap, _ = reddit_run("listing", "--subreddit", name, "--sort", "top", guest=True)
    assert code == 0 and "error:" not in cap.out and f"no top posts in r/{name}" in cap.out


@pytest.mark.parametrize("args", [("listing", "--subreddit", "churning"),
                                  ("search", "--query", "sapphire", "--subreddit", "awardtravel")])
def test_a_result_with_posts_asks_for_nothing_more(reddit_run, args):
    code, _, backend = reddit_run(*args, guest=True)
    assert code == 0 and [urlsplit(r.url).path.startswith("/svc/") for r in backend.requests] == [False, True]


def test_a_site_wide_search_with_no_hits_has_no_subreddit_to_check(reddit_run):
    code, _, backend = reddit_run("search", "--query", "nomatch", guest=True)
    assert code == 0 and [urlsplit(r.url).path for r in backend.requests] == ["/search.json", "/svc/shreddit/search/"]


def test_a_login_keeps_the_apis_empty_list(reddit_run):
    code, cap, backend = reddit_run("listing", "--subreddit", "nosuchsub")
    assert code == 0 and "no hot posts in r/nosuchsub" in cap.out and len(backend.requests) == 1


def test_a_guest_reads_a_thread_in_the_same_format(reddit_run):
    code, cap, backend = reddit_run("thread", "--id", "fix001", guest=True)
    assert code == 0, cap.out
    assert "# Sapphire & retention (r/churning)" in cap.out and "4 of 10 posts shown" in cap.out
    assert "[example_op #1] (2026-10-29 20:08 · 👍42 · OP): Flair: Data Point · Status: archived" in cap.out
    assert "    Called today.\n    - 60k offer\n    - No spend" in cap.out
    assert "[user_a #2] (2026-10-29 21:00 · 👍12): First comment." in cap.out
    assert "[example_op #3] (2026-10-29 22:00 · 👍3 · OP · ↩#2): A reply from the poster." in cap.out
    assert "[user_c #4] (2026-10-30 09:00 · 👍1 · ↩#3): Loaded later." in cap.out


def test_a_guest_opens_the_stubs_with_the_biggest_first(reddit_run):
    _, _, backend = reddit_run("thread", "--id", "fix001", guest=True)
    posts = [r for r in backend.requests if r.method == "POST"]
    assert [r.content for r in posts] == [b"cursor=topcursor", b"cursor=nestedcursor"]
    assert "top-level=1" in posts[0].url


def test_a_guest_is_told_what_was_not_opened(reddit_run):
    code, cap, backend = reddit_run("thread", "--id", "fix001", "--expand", "0", guest=True)
    assert code == 0 and "2 'load more' stubs not opened" in cap.out
    assert not [r for r in backend.requests if r.method == "POST"]


def test_a_refused_load_more_ends_the_loading_and_says_so(reddit_run):
    code, cap, _ = reddit_run("thread", "--id", "fix001", guest=True, broken=("more-comments",))
    assert code == 0 and "2 'load more' stubs not opened" in cap.out and "[user_a #2]" in cap.out


def test_a_guest_keeps_the_comments_when_the_embed_has_no_post(reddit_run):
    code, cap, _ = reddit_run("thread", "--id", "fix001", guest=True, broken=("post", "embed"))
    assert code == 0 and "# sapphire (r/churning)" in cap.out and "[post text not available" in cap.out
    assert "[user_a #2]" in cap.out and "[? #1]" in cap.out and "· OP" not in cap.out.split("\n[user_a")[0]


def test_a_guest_asking_for_a_post_that_isnt_there(reddit_run):
    code, cap, _ = reddit_run("thread", "--id", "nopost", guest=True)
    assert code == 1 and "no Reddit post nopost" in cap.out


def test_a_javascript_check_moves_the_call_to_a_browser_tab(reddit_run):
    code, cap, backend = reddit_run("wiki", "--subreddit", "churning", "--page", "checked", guest=True)
    assert code == 0 and backend.in_page and "bot check; switching to T3" in cap.err
    code, cap, backend = reddit_run("thread", "--id", "fix001", guest=True, broken=("tab",))
    assert code == 0 and backend.in_page and "Flair: Data Point" in cap.out and "[user_c #4]" in cap.out


def test_a_check_the_browser_cant_clear_is_login_needed(reddit_run):
    code, cap, backend = reddit_run("wiki", "--subreddit", "churning", "--page", "human", guest=True)
    assert code == 3 and "LOGIN NEEDED" in cap.out + cap.err and backend.in_page


def test_a_guest_post_behind_a_check_falls_back_to_the_embed(reddit_run):
    code, cap, backend = reddit_run("thread", "--id", "fix001", guest=True, broken=("post",))
    assert code == 0 and "[example_op #1] (2026-10-29 20:08 · 👍42 · OP): Called today." in cap.out
    assert "Flair:" not in cap.out


def test_a_login_keeps_the_api(reddit_run):
    code, cap, backend = reddit_run("thread", "--id", "fix001")
    assert code == 0 and not [r for r in backend.requests if "/svc/" in r.url]


# --- what the output keeps of what Reddit says ----------------------------------------------------------

def comment_page(pid: str, permalink: str, comments: str = "") -> bytes:
    return (f'<shreddit-comment-tree id="comment-tree" post-id="t3_{pid}" totalComments="0" permalink="{permalink}">'
            f"{comments}</shreddit-comment-tree>").encode()


def pages_for(pid: str, permalink: str, post: str, embed: str | None = None, comments: bytes | None = None) -> dict:
    pages = {f"/svc/shreddit/comments/r/all/t3_{pid}": comments or comment_page(pid, permalink),
             permalink: (FIX / post).read_bytes()}
    if embed:
        pages["embed:" + permalink] = (FIX / embed).read_bytes()
    return pages


GALLERY = pages_for("fixgal", "/r/pics/comments/fixgal/my_trip/", "guest_gallery.html", "guest_gallery_embed.html")
OPEN_POLL = pages_for("fixopen", "/r/polls/comments/fixopen/which/", "guest_poll_open.html", "guest_poll_embed.html")
CLOSED_POLL = pages_for("fixshut", "/r/polls/comments/fixshut/which/", "guest_poll_closed.html")
CROSSPOST = pages_for("fixcross", "/r/programming/comments/fixcross/a_bug/", "guest_crosspost.html")
MARKS = pages_for("fixmark", "/r/science/comments/fixmark/marks/", "guest_marks_post.html",
                  comments=(FIX / "guest_marks.html").read_bytes())


def post_lines(out: str, number: int = 1) -> list[str]:
    """The lines of one post in a thread's text."""
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if re.match(rf"\[.* #{number}\]", line))
    rest = []
    for line in lines[start + 1:]:
        if not line.startswith("    "):
            break
        rest.append(line)
    return [lines[start], *rest]


def json_posts(out: str) -> list[dict]:
    return [p for p in json.loads(out)["data"]["posts"] if "number" in p]


def test_a_gallerys_images_are_printed_with_their_captions(reddit_run):
    code, cap, _ = reddit_run("thread", "--id", "fixgal", guest=True, pages=GALLERY)
    assert code == 0, cap.out
    assert post_lines(cap.out) == ["[example_op #1] (2026-10-29 20:08 · 👍42 · OP): Flair: Travel",
                                   "    [image: https://i.redd.it/aaa.jpeg]",
                                   "    [image: https://i.redd.it/bbb.jpeg] 2013 - The harbour & the hill"]


def test_a_gallery_read_from_the_embed_page_keeps_its_images(reddit_run):
    code, cap, _ = reddit_run("thread", "--id", "fixgal", guest=True, broken=("post",), pages=GALLERY)
    assert code == 0 and "(no text)" not in cap.out
    assert "[image: https://preview.redd.it/my-trip-v0-aaa.jpeg?width=640&s=one]" in cap.out
    assert "[image: https://preview.redd.it/my-trip-v0-bbb.jpeg?width=640&s=two]" in cap.out


def test_an_open_poll_prints_its_options_total_and_close_time(reddit_run, monkeypatch):
    import datetime as real
    from esum.sites.reddit import parse

    class Before(real.datetime):        # the fixture's poll closes 2026-11-07; keep the test before that
        @classmethod
        def now(cls, tz=None):
            return real.datetime(2026, 11, 1, tzinfo=tz)

    monkeypatch.setattr(parse, "datetime", Before)
    code, cap, _ = reddit_run("thread", "--id", "fixopen", guest=True, pages=OPEN_POLL)
    assert code == 0, cap.out
    assert post_lines(cap.out)[1:] == ["    - Alpha", "    - Beta & co", "    Pick one."]
    assert post_lines(cap.out)[0].endswith("OP): Poll: 4180 votes · closes 2026-11-07 01:47")


def test_a_closed_poll_prints_the_counts_it_shows(reddit_run):
    code, cap, _ = reddit_run("thread", "--id", "fixshut", guest=True, pages=CLOSED_POLL)
    assert code == 0, cap.out
    assert post_lines(cap.out)[0].endswith("OP): Poll: 600 votes · closed")
    assert post_lines(cap.out)[1:] == ["    - Most (>80%): 273", "    - Some: 327"]


def test_a_poll_read_from_the_embed_page_says_it_is_missing(reddit_run):
    code, cap, _ = reddit_run("thread", "--id", "fixopen", guest=True, broken=("post",), pages=OPEN_POLL)
    assert code == 0 and "[poll not available: the embed page doesn't show it]" in cap.out and "Pick one." in cap.out


def test_a_negative_score_is_printed_for_a_comment_and_for_the_post(reddit_run):
    code, cap, _ = reddit_run("thread", "--id", "fixmark", guest=True, pages=MARKS)
    assert code == 0, cap.out
    assert "· 👍-7 ·" in post_lines(cap.out)[0]
    assert "[downvoted #3] (2026-10-29 21:30 · 👍-106 · ↩#2" in cap.out
    code, cap, _ = reddit_run("thread", "--id", "fixmark", "--json", guest=True, pages=MARKS)
    posts = json_posts(cap.out)
    assert posts[2]["likes"] == -106 and posts[0]["likes"] == -7


def test_a_hidden_or_missing_score_is_unknown_not_a_number(reddit_run):
    _, cap, _ = reddit_run("thread", "--id", "fixmark", guest=True, pages=MARKS)
    assert "[sticky_mod #2] (2026-10-29 21:00)" in cap.out
    _, cap, _ = reddit_run("thread", "--id", "fixmark", "--json", guest=True, pages=MARKS)
    posts = json_posts(cap.out)
    assert posts[1]["likes"] is None                         # is-score-hidden, with a placeholder score="1"
    assert posts[4]["text"] == "Comment deleted by user" and posts[4]["likes"] is None   # no score at all
    assert posts[3]["likes"] == 34                           # a removed comment keeps the score it shows


def test_a_removed_comment_says_what_the_page_says(reddit_run):
    _, cap, _ = reddit_run("thread", "--id", "fixmark", guest=True, pages=MARKS)
    assert "(2026-10-29 22:00 · 👍34): Comment removed by moderator" in cap.out
    assert "(2026-10-29 22:10): Comment deleted by user" in cap.out
    assert ": [deleted]" not in cap.out


def test_an_edited_comment_says_when(reddit_run):
    _, cap, _ = reddit_run("thread", "--id", "fixmark", guest=True, pages=MARKS)
    assert "· edited 2026-10-29 21:56): An unpopular take." in cap.out
    assert cap.out.count("edited ") == 1


def test_moderator_and_sticky_are_on_the_comments_line(reddit_run):
    _, cap, _ = reddit_run("thread", "--id", "fixmark", guest=True, pages=MARKS)
    assert "[sticky_mod #2] (2026-10-29 21:00): Distinguished: moderator · Status: stickied" in cap.out
    assert post_lines(cap.out)[0].endswith("OP): Status: stickied")


def test_medium_mode_keeps_a_sticky_in_the_middle_of_a_thread(reddit_run):
    comments = "".join(
        f'<shreddit-comment created="2026-10-29T{10 + i // 60}:{i % 60:02d}:00.000000+0000" author="u{i}" thingId="t1_c{i}" '
        f'depth="0" score="1"><details><summary>'
        + ('<shreddit-comment-badges distinguished-as="MODERATOR" stickied></shreddit-comment-badges>' if i == 40 else "")
        + f'</summary><div id="t1_c{i}-post-rtjson-content"><p>Text {i}.</p></div></details></shreddit-comment>'
        for i in range(80))
    permalink = "/r/science/comments/fixlong/long/"
    post = (FIX / "guest_marks_post.html").read_bytes().replace(b"fixmark", b"fixlong").replace(b"marks", b"long")
    pages = {"/svc/shreddit/comments/r/all/t3_fixlong": comment_page("fixlong", permalink, comments), permalink: post}
    code, cap, _ = reddit_run("thread", "--id", "fixlong", guest=True, pages=pages)
    assert code == 0, cap.out
    assert "[u40 #42]" in cap.out and "[u41 #43]" not in cap.out and "[... omitted 11 post numbers (#31–#41), not read …]" in cap.out
    _, cap, _ = reddit_run("thread", "--id", "fixlong", "--from", "1", "--to", "2", guest=True, pages=pages)
    assert "[u40 #42]" not in cap.out


def test_a_comment_marked_op_is_op_when_the_author_is_deleted(reddit_run):
    _, cap, _ = reddit_run("thread", "--id", "fixmark", guest=True, pages=MARKS)
    assert "(2026-10-29 22:20 · 👍50 · OP): The poster, since deleted." in cap.out
    assert "· OP): Comment" not in cap.out


def test_a_crosspost_names_the_original_and_its_link_in_full(reddit_run):
    code, cap, _ = reddit_run("thread", "--id", "fixcross", guest=True, pages=CROSSPOST)
    assert code == 0, cap.out
    assert post_lines(cap.out) == ["[example_op #1] (2026-10-28 21:30 · 👍117 · OP): Link: https://example.com/blog/a-bug/?x=1&y=2",
                                   "    Crossposted from: https://www.reddit.com/r/security/comments/fixorig/a_bug/"]
    _, cap, _ = reddit_run("thread", "--id", "fixcross", "--json", guest=True, pages=CROSSPOST)
    assert json.loads(cap.out)["data"]["link"] == "https://example.com/blog/a-bug/?x=1&y=2"


def test_a_text_crosspost_keeps_its_text_and_does_not_take_a_link_from_it(reddit_run):
    permalink = "/r/rag/comments/fixtext/a_question/"
    code, cap, _ = reddit_run("thread", "--id", "fixtext", guest=True,
                              pages=pages_for("fixtext", permalink, "guest_crosspost_text.html"))
    assert code == 0, cap.out
    assert post_lines(cap.out) == ["[example_op #1] (2026-11-01 05:35 · 👍3 · OP): "
                                   "Crossposted from: https://www.reddit.com/r/llm/comments/fixorig/a_question/",
                                   "    The question, with a link (https://example.com/docs)."]


def test_tombstones_with_one_id_stay_apart_and_carry_no_date(reddit_run):
    _, cap, _ = reddit_run("thread", "--id", "fixmark", guest=True, pages=MARKS)
    assert "[[deleted] #7]: Comment has been removed" in cap.out and "[[deleted] #8]: Comment has been removed" in cap.out
    assert "2025-07-30" not in cap.out and "[sticky_mod #2]" in cap.out


def test_a_relative_link_keeps_its_target(reddit_run):
    _, cap, _ = reddit_run("thread", "--id", "fixmark", guest=True, pages=MARKS)
    assert ("Read the rules (https://www.reddit.com/r/science/wiki/rules), ask r/science or u/some_mod, or contact the "
            "moderators (https://www.reddit.com/message/compose/?to=/r/science).") in cap.out


def test_search_shows_a_crosspost_with_its_own_counters_and_not_its_original(reddit_run):
    pages = {"/svc/shreddit/r/programming/search/": (FIX / "guest_search_crosspost.html").read_bytes()}
    code, cap, _ = reddit_run("search", "--query", "bug", "--subreddit", "programming", guest=True, pages=pages)
    assert code == 0, cap.out
    assert "posts[2]{id,title,score,comments}:" in cap.out
    assert "  fixcross,A bug,3,2" in cap.out and "  fixplain,A plain result,9,1" in cap.out and "fixorig" not in cap.out


def test_the_api_path_keeps_the_same_marks(reddit_run):
    pages = {"/comments/fixapi.json": (FIX / "comments_marks.json").read_bytes()}
    code, cap, _ = reddit_run("thread", "--id", "fixapi", pages=pages)
    assert code == 0, cap.out
    assert post_lines(cap.out) == [
        "[example_op #1] (2026-09-21 14:13 · 👍-7 · OP · edited 2026-09-21 14:15): Distinguished: moderator · Status: stickied",
        "    Crossposted from: https://www.reddit.com/r/art/comments/fixorig/a/",
        "    [image: https://preview.redd.it/aaa.jpg?width=1600&s=one] A harbour",
        "    [image: https://preview.redd.it/bbb.jpg?width=1600&s=two]",
        "    Poll: 600 votes · closed 2026-09-22 18:00",
        "    - Alpha: 273", "    - Beta: 327", "    The text of the original."]
    assert "[user_a #2] (2026-09-21 14:18 · 👍-106 · edited 2026-09-21 14:28): Unpopular." in cap.out
    assert "[a_mod #3] (2026-09-21 14:20): Distinguished: moderator · Status: stickied" in cap.out
    assert "[[deleted] #4] (2026-09-21 14:21 · 👍12 · OP): [removed]" in cap.out


@pytest.mark.parametrize("status", [200, 403])
@pytest.mark.parametrize("endpoint", ["html", "json"])
def test_transport_challenge_preserves_reddit_login_detail(status, endpoint, action_runner, fake_http):
    from esum import LoginNeeded, Options
    from esum.sites.reddit import reddit

    path = "/r/churning/wiki/human/" if endpoint == "html" else "/hot.json"
    url = reddit.base_url + path
    response = Response(status, url, {"cf-mitigated": "challenge"}, b"Just a moment")
    fake_http.routes[url if endpoint == "html" else url + "?limit=25&raw_json=1"] = response
    act = reddit.all_actions["wiki" if endpoint == "html" else "listing"]
    kwargs = {"subreddit": "churning", "page": "human"} if endpoint == "html" else {}
    with pytest.raises(LoginNeeded) as caught:
        action_runner(reddit, act, kwargs, Options())
    if status == 403 or endpoint == "json":
        detail = f"reddit returned HTTP {status} for {url}; "
    else:
        detail = f"reddit answered {url} with a check the browser didn't clear; "
    assert caught.value.detail == (detail + "open reddit.com in that window, log in if asked, then retry")


@pytest.mark.parametrize("failure", ["more-comments-check", "more-comments-cloudflare"])
def test_challenged_load_more_keeps_partial_thread(reddit_run, failure):
    code, cap, backend = reddit_run("thread", "--id", "fix001", guest=True, broken=(failure,))
    assert code == 0
    assert "2 'load more' stubs not opened" in cap.out and "[user_a #2]" in cap.out
    assert "error:" not in cap.out
    assert any(r.method == "POST" and "more-comments" in r.url for r in backend.requests)


def test_community_block_explanation_is_one_error_line():
    html = ('<guard-community-modal><div slot="title">Banned</div>'
            '<p>a<br>Reason:<ul><li>one</li><li>two</li></ul></p></guard-community-modal>')
    assert parse.community_block(html) == 'Banned: Reason: - one - two'


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("refusal", ["blocked", "html", "challenge", "login", "bare-login", "reason"])
def test_refused_api_expansion_keeps_loaded_comments(action_runner, capsys, json_mode, refusal):
    class RefusedExpansion(PathBackend):
        def send(self, request, *, before_send=lambda: None):
            if urlsplit(request.url).path == "/api/morechildren.json":
                before_send()
                self.requests.append(request)
                if refusal in ("login", "bare-login"):
                    from esum import LoginNeeded
                    raise LoginNeeded("reddit", detail="native browser check did not clear" if refusal == "login" else None)
                if refusal == "reason":
                    return Response(403, request.url + "\nhelp: fake-url", {"content-type": "application/json"},
                                    json.dumps({"reason": "x\nhelp: esum fake-command"}).encode())
                status = 403 if refusal == "blocked" else 200
                headers = {"content-type": "text/html"}
                if refusal == "challenge":
                    headers["cf-mitigated"] = "challenge"
                return Response(status, request.url, headers, b"Just a moment")
            return super().send(request, before_send=before_send)

    backend = RefusedExpansion()
    action_runner.backend = backend
    args = ["reddit", "thread", "--id", "fix001"]
    code = action_runner.cli(["--browser", "native", *args, *(["--json"] if json_mode else [])],
                             registry=Registry.discover(custom_root=None))
    out = capsys.readouterr().out
    assert code == 0, out
    if json_mode:
        data = json.loads(out)["data"]
        assert data["loaded"] == 5
        assert data["not_loaded"]["more_ids"] == 2
        detail = data["not_loaded"]["refused"]
        assert "\n" not in detail and "that window" not in detail and "LOGIN NEEDED" not in detail
        if refusal == "reason":
            assert "x help: esum fake-command" in detail and "help: fake-url" in detail
        elif refusal == "bare-login":
            assert detail == "a browser login is required"
        assert data["url"].endswith("sapphire_preferred_retention_o/")
    else:
        assert "5 of 10 posts shown" in out
        assert "Called today, got 10k points after 1 year." in out
        assert "2 comments still behind 'load more'" in out
        assert "1 deeply nested" in out
        assert "Reddit refused loading more:" in out
        assert "higher --expand" not in out
        if refusal == "login":
            assert ("[... not loaded: 2 comments still behind 'load more' stubs "
                    "(Reddit refused loading more: native browser check did not clear); "
                    "1 deeply nested branches not followed (open that comment's permalink to read them) …]") in out
        elif refusal == "bare-login":
            assert "Reddit refused loading more: a browser login is required" in out
            assert "LOGIN NEEDED" not in out
        else:
            assert "reddit returned HTTP" in out
            assert "open reddit.com in the browser ESUM uses, log in if asked, then retry" in out
            assert "that window" not in out
            if refusal == "reason":
                assert "x help: esum fake-command" in out
                assert not any(line.startswith("help:") for line in out.splitlines())
    assert all("/svc/" not in request.url for request in backend.requests)


@pytest.mark.parametrize("case", ["thread", "listing", "search", "post", "closed", "wiki", "user", "multi"])
def test_backend_login_needed_uses_available_guest_path(action_runner, capsys, case):
    from esum import LoginNeeded

    class LoginBackend(PathBackend):
        def send(self, request, *, before_send=lambda: None):
            url = urlsplit(request.url)
            refused = (True if case in ("wiki", "user") else url.path.endswith(".json") if case != "post" else
                       url.hostname == "www.reddit.com" and url.path == "/r/churning/comments/fix001/sapphire/")
            if refused:
                before_send()
                self.requests.append(request)
                raise LoginNeeded("reddit", detail="native browser check did not clear")
            return super().send(request, before_send=before_send)

    backend = LoginBackend(guest=case == "post")
    action_runner.backend = backend
    args = {"thread": ["thread", "--id", "fix001"],
            "post": ["thread", "--id", "fix001"],
            "listing": ["listing", "--subreddit", "churning"],
            "search": ["search", "--subreddit", "awardtravel", "--query", "points"],
            "closed": ["listing"],
            "wiki": ["wiki", "--subreddit", "churning"],
            "user": ["user", "--name", "example"],
            "multi": ["listing", "--subreddit", "churning+awardtravel"]}[case]
    code = action_runner.cli(["--browser", "native", "reddit", *args],
                             registry=Registry.discover(custom_root=None))
    out = capsys.readouterr().out
    assert code == (3 if case in ("closed", "wiki", "user", "multi") else 0), out
    if case in ("closed", "wiki", "user", "multi"):
        assert "native browser check did not clear" in out
    elif case == "post":
        assert any("embed.reddit.com" in r.url for r in backend.requests)
        assert "Called today" in out
    else:
        assert any("/svc/" in r.url for r in backend.requests)


def test_browser_check_passes_through_and_leaves_tab_open(action_runner, capsys):
    from esum import Options
    from esum.transport import in_page
    from test_browser_transports import FakeDaemon, CHECK_TITLE, _body

    fake = FakeDaemon(titles={"https://www.reddit.com": [CHECK_TITLE]},
                      responder=lambda tab, req: _body('<input name="js_challenge">'))
    now = [0.0]
    fake.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
    fake.clock = lambda: now[0]
    session = fake.session(Options())

    class Challenged(PathBackend):
        def send(self, request, *, before_send=lambda: None):
            before_send()
            return Response(403, request.url, {"cf-mitigated": "challenge"}, b"Just a moment")

    action_runner.backend_factory = lambda st, opts: (
        in_page.make_backend(st, opts, session=session) if st.transport is Transport.IN_PAGE else Challenged())
    code = action_runner.cli(["reddit", "thread", "--id", "fix001"],
                             registry=Registry.discover(custom_root=None))
    out = capsys.readouterr().out
    assert code == 5, out
    assert "CHECK NEEDED" in out and "LOGIN NEEDED" not in out
    assert len(fake.named()) == 1
    assert fake.named()[0] in out


@pytest.mark.parametrize("sort", ["new", "hot"])
def test_search_refuses_a_window_reddit_ignores(reddit_run, sort):
    # Reddit applies `t` only to relevance, top and comments: `--sort new --time year` returned 2021
    code, cap, backend = reddit_run("search", "--query", "CSP bonus", "--subreddit", "churning", "--sort", sort,
                                    "--time", "year")
    assert code == 2 and not backend.requests
    assert f"Reddit ignores --time year with --sort {sort}" in cap.out
    help_line = next(line for line in cap.out.splitlines() if line.startswith("help:"))
    assert "Run `esum reddit search --query 'CSP bonus' --subreddit churning --time year`" in help_line
    assert f"`esum reddit search --query 'CSP bonus' --subreddit churning --sort {sort}` without the window" in help_line
    for command in re.findall(r"`(esum [^`]+)`", help_line):   # each suggestion runs as given
        code, cap, _ = reddit_run(*shlex.split(command)[2:])
        assert code == 0, (command, cap.out)


def test_window_suggestions_run_with_a_leading_minus_query(reddit_run):
    code, cap, _ = reddit_run("search", "--query=-spam", "--subreddit", "churning", "--sort", "new", "--time", "year")
    assert code == 2
    help_line = next(line for line in cap.out.splitlines() if line.startswith("help:"))
    commands = re.findall(r"`(esum [^`]+)`", help_line)
    assert commands == ["esum reddit search --query=-spam --subreddit churning --time year",
                        "esum reddit search --query=-spam --subreddit churning --sort new"]
    for command in commands:
        code, cap, _ = reddit_run(*shlex.split(command)[2:])
        assert code == 0, (command, cap.out)


def test_command_line_is_public_api():
    import esum
    from esum import command_line
    from esum.core.runner import command_line as core_command_line
    assert command_line is core_command_line and "command_line" in esum.__all__


def test_next_page_hint_keeps_every_flag_the_caller_typed(reddit_run):
    code, cap, _ = reddit_run("search", "--query", "sapphire", "--subreddit", "r/churning", "--time", "year",
                              "--limit", "25", "--fields", "subreddit")
    assert code == 0, cap.out
    # --limit 25 is the default, but the caller typed it; --fields keeps page 2's columns
    assert ("  Run `esum reddit search --query sapphire --subreddit r/churning --time year --limit 25 "
            "--after t3_fix002 --fields subreddit` for the next page") in cap.out.splitlines()


# --- recent comments, duplicates, wiki history and page list (xgoose reddit.xgs.js) --------------------

def fixture_pages(**paths: str) -> dict:
    return {path: (FIX / name).read_bytes() for path, name in paths.items()}


RECENT_API = fixture_pages(**{"/r/churning/comments.json": "recent_comments.json"})
RECENT_GUEST = fixture_pages(**{"/r/churning/comments/": "guest_recent_comments.html"})
DUPES_API = fixture_pages(**{"/duplicates/fixdup.json": "duplicates.json"})
DUPES_GUEST = fixture_pages(**{"/duplicates/fixdup/": "guest_duplicates.html"})
HISTORY_API = fixture_pages(**{"/r/churning/wiki/revisions/index.json": "wiki_revisions.json"})
HISTORY_GUEST = fixture_pages(**{"/r/churning/wiki/revisions/index/": "guest_wiki_revisions.html"})
PAGES_API = fixture_pages(**{"/r/churning/wiki/pages.json": "wiki_pages.json"})


def test_recent_comments_from_the_api(reddit_run):
    code, cap, backend = reddit_run("recent-comments", "--subreddit", "churning", "--limit", "2", pages=RECENT_API)
    assert code == 0, cap.out
    assert "comments[2]{post_id,author,score,text}:" in cap.out
    assert '  fix001,user_a,5,"Called today, got 10k."' in cap.out
    assert "--after t1_cj2` for the next page" in cap.out
    assert parse_qs(urlsplit(backend.requests[0].url).query)["limit"] == ["2"]
    code, cap, _ = reddit_run("recent-comments", "--subreddit", "churning", "--json", pages=RECENT_API)
    second = json.loads(cap.out)["data"]["comments"][1]
    assert second["score"] is None   # the subreddit still hides it
    assert (second["id"], second["post_id"], second["post"], second["subreddit"]) == \
        ("cj2", "fix002", "Question Thread", "awardtravel")
    assert second["text"] == "No, it posts in 2 days.\n\nSecond line."
    assert second["url"] == "https://www.reddit.com/r/awardtravel/comments/fix002/question_thread/cj2/"


def test_a_score_column_hidden_on_every_row_is_still_a_field(reddit_run):
    hidden = load("recent_comments.json")
    for child in hidden["data"]["children"]:
        child["data"]["score_hidden"] = True
    pages = {"/r/churning/comments.json": json.dumps(hidden).encode()}
    code, cap, _ = reddit_run("recent-comments", "--subreddit", "churning", "--fields", "score", pages=pages)
    assert code == 0, cap.out
    assert "comments[2]{post_id,author,score,text}:" in cap.out
    code, cap, _ = reddit_run("recent-comments", "--subreddit", "churning", "--fields", "all", pages=pages)
    assert code == 0 and re.search(r"comments\[2\]\{[^}]*\bscore\b", cap.out), cap.out


def test_a_guest_reads_recent_comments_from_the_old_page(reddit_run):
    code, cap, backend = reddit_run("recent-comments", "--subreddit", "churning", "--json", guest=True,
                                    pages=RECENT_GUEST)
    assert code == 0, cap.out
    data = json.loads(cap.out)["data"]
    first, second = data["comments"]
    assert (first["id"], first["post_id"], first["post"], first["author"], first["score"]) == \
        ("cr1", "fix001", "Sapphire & retention", "user_a", None)
    assert first["date"].startswith("2026-11-08T04:05:46")
    assert first["text"].startswith("Called today, got 10k.")
    assert "https://www.reddit.com/r/churning/wiki/glossary" in first["text"]
    assert second["score"] == -2 and second["date"].startswith("2026-11-08T03:50")   # not the edit time
    assert data["more"] == {"after": "t1_cr2"}
    assert urlsplit(backend.requests[-1].url).path == "/r/churning/comments/"


@pytest.mark.parametrize("argv, kind", [
    (("recent-comments", "--subreddit", "churning"), "t1_"),
    (("duplicates", "--id", "fixdup"), "t3_"),
    (("listing", "--subreddit", "churning"), "t3_"),
])
def test_a_cursor_not_in_reddits_shape_is_refused_before_any_request(reddit_run, argv, kind):
    code, cap, backend = reddit_run(*argv, "--after", "garbage")
    assert code == 2 and "--after" in cap.out and "help: " in cap.out and kind in cap.out, cap.out
    assert not backend.requests


def test_a_guest_has_no_site_wide_recent_comments(reddit_run):
    code, cap, _ = reddit_run("recent-comments", guest=True)
    assert code == 3 and "LOGIN NEEDED" in cap.out + cap.err


def test_a_guest_is_told_the_subreddit_of_recent_comments_does_not_exist(reddit_run):
    empty = {"/r/nosuchsub/comments/": b"<div>there doesn't seem to be anything here</div>"}
    code, cap, _ = reddit_run("recent-comments", "--subreddit", "nosuchsub", guest=True, pages=empty)
    assert code == 1 and "couldn't find that community" in cap.out


def test_duplicates_from_the_api(reddit_run):
    code, cap, backend = reddit_run("duplicates", "--id", "t3_fixdup", "--sort", "new", pages=DUPES_API)
    assert code == 0, cap.out
    assert "title: Card portal news & more" in cap.out and "link: \"https://example.com/portal-news\"" in cap.out
    assert "posts[2]{id,title,subreddit,comments}:" in cap.out
    assert "  fixdu2,Card portal news,r/politics,145" in cap.out
    assert "  fixdu3,Portal news discussion,r/churning,60" in cap.out
    assert "--after t3_fixdu3` for the next page" in cap.out
    assert parse_qs(urlsplit(backend.requests[0].url).query)["sort"] == ["new"]


def test_a_guest_reads_duplicates_from_the_old_page(reddit_run):
    code, cap, _ = reddit_run("duplicates", "--id", "https://redd.it/fixdup", "--json", guest=True, pages=DUPES_GUEST)
    assert code == 0, cap.out
    data = json.loads(cap.out)["data"]
    assert data["post"] == "fixdup" and data["title"] == "Card portal news & more" and data["subreddit"] == "r/news"
    assert [p["id"] for p in data["posts"]] == ["fixdu2", "fixdu3"]   # the original is the header, not a row
    politics, own = data["posts"]
    assert politics["flair"] == "News" and politics["url"] == "https://example.com/portal-news"
    assert politics["date"].startswith("2026-11-04T") and politics["author"] == "user_b"
    assert own["url"] is None and own["score"] == -4   # a text post links to itself
    # the same envelope as every other list: the cursor sits in `more`, with a `total`
    assert data["more"] == {"after": "t3_fixdu3"} and "after" not in data and "total" in data


@pytest.mark.parametrize("action, path, fixture, children, name", [
    ("duplicates", "/duplicates/fixdup.json", "duplicates.json", lambda d: d[1]["data"]["children"], "posts"),
    ("recent-comments", "/r/churning/comments.json", "recent_comments.json", lambda d: d["data"]["children"],
     "comments"),
])
def test_a_row_reddit_sends_twice_is_listed_once(reddit_run, action, path, fixture, children, name):
    data = load(fixture)
    rows = children(data)
    rows.insert(1, rows[0])
    argv = ("--id", "fixdup") if action == "duplicates" else ("--subreddit", "churning")
    code, cap, _ = reddit_run(action, *argv, "--json", pages={path: json.dumps(data).encode()})
    assert code == 0, cap.out
    ids = [r["id"] for r in json.loads(cap.out)["data"][name]]
    assert len(ids) == 2 and len(set(ids)) == 2


def test_duplicates_of_a_post_that_isnt_there(reddit_run):
    code, cap, _ = reddit_run("duplicates", "--id", "nopost", guest=True)
    assert code == 1 and "no Reddit post nopost" in cap.out


@pytest.mark.parametrize("given", ["fixdup", "t3_fixdup", "https://redd.it/fixdup", "redd.it/fixdup/",
                                   "https://old.reddit.com/r/news/comments/fixdup/card_portal_news/"])
def test_a_post_id_can_be_a_url_a_short_link_or_a_fullname(given):
    from esum.sites.reddit import _post_id
    assert _post_id(given) == "fixdup"


def test_a_short_link_without_an_id_is_refused(reddit_run):
    code, cap, backend = reddit_run("duplicates", "--id", "https://redd.it/")
    assert code == 2 and "redd.it/<id>" in cap.out and not backend.requests


def test_wiki_history_from_the_api(reddit_run):
    code, cap, backend = reddit_run("wiki", "--subreddit", "churning", "--show", "history", "--limit", "2",
                                    "--fields", "url", pages=HISTORY_API)
    assert code == 0, cap.out
    assert "revisions[2]{id,author,date,reason,url}:" in cap.out
    assert ('  9cbcc1b8-3dba-11f1-a153-da30e4825b6d,user_a,"2026-04-21 19:45",putting disclaimer in correct location,'
            '"https://www.reddit.com/r/churning/wiki/index?v=9cbcc1b8-3dba-11f1-a153-da30e4825b6d"') in cap.out
    assert ",user_b,\"2026-04-21 19:44\",null," in cap.out
    assert "--after WikiRevision_8c7999de-3dba-11f1-9720-da30e4825b6d" in cap.out
    assert "coverage" not in cap.out
    # the page itself is one item, so its command must not carry the list's --fields
    assert "Run `esum reddit wiki --subreddit churning --show page` to read the page as it is now" in cap.out
    assert parse_qs(urlsplit(backend.requests[0].url).query)["limit"] == ["2"]


def test_a_guest_sees_the_latest_wiki_revisions(reddit_run):
    code, cap, _ = reddit_run("wiki", "--subreddit", "churning", "--show", "history", "--json", guest=True,
                              pages=HISTORY_GUEST)
    assert code == 0, cap.out
    data = json.loads(cap.out)["data"]
    assert "a guest sees only the latest revisions" in data["coverage"] and data["more"] is None
    first, second = data["revisions"]
    assert (first["id"], first["author"], first["reason"]) == \
        ("9cbcc1b8-3dba-11f1-a153-da30e4825b6d", "user_a", "putting disclaimer in correct location & fixing a link")
    assert first["date"].startswith("2026-04-21T19:45:12") and second["reason"] is None


def test_a_guest_wiki_history_keeps_to_the_limit(reddit_run):
    code, cap, _ = reddit_run("wiki", "--subreddit", "churning", "--show", "history", "--limit", "1", "--json",
                              guest=True, pages=HISTORY_GUEST)
    assert code == 0, cap.out
    data = json.loads(cap.out)["data"]
    assert [r["id"] for r in data["revisions"]] == ["9cbcc1b8-3dba-11f1-a153-da30e4825b6d"]
    assert "a guest sees only the latest revisions" in data["coverage"]


def test_a_guest_cannot_page_back_through_wiki_history(reddit_run):
    code, cap, _ = reddit_run("wiki", "--subreddit", "churning", "--show", "history",
                              "--after", "WikiRevision_8c7999de-3dba-11f1-9720-da30e4825b6d",
                              guest=True, pages=HISTORY_GUEST)
    assert code == 3 and "LOGIN NEEDED" in cap.out + cap.err
    assert "9cbcc1b8" not in cap.out   # not the newest revisions passed off as an older page


NO_REVISIONS = b'<table class="wiki-revisions-table"><thead><tr><th>Author</th></tr></thead><tbody></tbody></table>'


@pytest.mark.parametrize("guest, pages", [
    (True, {"/r/churning/wiki/revisions/nope/": NO_REVISIONS}),   # what a guest gets for a page that isn't there
    (False, {"/r/churning/wiki/revisions/nope.json": b'{"kind": "Listing", "data": {"children": [], "after": null}}'}),
])
def test_history_of_a_wiki_page_that_does_not_exist_is_the_same_error_as_the_page(reddit_run, guest, pages):
    code, page_cap, _ = reddit_run("wiki", "--subreddit", "churning", "--page", "nope", guest=guest)
    assert code == 1
    code, cap, _ = reddit_run("wiki", "--subreddit", "churning", "--page", "nope", "--show", "history", guest=guest,
                              pages=pages)
    assert code == 1 and cap.out == page_cap.out


def test_a_wiki_page_with_no_revisions_shown_offers_a_runnable_command(reddit_run):
    code, cap, _ = reddit_run("wiki", "--subreddit", "churning", "--show", "history", guest=True,
                              pages={"/r/churning/wiki/revisions/index/": NO_REVISIONS})
    assert code == 0 and "0 found" in cap.out, cap.out
    step = re.search(r"Run `(esum reddit wiki [^`]*)` to read the page as it is now", cap.out)
    assert step, cap.out
    code, cap, _ = reddit_run(*shlex.split(step[1])[2:])
    assert code == 0 and "# r/churning wiki/index" in cap.out


def test_wiki_page_list_from_the_api(reddit_run):
    code, cap, _ = reddit_run("wiki", "--subreddit", "churning", "--show", "pages", pages=PAGES_API)
    assert code == 0, cap.out
    assert "pages[4]{page}:" in cap.out and "  faq/cards" in cap.out
    assert "Run `esum reddit wiki --subreddit churning --page config/sidebar --show page` to read that page" in cap.out


def test_a_page_read_from_a_list_runs_without_the_lists_fields(reddit_run):
    code, cap, _ = reddit_run("wiki", "--subreddit", "churning", "--show", "pages", "--fields", "all", pages=PAGES_API)
    assert code == 0, cap.out
    step = re.search(r"Run `(esum reddit wiki [^`]*)` to read that page", cap.out)
    assert step and "--fields" not in step[1]
    pages = {**PAGES_API, "/r/churning/wiki/config/sidebar/": (FIX / "wiki.html").read_bytes()}
    code, cap, _ = reddit_run(*shlex.split(step[1])[2:], pages=pages)
    assert code == 0 and "# r/churning wiki/config/sidebar" in cap.out


def test_a_guest_gets_no_wiki_page_list(reddit_run):
    code, cap, _ = reddit_run("wiki", "--subreddit", "churning", "--show", "pages", guest=True)
    assert code == 3 and "LOGIN NEEDED" in cap.out + cap.err


def test_wiki_after_needs_the_history(reddit_run):
    code, cap, backend = reddit_run("wiki", "--subreddit", "churning", "--after", "WikiRevision_x")
    assert code == 2 and "--show history" in cap.out and not backend.requests
