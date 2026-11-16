"""Discuz! engine on the engine's fixtures (tests/fixtures/discuz/): scrubbed pages captured from
52pojie, and search pages from Stage1st. Actions run on `board`, a synthetic site config. A site
config's own actions are tested in that site's folder."""

from __future__ import annotations

from datetime import date, datetime
from urllib.parse import urlencode

import pytest

from esum import Gap
from esum.engines.discuz import parse
from esum.transport import Response
from esum.engines.discuz import parse as dz

from support import engine_fixtures, engine_sites  # noqa: F401

FIX = engine_fixtures("discuz")
BASE = "https://www.52pojie.cn"   # where most of the pages were captured, and `board`'s address


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def page(url: str, name: str | None = None, text: str | None = None) -> Response:
    """A 52pojie response: GBK, as the site serves it."""
    body = (text if text is not None else fixture(name)).encode("gbk")
    return Response(200, url, {"content-type": "text/html; charset=gbk"}, body)


def forum_php(**params) -> str:
    return f"{BASE}/forum.php" + (f"?{urlencode(params)}" if params else "")


# --- pure parsing ------------------------------------------------------------------------------------

def test_dz_date_forms():
    assert parse.dz_date("<span>2026-10-5 23:30</span>") == datetime(2026, 10, 5, 23, 30)
    assert parse.dz_date("2026-1-22") == date(2026, 1, 22)
    assert parse.dz_date('发表于 <span title="2026-10-26 09:25:33">3&nbsp;天前</span>') == datetime(2026, 10, 26, 9, 25, 33)
    assert parse.dz_date("昨天&nbsp;12:30", date(2026, 10, 27)) == datetime(2026, 10, 26, 12, 30)
    assert parse.dz_date("半小时前") is None and parse.dz_date("") is None


def test_thread_and_forum_refs():
    assert parse.thread_ref("2134210") == (2134210, None)
    assert parse.thread_ref(f"{BASE}/thread-2134210-3-1.html") == (2134210, 3)
    assert parse.thread_ref(f"{BASE}/forum.php?mod=viewthread&tid=2134210&extra=page%3D1&page=2") == (2134210, 2)
    assert parse.thread_ref(f"{BASE}/forum.php?mod=redirect&goto=findpost&ptid=2134210&pid=56068004") == (2134210, None)
    assert parse.thread_ref(f"{BASE}/archiver/tid-2134210.html?page=2") == (2134210, 2)
    with pytest.raises(ValueError):
        parse.thread_ref("not a thread")
    assert parse.forum_ref("24") == 24
    assert parse.forum_ref(f"{BASE}/forum-24-3.html") == 24
    assert parse.forum_ref(f"{BASE}/forum.php?mod=forumdisplay&amp;fid=24&page=2") == 24
    assert parse.forum_ref("编程语言区") is None


def test_floor_labels():
    assert parse.floor_of("<em>7</em><sup>#</sup>") == 7
    assert parse.floor_of("<font color=red><b>楼主</b></font>") == 1
    assert parse.floor_of("<b>沙发</b>") == 2 and parse.floor_of("地板") == 4
    assert parse.floor_of("12#") == 12
    assert parse.floor_of("\n推荐\n") is None


def test_board_index():
    rows = parse.parse_forums(fixture("index.html"))
    assert [r["id"] for r in rows] == [2, 5, 75, 22, 68, 24, 74, 62, 42]
    first = rows[0]
    assert first["name"] == "『原创发布区』" and first["group"] == "【 软件安全 】" and first["parent"] is None
    assert (first["threads"], first["posts"], first["today"]) == (13459, 2853405, 203)
    assert first["description"].startswith("◇吾爱破解坛友原创作品展示")
    sub = next(r for r in rows if r["id"] == 75)
    assert sub["parent"] == 5 and sub["name"] == "『脱壳破解讨论求助区』"
    grid = next(r for r in rows if r["id"] == 62)   # the grid layout of a group
    assert grid["group"] == "【 活动策划专区 】" and (grid["threads"], grid["posts"]) == (697, 29732)


def test_forum_page_rows():
    data = parse.parse_forum_page(fixture("forum.html"), f"{BASE}/")
    assert (data["name"], data["threads_total"], data["page"], data["pages"]) == ("『编程语言区』", 21389, 1, 428)
    rows = data["threads"]
    assert [r["id"] for r in rows] == [2088125, 1691883, 2131070, 2134210, 2134065]   # no announcement row
    sticky, closed, first = rows[0], rows[1], rows[2]
    assert sticky["sticky"] and not sticky["closed"] and sticky["pages"] == 121
    assert closed["sticky"] and closed["closed"] and closed["type"] == "其他转载"
    assert first == {
        "id": 2131070, "title": "【油猴脚本】B站动态筛选助手", "type": "其他转载", "type_id": 33,
        "url": f"{BASE}/forum.php?mod=viewthread&tid=2131070", "forum": "", "forum_id": None,
        "starter": "麦米尔加弗德", "started": datetime(2026, 10, 5, 23, 30), "replies": 41, "views": 1395,
        "last_post": datetime(2026, 10, 27, 14, 30), "last_user": "Pandacute", "pages": 5,
        "sticky": False, "digest": False, "closed": False, "read_permission": None}
    assert rows[4]["read_permission"] == 10 and rows[4]["type"] == "Python 原创" and rows[4]["type_id"] == 385


def test_guide_rows_carry_their_board():
    rows = parse.parse_thread_rows(fixture("guide_hot.html"), f"{BASE}/")
    assert len(rows) == 3 and parse.page_count(fixture("guide_hot.html")) == (1, 2)
    assert rows[0]["title"] == "中国香烟博物馆v1.0.0" and rows[0]["forum"] == "『精品软件区』"
    assert rows[0]["forum_id"] == 16 and (rows[0]["replies"], rows[0]["views"]) == (180, 6962)


def test_thread_page_one():
    tp = parse.parse_thread_page(fixture("thread_p1.html"))
    assert (tp.title, tp.type, tp.forum, tp.forum_id) == ("库存管理", "C&C++ 转载", "『编程语言区』", 24)
    assert (tp.views, tp.replies, tp.page, tp.pages) == (706, 17, 1, 2)
    assert [p.number for p in tp.posts] == [1, 2, 3, 7, 10]
    assert [p.number for p in tp.featured] == [0]   # a 推荐 post has no floor number in the page
    op, second, third = tp.posts[:3]
    assert op.user == "再贱就再见" and op.date == datetime(2026, 10, 26, 9, 25) and op.likes == 2
    assert "本帖最后由" not in op.text and "下载附件" not in op.text and "复制代码" not in op.text
    assert "[image: 1.png https://" in op.text and "[image: 2.png https://" in op.text
    assert '#include "document.h"\n#include <stdio.h>' in op.text   # code stays as posted
    assert second.user == "dupengpeng" and second.quotes == []
    assert third.quotes == [56068004] and third.text.startswith("> @dupengpeng: 谢谢老师的分享")


def test_to_posts_marks_op_and_replies():
    raw = parse.parse_thread_page(fixture("thread_p1.html")).posts
    posts = {p.number: p for p in parse.to_posts(raw, "再贱就再见")}
    assert posts[1].is_op and posts[3].is_op and not posts[2].is_op
    assert posts[3].reply_to == 2 and posts[7].reply_to == 3 and posts[2].reply_to is None


def test_notices():
    login = fixture("thread_login.html")
    assert parse.needs_login(login) == "您需要先登录才能继续本操作"
    missing = fixture("notice_noboard.html")
    assert parse.message(missing) == "抱歉，指定的版块不存在" and parse.needs_login(missing) is None
    assert parse.message(fixture("forum.html")) is None
    denied = '<form id="lsform"></form><div id="messagetext" class="alert_error"><p>抱歉，您没有权限访问该版块</p>'
    assert parse.needs_login(denied) and not parse.needs_login(denied.replace('id="lsform"', ""))



# --- whole actions ------------------------------------------------------------------------------------

@pytest.fixture
def engine_cli(capsys, monkeypatch, action_runner, engine_sites):
    """The CLI on `board`: a synthetic GBK site on the Discuz engine with no action of its own."""
    monkeypatch.setenv("ESUM_SPILL_CHARS", "10000000")
    registry = engine_sites("discuz", board=f'base_url="{BASE}", label="A test board", charset="gbk", '
                            'transport=Transport.BROWSER_HTTP')

    def _run(*argv: str):
        code = action_runner.cli(argv, registry=registry)
        out = capsys.readouterr()
        return code, out.out, out.err

    return _run


# --- ported from xgoose discuz-forum: search, type filter, anti-copy noise, lazy images, 404 notices ----

S1 = "https://stage1st.com/2b"   # search_s1.html comes from Stage1st (Discuz! X3.5, UTF-8, sub-path)


def search_php(**params) -> str:
    from esum.transport import form_encode
    return f"{BASE}/search.php?" + form_encode({k: v for k, v in params.items() if v is not None}, "gbk")


def test_search_results_page():
    data = parse.parse_search(fixture("search_s1.html"), f"{S1}/", today=date(2026, 11, 7))
    assert (data["total"], data["page"], data["pages"]) == (100, 1, 2)
    first, _, third = data["threads"]
    assert first == {
        "id": 2293371, "title": "任天堂26财年2季度财报，Switch 2 2635万，节奏天国289万，斯普拉顿242万，火纹134万",
        "replies": 64, "views": 5901, "date": datetime(2026, 11, 5, 21, 10), "author": "绿冰", "author_id": 248304,
        "forum": "游戏论坛", "forum_id": 4, "excerpt": first["excerpt"],
        "url": f"{S1}/forum.php?mod=viewthread&tid=2293371"}
    assert first["excerpt"].startswith("游戏机销量成绩 主要游戏销量成绩") and first["excerpt"].endswith("...")
    assert third["title"] == "为了准备迎接switch移植到手机，求推手柄"   # the highlight markup is dropped
    assert parse.search_id(f"{S1}/search.php?mod=forum&searchid=300&orderby=lastpost") == 300


def test_search_wait_notice():
    assert parse.search_wait("抱歉，您在 10 秒内只能进行一次搜索") == 10
    assert parse.search_wait("抱歉，您在 10 秒内只能发一次帖") is None


def test_search_action_encodes_gbk_and_pages_by_search_id(fake_http, engine_cli):
    first = search_php(mod="forum", srchtxt="脱壳", searchsubmit="yes", orderby="lastpost", ascdesc="desc")
    assert "srchtxt=%CD%D1%BF%C7" in first   # the query goes out in the site's charset
    landed = f"{BASE}/search.php?mod=forum&searchid=300&orderby=lastpost&ascdesc=desc&searchsubmit=yes&kw=x"
    fake_http.routes[first] = page(landed, "search_s1.html")
    code, out, err = engine_cli("board", "search", "--query", "脱壳")
    assert code == 0, err
    lines = out.splitlines()
    assert {"query: 脱壳", "search_id: 300", "count: 3 of 100 total", "threads[3]{id,title,replies,date}:"} <= set(lines)
    assert "  Run `esum board search --query '脱壳' --page 2 --search-id 300` for the next page" in lines
    second = search_php(mod="forum", searchid=300, orderby="lastpost", ascdesc="desc", searchsubmit="yes", page=2)
    fake_http.routes[second] = page(second, "search_s1.html")
    fake_http.requests.clear()
    code, out, err = engine_cli("board", "search", "--query", "脱壳", "--page", "2", "--search-id", "300")
    assert code == 0, err
    assert [r.url for r in fake_http.requests] == [second]   # no new search


def test_search_with_no_hits_is_an_empty_list(fake_http, engine_cli):
    first = search_php(mod="forum", srchtxt="脱壳", searchsubmit="yes", orderby="lastpost", ascdesc="desc")
    landed = f"{BASE}/search.php?mod=forum&searchid=381&orderby=lastpost&ascdesc=desc&searchsubmit=yes"
    fake_http.routes[first] = page(landed, "search_s1_empty.html")
    code, out, err = engine_cli("board", "search", "--query", "脱壳")
    assert code == 0, out
    assert "no thread title matches '脱壳'" in out


def test_search_page_without_results_or_notice_is_an_error(fake_http, engine_cli):
    first = search_php(mod="forum", srchtxt="脱壳", searchsubmit="yes", orderby="lastpost", ascdesc="desc")
    landed = f"{BASE}/search.php?mod=forum&searchid=381&orderby=lastpost&ascdesc=desc&searchsubmit=yes"
    fake_http.routes[first] = page(landed, text="<html><title>Maintenance</title><body>Try later</body></html>")
    code, out, err = engine_cli("board", "search", "--query", "脱壳")
    assert code == 1, out
    assert "no thread title matches" not in out
    assert out.startswith("error: ") and "\nhelp: " in out


def test_search_sent_to_another_site_is_refused(fake_http, engine_cli):
    first = search_php(mod="forum", srchtxt="脱壳", searchsubmit="yes", orderby="lastpost", ascdesc="desc")
    fake_http.routes[first] = Response(200, "https://www.bing.com/search?q=site%3A52pojie.cn", {}, b"<html></html>")
    code, out, _ = engine_cli("board", "search", "--query", "脱壳")
    assert code == 1 and "sends searches to another site (www.bing.com)" in out


def test_search_flood_notice_waits_once_then_reads(fake_http, engine_cli, monkeypatch):
    from esum.engines import discuz
    slept = []
    monkeypatch.setattr(discuz.time, "sleep", slept.append)
    first = search_php(mod="forum", srchtxt="脱壳", searchsubmit="yes", orderby="lastpost", ascdesc="desc")
    flood = '<div id="messagetext" class="alert_error"><p>抱歉，您在 10 秒内只能进行一次搜索</p></div>'
    landed = f"{BASE}/search.php?mod=forum&searchid=301&orderby=lastpost&ascdesc=desc&searchsubmit=yes"
    fake_http.routes[first] = [page(first, text=flood), page(landed, "search_s1.html")]
    code, out, err = engine_cli("board", "search", "--query", "脱壳", "--json")
    assert code == 0, out
    assert slept == [11] and '"search_id": 301' in out


def test_post_body_drops_anti_copy_noise_and_reads_lazy_images():
    msg = ('第一行<font class="jammer">x9Kq2</font>正文<span style="display:none">aZ3</span>结束<br />'
           '<img id="aimg_1" src="static/image/common/none.gif" file="https://img.example/a.png" class="zoom" />'
           '<img src="static/image/smiley/default/smile.gif" />'
           '<div class="locked">本帖隐藏的内容需要回复才可以浏览</div>')
    text, _ = parse.message_text(msg)
    assert text.startswith("第一行正文结束")
    assert "https://img.example/a.png" in text and "none.gif" not in text and "smile" not in text
    assert "[hidden: 本帖隐藏的内容需要回复才可以浏览]" in text


def test_post_body_drops_the_forums_own_decoration_images():
    msg = ('上<img src="https://static.52pojie.cn/static/image/hrline/1.gif" border="0" alt="" />下'
           '<ignore_js_op><img aid="2" src="https://static.52pojie.cn/static/image/common/none.gif" '
           'zoomfile="https://attach.52pojie.cn/forum/a.png" file="https://attach.52pojie.cn/forum/a.png" /></ignore_js_op>')
    text, _ = parse.message_text(msg)
    assert "hrline" not in text and "https://attach.52pojie.cn/forum/a.png" in text and text.startswith("上下")


# --- 吾爱破解 and Substack against raw pages: lifted posts, embeds, images, list ends and edits ----------

def block(pid: int, label: str, body: str, extra: str = "") -> str:
    return (f'<div id="post_{pid}"><div class="authi"><a>user{pid}</a></div>'
            f'<a href="x" id="postnum{pid}">{label}</a>'
            f'<td class="t_f" id="postmessage_{pid}">{body}</td>{extra}')


def test_discuz_keeps_lifted_posts_apart_from_the_floors():
    html = block(1, "楼主", "正文") + block(2, " 推荐 ", "踩坑反馈") + block(3, "<em>8</em><sup>#</sup>", "八楼")
    page = dz.parse_thread_page(html)
    assert [p.number for p in page.posts] == [1, 8]
    assert [(p.pid, p.text) for p in page.featured] == [(2, "踩坑反馈")]


def test_discuz_attachment_image_keeps_its_url():
    msg = ('<ignore_js_op><img aid="1" src="none.gif" zoomfile="https://attach.invalid/a.png" '
           'file="https://attach.invalid/a.png"><strong>a.png</strong></ignore_js_op>')
    assert dz.message_text(msg)[0] == "[image: a.png https://attach.invalid/a.png]"


def test_discuz_empty_placeholder_is_not_an_attachment():
    assert dz.message_text('<ignore_js_op><span id="flv_p4l"></span></ignore_js_op>文字')[0] == "文字"


def test_discuz_keeps_the_edit_note():
    html = block(1, "楼主", '<i class="pstatus"> 本帖最后由 正己 于 2024-9-26 18:59 编辑 </i>正文')
    [post] = dz.parse_posts(html)
    assert post.edited == "2024-09-26 18:59 by 正己" and post.text == "正文"
    assert dz.to_posts([post], None)[0].edited == post.edited


# --- the edit note and numbered smileys (dogfooding, on 52pojie's markup) ---------------------------

def dz_post(stamp: str, author: str = "wendi_love") -> str:
    return (f'<div id="post_1"><a id="postnum1"><em>2</em></a><div class="authi"><a href="space-uid-1.html">{author}</a>'
            f'</div><td class="t_f" id="postmessage_1"><i class="pstatus"> 本帖最后由 {stamp} 编辑 </i>Text'
            '<img src="static/image/smiley/default/24.gif" smilieid="24" border="0" alt="" /></td></div>')


def test_discuz_edit_note_reads_like_other_sites():
    [post] = dz.parse_posts(dz_post("wendi_love 于 2026-11-4 02:47"))
    assert post.edited == "2026-11-04 02:47"


def test_discuz_edit_note_names_an_editor_who_is_not_the_author():
    [post] = dz.parse_posts(dz_post("moderator 于 2026-11-4 02:47"))
    assert post.edited == "2026-11-04 02:47 by moderator"


def test_discuz_numbered_smileys_are_dropped():
    [post] = dz.parse_posts(dz_post("wendi_love 于 2026-11-4 02:47"))
    assert post.text == "Text"
