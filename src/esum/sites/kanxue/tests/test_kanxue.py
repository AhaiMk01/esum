"""Offline tests for the Kanxue (bbs.kanxue.com) module on saved, trimmed guest pages."""

from __future__ import annotations

from esum import ArgumentError

from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from esum.core.registry import Registry
from esum.sites.kanxue import parse
from esum.transport import Response
from esum.sites.kanxue import parse as kx

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 27, 15, 0)   # China time when the fixtures were saved


def load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def thread_posts(name: str, tid: int, page: int = 1):
    tp = parse.parse_thread_page(load(name), tid, page, NOW)
    raw = ([tp.op] if tp.op else []) + tp.posts
    return tp, {p.number: p for p in parse.to_posts(raw, tid, tp.op.user if tp.op else None)}


def test_dates_absolute_and_relative():
    assert parse.kx_date("2026-9-23 11:52") == datetime(2026, 9, 23, 11, 52)
    assert parse.kx_date("・2024-1-3", NOW) == date(2024, 1, 3)
    assert parse.kx_date("8分钟前", NOW) == datetime(2026, 10, 27, 14, 52)
    assert parse.kx_date("19小时前", NOW) == datetime(2026, 10, 26, 20, 0)
    assert parse.kx_date("3天前", NOW) == date(2026, 10, 24)
    assert parse.kx_date("刚刚", NOW) == NOW
    assert parse.kx_date("昨天 09:10", NOW) == datetime(2026, 10, 26, 9, 10)
    assert parse.kx_date("whenever", NOW) is None


def test_refs_from_ids_and_urls():
    assert parse.thread_ref("293027") == 293027
    assert parse.thread_ref("https://bbs.kanxue.com/thread-280081-3.htm#1760124") == 280081
    with pytest.raises(ArgumentError):
        parse.thread_ref("forum-4.htm")
    assert parse.forum_ref("forum-4-2.htm?orderby=tid&digest=0") == 4
    assert parse.forum_ref("161") == 161
    assert parse.forum_ref("逆向工程") is None


def test_home_catalog_with_sub_boards():
    forums = parse.parse_forums(load("home.html"))
    by_id = {f["id"]: f for f in forums}
    assert by_id[4] == {**by_id[4], "name": "逆向工程", "category": "PC安全", "topics": 35436, "parent": None}
    assert by_id[161]["name"] == "Android安全" and by_id[161]["category"] == "移动安全"
    assert by_id[122]["parent"] == 37 and by_id[122]["name"] == "KCTF2026参赛题目提交区"
    assert "KCTF2026" not in by_id[37]["description"]          # sub-board links are not description text
    assert [f["id"] for f in forums].index(122) == [f["id"] for f in forums].index(37) + 1
    assert by_id[4]["description"].startswith("Windows、UNIX")


def test_board_page_rows_flags_and_counts():
    data = parse.parse_forum_page(load("forum_4.html"), NOW)
    assert (data["id"], data["name"], data["topics"], data["page"], data["pages"]) == (4, "逆向工程", 35436, 1, 1182)
    rows = {r["id"]: r for r in data["threads"]}
    sticky = rows[280081]
    assert sticky["sticky"] and sticky["author"] == "MallocFree" and sticky["date"] == "2024-01-03T21:05:00"
    assert (sticky["views"], sticky["replies"], sticky["pages"]) == (94198, 51, 3)
    assert sticky["last_user"] == "mb_oxqwhbqt" and sticky["last_date"] == "2026-10-21"   # "6天前"
    assert rows[293322]["last_user"] is None and rows[293322]["replies"] == 0
    assert rows[293117]["digest"] == "优秀" and rows[293117]["attachment"]
    assert rows[287862]["digest"] == "关注" and not rows[287862]["sticky"]
    assert rows[293027]["last_date"] == "2026-10-27T14:52:00"                            # "8分钟前"
    assert rows[293117]["title"].startswith("[原创]银狐远控木马深度分析")


def test_hot_list_includes_the_highlighted_top_three():
    rows = parse.parse_hot(load("hot_week.html"))
    assert [r["rank"] for r in rows] == [1, 2, 3, 4, 5]
    assert rows[0] == {"rank": 1, "id": 293303, "title": "[原创]VMProtect3.5.1还原演示", "author": "zZhouQing",
                       "heat": 16717, "url": "https://bbs.kanxue.com/thread-293303.htm"}


def test_thread_first_page_floors_quotes_and_gaps():
    tp, posts = thread_posts("thread_280081.html", 280081)
    assert tp.title == "“内核驱动高级班”第26期招生进行中" and (tp.forum, tp.forum_id) == ("职业生涯", 137)
    assert (tp.replies, tp.total, tp.pages, tp.views) == (51, 52, 3, 94200)
    assert sorted(posts) == [1, 2, 3, 4, 6, 7]                 # floor 5 is deleted or hidden on the site
    op = posts[1]
    assert op.is_op and op.user == "MallocFree" and op.likes == 20 and op.date == datetime(2024, 1, 3, 21, 5)
    assert "传递专业知识" not in op.text and "最后于" not in op.text   # promo link and edit note dropped
    assert posts[4].reply_to == 3 and posts[4].is_op
    assert posts[4].text.startswith("> @NoThx: 在线还是实地？\n每周日下午")
    assert posts[3].reply_to is None and not posts[3].is_op
    assert posts[3].url == "https://bbs.kanxue.com/thread-280081.htm#1761125"


def test_later_pages_have_no_opening_post_and_page_urls():
    tp, posts = thread_posts("thread_280081_3.html", 280081, 3)
    assert tp.op is None and sorted(posts) == [51, 52]
    assert posts[51].url.startswith("https://bbs.kanxue.com/thread-280081-3.htm#")


def test_opening_post_cut_for_guests_is_marked_hidden():
    tp, posts = thread_posts("thread_293027.html", 293027)
    assert not tp.op_truncated   # the page carries the whole Markdown source
    assert "样本站点是 otto.de 的商品详情页，SDK 版本为 `j-1.2.779`" in posts[1].text
    assert parse.TRUNCATED_NOTE not in posts[1].text and "回复或点赞" not in posts[1].text
    assert posts[1].text.count("样本站点是 otto.de") == 1        # read once, from the source


def test_attachments_listed_by_name_and_quotes_matched():
    tp, posts = thread_posts("thread_293307.html", 293307)
    assert not tp.op_truncated and sorted(posts) == list(range(1, 9))
    assert "[attachment: lic.lic （0.43kb，20次下载）]" in posts[1].text
    assert "attach-download" not in posts[3].text
    assert (posts[4].reply_to, posts[5].reply_to, posts[7].reply_to) == (3, 2, 6)
    assert posts[8].is_op and posts[8].reply_to is None


def test_obfuscated_links_are_hidden():
    _, posts = thread_posts("thread_293211.html", 293211)
    assert "开源地址：[link hidden from guests]" in posts[1].text
    assert "elink@" not in posts[1].text and "K9s2c8" not in posts[1].text


def test_quote_target_needs_user_and_text():
    a = parse.RawPost(2, 1, "a", "hello world again", "hello world again")
    b = parse.RawPost(3, 2, "b", "other", "other")
    assert parse.quote_target(("a", "hello world"), 4, [a, b]) == 2
    assert parse.quote_target(("b", "hello world"), 4, [a, b]) is None
    assert parse.quote_target(("a", "hello world"), 2, [a, b]) is None


def test_message_pages():
    assert parse.message_of(load("msg_login.html")) == "请先登录"
    assert parse.message_of(load("msg_no_access.html")) == "您所在的用户组无权访问该版块"
    assert parse.message_of(load("msg_not_found.html")) == "主题不存在！"
    assert parse.message_of(load("forum_4.html")) is None


# --- whole actions on a fake T2 backend --------------------------------------------------------------

ROUTES = {
    "/": "home.html",
    "/forum-4-1.htm": "forum_4.html",
    "/forum-122-1.htm": "msg_no_access.html",
    "/thread-hotlist-all-3.htm": "hot_week.html",
    "/thread-280081.htm": "thread_280081.html",
    "/thread-280081-2.htm": "thread_280081_2.html",
    "/thread-280081-3.htm": "thread_280081_3.html",
    "/thread-293027.htm": "thread_293027.html",
    "/thread-1.htm": "msg_not_found.html",
    "/new-tid.htm": "msg_login.html",
}


class PathBackend:
    def __init__(self):
        self.requests = []

    def send(self, request, *, before_send=lambda: None):
        before_send()
        self.requests.append(request)
        name = ROUTES.get(urlsplit(request.url).path)
        if name is None:
            return Response(404, request.url, {}, b"not found")
        return Response(200, request.url, {"content-type": "text/html; charset=utf-8"}, load(name).encode())

    def close(self):
        pass


@pytest.fixture
def run(action_runner, monkeypatch, capsys):
    backend = PathBackend()
    action_runner.backend = backend
    monkeypatch.setattr(parse, "china_now", lambda: NOW)
    reg = Registry.discover(custom_root=None)

    def _run(*argv):
        code = action_runner.cli(["kanxue", *argv], registry=reg)
        captured = capsys.readouterr()
        return code, captured.out + captured.err, backend

    return _run


def test_site_declares_polite_spacing():
    site = Registry.discover(custom_root=None).get("kanxue")
    assert site.extras["min_interval"] == 1.0 and site.transport.value == "browser_http"


def test_forums_action(run):
    code, out, _ = run("forums")
    lines = out.splitlines()
    assert code == 0 and "count: 29" in lines and "forums[29]{id,name,topics}:" in lines
    assert "  161,Android安全,8983" in lines and "  122,KCTF2026参赛题目提交区,null" in lines
    assert "  Run `esum kanxue forum --id <id>` to list a board's threads" in lines


def test_forum_action_by_id_url_and_name(run):
    code, out, backend = run("forum", "--id", "4")
    lines = out.splitlines()
    assert code == 0 and {"id: 4", "name: 逆向工程", "order: last-reply", "page: 1", "pages: 1182"} <= set(lines)
    assert any(line.startswith("count: ") and line.endswith(" of 35436 total") for line in lines)
    assert any(line.startswith("  280081,") and line.endswith(",51,2026-10-21") for line in lines)
    assert "  Run `esum kanxue forum --id 4 --page 2` for the next page" in lines
    code, out, _ = run("forum", "--id", "4", "--fields", "digest,attachment")
    assert code == 0 and "{id,title,replies,last_date,digest,attachment}:" in out and ",优秀,true" in out
    assert backend.requests[-1].url == "https://bbs.kanxue.com/forum-4-1.htm?orderby=lastpid&digest=0"
    run("forum", "--id", "https://bbs.kanxue.com/forum-4.htm", "--order", "newest", "--digest")
    assert backend.requests[-1].url.endswith("/forum-4-1.htm?orderby=tid&digest=1")
    code, out, backend = run("forum", "--id", "逆向工程")
    assert code == 0 and [urlsplit(r.url).path for r in backend.requests[-2:]] == ["/", "/forum-4-1.htm"]


def test_forum_closed_to_the_user_group_is_a_raw_error(run):
    code, out, _ = run("forum", "--id", "122")
    assert code == 1 and "您所在的用户组无权访问该版块" in out and "LOGIN NEEDED" not in out
    assert "esum docs kanxue" in out


def test_login_only_page_is_login_needed(run):
    """Over T2 with the browser's cookies, logging in does help, so "请先登录" is LOGIN NEEDED."""
    from esum.sites.kanxue import get_page

    class Ctx:
        class http:
            get_text = staticmethod(lambda path: load("msg_login.html"))

        def login_needed(self, detail=None):
            from esum.core.errors import LoginNeeded
            return LoginNeeded("kanxue", "https://bbs.kanxue.com", detail=detail)

    with pytest.raises(Exception) as caught:
        get_page(Ctx(), "/new-tid.htm", "latest threads")
    assert type(caught.value).__name__ == "LoginNeeded" and "请先登录" in str(caught.value.detail)


def test_hot_action(run):
    code, out, _ = run("hot", "--limit", "2")
    lines = out.splitlines()
    assert code == 0 and {"period: week", "count: 2 of 5 total", "threads[2]{rank,id,title,heat}:"} <= set(lines)
    assert '  1,293303,"[原创]VMProtect3.5.1还原演示",16717' in lines


def test_thread_action_fetches_only_needed_pages(run):
    code, out, backend = run("thread", "--id", "280081", "--from", "50", "--to", "52")
    assert code == 0
    assert [urlsplit(r.url).path for r in backend.requests] == ["/thread-280081.htm", "/thread-280081-2.htm",
                                                                "/thread-280081-3.htm"]
    assert "Board: 职业生涯 [id 137] · 52 floors in 3 page(s) of 25 · 94200 views" in out
    assert "[mb_bnjbcdzy #50]" in out and "[mb_oxqwhbqt #52] (2026-10-21)" in out
    assert "[... omitted 49 post numbers (#1–#49), not read …]" in out


def test_thread_action_medium_reads_page_one_and_marks_hidden(run):
    code, out, backend = run("thread", "--id", "https://bbs.kanxue.com/thread-293027.htm")
    assert code == 0 and len(backend.requests) == 1
    assert "opening post cut for guests" not in out and "[hidden: the rest of this post" not in out
    assert "[红皮西瓜 #1] (2026-09-23 11:52 · 👍33 · OP):" in out


def test_missing_thread_is_an_error(run):
    code, out, _ = run("thread", "--id", "1")
    assert code == 1 and "主题不存在" in out


# --- What the page says must survive: code, struck text, images, attachments, and honest gaps --------

MARKDOWN = "# 标题\n\n比例 0.85~0.90\n\n```python\ndef f():\n    return 1\n```\n\n| a | b |\n|---|---|\n\n![图](upload/attach/1.webp)"


def post_html(markdown: str = MARKDOWN) -> str:
    return (f'<textarea class="d-none js-message-markdown">{markdown}</textarea><p>比例 0.85<del>0.90</del></p>'
            '<div class="expandNoteBox">回复或点赞可查看</div>')


def test_kanxue_reads_the_whole_markdown_source():
    text, _, truncated = kx.clean_message(post_html())
    assert not truncated and "0.85~0.90" in text and "    return 1" in text and "| a | b |" in text
    assert "(https://bbs.kanxue.com/upload/attach/1.webp)" in text


def test_kanxue_without_a_markdown_source_still_reports_a_cut_post():
    text, _, truncated = kx.clean_message('<p>前半</p><div class="expandNoteBox">回复或点赞可查看</div>')
    assert truncated and text == "前半"


def test_kanxue_relative_images_become_urls():
    text, _, _ = kx.clean_message('<p>老师<img src="upload/attach/202610/1.webp"/></p>')
    assert "[image: https://bbs.kanxue.com/upload/attach/202610/1.webp]" in text


def test_kanxue_knows_its_verification_page():
    assert kx.is_verification_page("<html><head><title>安全验证</title></head><body>登录后1小时内无需重复验证</body></html>")
    assert not kx.is_verification_page("<html><head><title>脱壳-Android安全-看雪</title></head></html>")
