"""Offline tests for the NodeSeek module on saved (trimmed) list and post pages."""

from __future__ import annotations

from esum import ArgumentError

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from esum import Transport
from esum.core.registry import Registry
from esum.sites.nodeseek import parse
from esum.transport import Response
from esum.sites.nodeseek import parse as ns

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def post_page(n: int) -> parse.PostPage:
    return parse.parse_post_page(parse.page_config(load(f"post_1012229_p{n}.html")), 1012229)


# --- parsers -----------------------------------------------------------------------------------------

def test_config_decodes_and_lists_categories():
    config = parse.page_config(load("list_home.html"))
    assert config["pageType"] == "list" and parse.is_guest(config)
    cats = {c["key"]: c for c in parse.categories(config)}
    assert cats["daily"]["name"] == "日常" and cats["daily"]["in_site_list"]
    assert not cats["life"]["in_site_list"] and cats["meaningless"]["admin_only"]
    assert parse.page_config("<html>no data</html>") is None


def test_list_page_rows_and_flags():
    data = parse.parse_list_page(load("list_home.html"))
    assert data["page"] == 1 and data["last_page"] == 100 and data["has_next"]
    rows = {r["id"]: r for r in data["threads"]}
    assert len(rows) == 8
    pinned = rows[832584]
    assert pinned["title"].startswith("iLatency公测") and pinned["pinned"] and pinned["author"] == "酒神"
    assert pinned["category"] == "dev" and pinned["replies"] == 721 and pinned["views"] == 351788
    assert pinned["last_reply"] == "2026-10-27T05:30:13.000Z" and pinned["last_reply_by"] == "cogine"
    members = rows[1010528]
    assert members["read_level"] == 1 and members["read_only"] and members["category_name"] == "内版"
    plain = rows[1012536]
    assert not plain["pinned"] and plain["read_level"] == 0 and plain["url"] == "https://www.nodeseek.com/post-1012536-1"


def test_category_page_two_pager():
    data = parse.parse_list_page(load("list_daily_p2.html"))
    assert data["page"] == 2 and data["has_next"] and len(data["threads"]) == 4
    assert all(r["category"] == "daily" for r in data["threads"])


def test_post_page_floors_and_hot_repeats():
    p1 = post_page(1)
    assert p1.title == "生物信息学这方面还有前景吗" and p1.pages == 3 and p1.category == "daily"
    floors = [f.floor for f in p1.floors]
    assert floors[0] == 0 and 16 in floors          # floor 16 is a "hot" repeat from page 2
    assert parse.last_floor(p1) == 10                # the repeat doesn't count for page 1
    assert parse.last_floor(post_page(3)) == 30
    assert p1.floors[0].is_op and p1.floors[0].user == "AnthonyYEYE"


def test_reply_markers_follow_the_site_link():
    p2 = {f.floor: f for f in post_page(2).floors}
    post = parse.to_post(p2[19], 1012229)
    assert post.reply_to == 16 and post.is_op and post.text.startswith("@caiisen 我目前")
    assert post.url == "https://www.nodeseek.com/post-1012229-2#19"
    assert parse.reply_target("@a [#5](https://www.nodeseek.com/post-1-1#5) hi", 1, 9) == (5, "@a hi")
    assert parse.reply_target("@a [#5](https://www.nodeseek.com/post-2-1#5) hi", 1, 9)[0] is None  # other post
    assert parse.reply_target("@a [#0](https://www.nodeseek.com/post-1-1#0) hi", 1, 9)[0] is None  # the post itself
    assert parse.reply_target("@a [#12](https://www.nodeseek.com/post-1-2#12) hi", 1, 9)[0] is None  # later floor
    assert parse.reply_target("中间 @a [#5](https://www.nodeseek.com/post-1-1#5)", 1, 9)[0] is None


def test_paid_likes_and_free_likes():
    f16 = {f.floor: f for f in post_page(2).floors}[16]
    post = parse.to_post(f16, 1012229)
    assert post.likes == 0 and "[🍗1 chicken legs" in post.text


def test_refs_and_urls():
    assert parse.thread_ref("1012229") == 1012229
    assert parse.thread_ref("https://www.nodeseek.com/post-832584-9#81") == 832584
    with pytest.raises(ArgumentError):
        parse.thread_ref("abc")
    assert parse.floor_url(5, 0) == "https://www.nodeseek.com/post-5-1"
    assert parse.floor_url(5, 10) == "https://www.nodeseek.com/post-5-1#10"
    assert parse.floor_url(5, 11) == "https://www.nodeseek.com/post-5-2#11"


def test_members_only_page_message():
    html = load("post_members_only.html")
    assert parse.page_config(html)["pageType"] == "error"
    assert parse.MEMBERS_ONLY in parse.page_message(html)


# --- whole actions on a fake backend standing in for the T2 transport -------------------------------

class PathBackend:
    def __init__(self):
        self.requests = []

    def send(self, request, *, before_send=lambda: None):
        before_send()
        self.requests.append(request)
        path = urlsplit(request.url).path
        html = {"/": "list_home.html", "/categories/daily/page-2": "list_daily_p2.html",
                "/post-1012229-1": "post_1012229_p1.html", "/post-1012229-2": "post_1012229_p2.html",
                "/post-1012229-3": "post_1012229_p3.html", "/post-1012528-1": "post_members_only.html"}.get(path)
        if path == "/categories/inside":
            return Response(400, request.url, {"content-type": "application/json; charset=utf-8"},
                            b'{"success":false,"message":"Wrong category"}')
        if html is None:
            return Response(404, request.url, {"content-type": "text/plain; charset=utf-8"}, b"Not Found")
        status = 404 if "members_only" in html else 200
        return Response(status, request.url, {"content-type": "text/html; charset=utf-8"},
                        (FIX / html).read_bytes())

    def close(self):
        pass


@pytest.fixture
def run(action_runner, capsys):
    backend = PathBackend()
    action_runner.backend = backend
    reg = Registry.discover(custom_root=None)

    def _run(*argv):
        code = action_runner.cli(["nodeseek", *argv], registry=reg)
        captured = capsys.readouterr()
        return code, captured.out + captured.err, backend

    return _run


def test_site_declares_browser_transport_with_cookies():
    s = Registry.discover(custom_root=None).sites["nodeseek"]
    assert s.transport is Transport.BROWSER_HTTP and s.extras.get("browser_cookies", True)


def test_categories_action(run):
    code, out, _ = run("categories")
    lines = out.splitlines()
    assert code == 0 and "count: 13" in lines and "categories[13]{key,name,in_site_list}:" in lines
    assert "  life,生活,false" in lines and "  daily,日常,true" in lines
    assert "  Run `esum nodeseek listing --category <key>` to list a category's posts" in lines


def test_listing_action(run):
    code, out, backend = run("listing")
    lines = out.splitlines()
    assert code == 0 and "category: all" in lines and "sort: last-reply" in lines and "page: 1" in lines
    assert "threads[8]{id,title,replies,flags}:" in lines
    assert "  1010528,该发内版的发内版,58,pinned; read level 1; read-only" in lines
    assert "  Run `esum nodeseek listing --page 2` for the next page" in lines
    assert "  Run `esum nodeseek thread --id <id>` to read a post with its comments" in lines
    code, out, backend = run("listing", "--category", "daily", "--page", "2", "--order", "newest")
    lines = out.splitlines()
    assert code == 0 and "category: daily" in lines and "sort: newest" in lines and "page: 2" in lines
    assert "  Run `esum nodeseek listing --category daily --page 3 --order newest` for the next page" in lines
    assert parse_qs(urlsplit(backend.requests[-1].url).query) == {"sortBy": ["postTime"]}


def test_listing_errors_pass_through(run):
    code, out, _ = run("listing", "--category", "inside")
    assert code == 1 and "HTTP 400" in out and "Wrong category" in out
    code, out, _ = run("listing", "--category", "../x")
    assert code == 2 and "not a category key" in out


def test_thread_action_fetches_only_needed_pages(run):
    code, out, backend = run("thread", "--id", "1012229", "--from", "15", "--to", "19")
    assert code == 0 and "5 of 31 posts shown" in out and "30 comments in 3 page(s) of 10" in out
    assert "[AnthonyYEYE #19] (2026-10-27 05:04 · OP · ↩#16): @caiisen 我目前" in out
    assert "[... omitted 14 post numbers (#1–#14), not read …]" in out and "[... omitted 11 post numbers (#20–#30), not read …]" in out
    assert "#0]" not in out
    paths = [urlsplit(r.url).path for r in backend.requests]
    assert paths == ["/post-1012229-1", "/post-1012229-3", "/post-1012229-2"]


def test_thread_action_full_keeps_each_floor_once(run):
    code, out, backend = run("thread", "--id", "https://www.nodeseek.com/post-1012229-2#16", "--mode", "full")
    assert code == 0 and "31 of 31 posts shown" in out
    assert out.count("[caiisen #16]") == 1
    assert "[AnthonyYEYE #0] (2026-10-27 04:46 · OP · edited" in out


def test_members_only_post_needs_login(run):
    code, out, _ = run("thread", "--id", "1012528")
    assert code == 3 and "LOGIN NEEDED" in out and "本帖需要注册用户才能查看" in out


def test_missing_post_is_an_error(run):
    code, out, _ = run("thread", "--id", "9999999")
    assert code == 1 and "nodeseek post 9999999: not found" in out
    assert "check the id or name" in out and "may have been removed" in out


# --- V2EX and NodeSeek against their raw pages: nothing unknown shown as zero, nothing silently missing ---

def test_nodeseek_keeps_the_edit_time():
    config = {"postData": {"comments": [{"floorIndex": 0, "markdown": "x", "poster": {"name": "a"},
                                         "time": {"createdDate": "2026-10-30T07:00:00Z",
                                                  "editedDate": "2026-10-30T08:02:35Z"}}]}}
    assert ns.parse_post_page(config, 1).floors[0].edited == "2026-10-30 08:02"
