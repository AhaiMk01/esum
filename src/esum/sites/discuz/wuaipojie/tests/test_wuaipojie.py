"""吾爱破解 (wuaipojie, on the Discuz! engine): its actions on 52pojie's scrubbed pages. The engine's
parser tests read most of them, so those are the engine's fixtures (the checkout's
tests/fixtures/discuz/, see the README there); the one page only this site's tests read is in
tests/fixtures/ beside this file. The parser tests are tests/test_parsers_discuz.py."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

import pytest

from esum import Transport
from esum.core.registry import Registry
from esum.transport import Response

from support import engine_fixtures, find_fixture

FIX = Path(__file__).parent / "fixtures"
BASE = "https://www.52pojie.cn"


def fixture(name: str) -> str:
    return find_fixture(name, FIX, engine_fixtures("discuz")).read_text(encoding="utf-8")


def page(url: str, name: str | None = None, text: str | None = None) -> Response:
    """A 52pojie response: GBK, as the site serves it."""
    body = (text if text is not None else fixture(name)).encode("gbk")
    return Response(200, url, {"content-type": "text/html; charset=gbk"}, body)


def forum_php(**params) -> str:
    return f"{BASE}/forum.php" + (f"?{urlencode(params)}" if params else "")


# --- whole actions ------------------------------------------------------------------------------------


@pytest.fixture
def cli(capsys, monkeypatch, action_runner):
    monkeypatch.setenv("ESUM_SPILL_CHARS", "10000000")
    registry = Registry.discover(custom_root=None)

    def _run(*argv: str):
        code = action_runner.cli(argv, registry=registry)
        out = capsys.readouterr()
        return code, out.out, out.err

    return _run


def test_site_config():
    s = Registry.discover(custom_root=None).sites["wuaipojie"]
    assert s.transport is Transport.BROWSER_HTTP and s.charset == "gbk" and s.extras["min_interval"] == 1.0


def test_forums_action_filters(fake_http, cli):
    fake_http.routes[forum_php()] = page(forum_php(), "index.html")
    code, out, err = cli("wuaipojie", "forums", "--query", "脱壳")
    assert code == 0, err
    lines = out.splitlines()
    assert "query: 脱壳" in lines and "forums[2]{id,name,group}:" in lines
    assert "  5,『脱壳破解区』,【 软件安全 】" in lines
    assert "  Run `esum wuaipojie forum --id <id>` to list a board's threads" in lines
    code, out, err = cli("wuaipojie", "forums", "--query", "脱壳", "--fields", "threads,posts,today,parent")
    assert code == 0, err
    assert "  5,『脱壳破解区』,【 软件安全 】,43080,911794,35,null" in out
    assert "  75,『脱壳破解讨论求助区』,【 软件安全 】,null,null,0,5" in out
    code, out, err = cli("wuaipojie", "forums", "--query", "zzz")
    assert code == 0 and "0 found: no board name or group contains 'zzz'" in out


def test_forum_by_name_with_order_and_page(fake_http, cli):
    fake_http.routes[forum_php()] = page(forum_php(), "index.html")
    url = forum_php(mod="forumdisplay", fid=24, page=2, filter="author", orderby="dateline")
    fake_http.routes[url] = page(url, "forum.html")
    code, out, err = cli("wuaipojie", "forum", "--id", "编程语言", "--order", "dateline", "--page", "2")
    assert code == 0, err   # 编程语言 matches boards 24 and 74; the top-level board wins
    lines = out.splitlines()
    assert {"id: 24", "name: 『编程语言区』", "order: dateline", "page: 1", "pages: 428", "count: 5 of 21389 total",
            "threads[5]{id,title,replies,last_post}:"} <= set(lines)
    assert "  Run `esum wuaipojie forum --id '编程语言' --page 2 --order dateline` for the next page" in lines
    code, out, err = cli("wuaipojie", "forum", "--id", "编程语言", "--order", "dateline", "--page", "2",
                         "--fields", "sticky,closed,type,starter,started,views,pages,last_user")
    assert code == 0, err
    assert "  1691883,【其他转载】" not in out and ",true,true,其他转载,Hmily," in out
    assert ('  2131070,' in out and ',41,"2026-10-27 14:30",false,false,其他转载,麦米尔加弗德,"2026-10-05 23:30",'
            '1395,5,Pandacute') in out


def test_forum_notice_passes_through_raw(fake_http, cli):
    url = forum_php(mod="forumdisplay", fid=99999)
    fake_http.routes[url] = page(url, "notice_noboard.html")
    code, err, _ = cli("wuaipojie", "forum", "--id", "99999")
    assert code == 1 and "指定的版块不存在" in err


def test_latest_hot(fake_http, cli):
    url = forum_php(mod="guide", view="hot")
    fake_http.routes[url] = page(url, "guide_hot.html")
    code, out, err = cli("wuaipojie", "latest", "--view", "hot")
    assert code == 0, err
    lines = out.splitlines()
    assert {"view: hot", "page: 1", "pages: 2", "count: 3", "threads[3]{id,title,forum,replies}:"} <= set(lines)
    assert any(line.startswith("  2134196,") and line.endswith(",『精品软件区』,180") for line in lines)
    assert "  Run `esum wuaipojie latest --view hot --page 2` for the next page" in lines


def test_thread_as_guest_reads_page_one_and_notes_members_only_pages(fake_http, cli):
    fake_http.routes[forum_php(mod="viewthread", tid=2134210)] = page(forum_php(), "thread_p1.html")
    fake_http.routes[forum_php(mod="viewthread", tid=2134210, page=2)] = page(forum_php(), "thread_login.html")
    code, out, err = cli("wuaipojie", "thread", "--id", f"{BASE}/thread-2134210-1-1.html")
    assert code == 0, err
    assert "# [C&C++ 转载] 库存管理" in out
    assert "Board: 『编程语言区』 (24) · 706 views · about 18 floors in 2 page(s)" in out
    assert "Recommended posts (the site lifts these above the thread" in out
    assert "Page(s) 2 not read: the site shows them to members only." in out
    assert ("[再贱就再见 #1] (2026-10-26 09:25 · 👍2 · OP · edited 2026-10-26 14:34): "
            "转载个电脑+手机配合的进销存软件") in out
    assert "[再贱就再见 #3] (2026-10-26 10:34 · OP · ↩#2): > @dupengpeng:" in out
    assert "[... omitted 3 post numbers (#4–#6), deleted, or among the recommended posts below …]" in out
    assert "[... omitted 8 post numbers (#11–#18), members only …]" in out
    assert len(fake_http.requests) == 2


def test_thread_range_on_a_members_only_page_is_login_needed(fake_http, cli):
    fake_http.routes[forum_php(mod="viewthread", tid=2134210)] = page(forum_php(), "thread_p1.html")
    fake_http.routes[forum_php(mod="viewthread", tid=2134210, page=2)] = page(forum_php(), "thread_login.html")
    code, err, _ = cli("wuaipojie", "thread", "--id", "2134210", "--from", "12")
    assert code == 3 and "LOGIN NEEDED" in err and "page(s) 2 to members only" in err


def test_thread_as_member_reads_later_pages(fake_http, cli):
    fake_http.routes[forum_php(mod="viewthread", tid=2134210)] = page(forum_php(), "thread_p1.html")
    fake_http.routes[forum_php(mod="viewthread", tid=2134210, page=2)] = page(forum_php(), "thread_p2_synthetic.html")
    code, out, err = cli("wuaipojie", "thread", "--id", "2134210", "--json")
    assert code == 0, err
    import json
    data = json.loads(out)["data"]
    assert data["members_only_pages"] == [] and (data["pages"], data["per_page"], data["total"]) == (2, 10, 18)
    posts = {p["number"]: p for p in data["posts"] if "number" in p}
    assert posts[11]["reply_to"] == 7 and posts[18]["is_op"] and not posts[11]["is_op"]
    assert posts[11]["url"] == f"{BASE}/forum.php?mod=redirect&goto=findpost&ptid=2134210&pid=56070001"


def test_thread_page_url_reads_that_page(fake_http, cli):
    fake_http.routes[forum_php(mod="viewthread", tid=2134210)] = page(forum_php(), "thread_p1.html")
    fake_http.routes[forum_php(mod="viewthread", tid=2134210, page=2)] = page(forum_php(), "thread_p2_synthetic.html")
    code, out, _ = cli("wuaipojie", "thread", "--id", f"{BASE}/thread-2134210-2-1.html")
    assert code == 0, out
    assert "[... omitted 10 post numbers (#1–#10), not read …]" in out and "[Synthetic Reader #11]" in out
    assert "Recommended posts" not in out   # they sit on page 1
    assert "#3]" not in out
    code, err, _ = cli("wuaipojie", "thread", "--id", f"{BASE}/thread-2134210-5-1.html")
    assert code == 2 and "has no page 5" in err


def test_thread_page_one_login_is_login_needed(fake_http, cli):
    fake_http.routes[forum_php(mod="viewthread", tid=1)] = page(forum_php(), "thread_login.html")
    code, err, _ = cli("wuaipojie", "thread", "--id", "1")
    assert code == 3 and "LOGIN NEEDED" in err and "您需要先登录" in err


def test_one_login_wall_stops_reading_later_pages(fake_http, cli):
    five = fixture("thread_p1.html").replace("共 2 页", "共 5 页").replace(" / 2 页", " / 5 页")
    fake_http.routes[forum_php(mod="viewthread", tid=2134210)] = page(forum_php(), text=five)
    fake_http.routes[forum_php(mod="viewthread", tid=2134210, page=2)] = page(forum_php(), "thread_login.html")
    code, out, err = cli("wuaipojie", "thread", "--id", "2134210")
    assert code == 0, err
    assert len(fake_http.requests) == 2 and "Page(s) 2–5 not read" in out
    assert "41 floors in 5 page(s)" in out and "[... omitted 31 post numbers (#11–#41), members only …]" in out


@pytest.mark.parametrize("as_json", [False, True])
def test_thread_repeated_floor_across_pages_keeps_first_copy(fake_http, cli, as_json):
    first = fixture("thread_p1.html").replace("共 2 页", "共 3 页").replace(" / 2 页", " / 3 页")

    def posts(*entries):
        return "".join(
            f'<div id="post_{pid}"><div class="authi"><a>reader</a></div>'
            f'<a id="postnum{pid}"><em>{floor}</em><sup>#</sup></a>'
            f'<td class="t_f" id="postmessage_{pid}">{text}</td></div>'
            for pid, floor, text in entries)

    routes = {
        forum_php(mod="viewthread", tid=2134210): first,
        forum_php(mod="viewthread", tid=2134210, page=2): posts((100020, 20, "page two copy")),
        forum_php(mod="viewthread", tid=2134210, page=3): posts(
            (100020, 20, "page three copy"), (100021, 21, "last floor")),
    }
    fake_http.routes.update({url: page(url, text=html) for url, html in routes.items()})
    code, out, err = cli("wuaipojie", "thread", "--id", "2134210", "--from", "20", "--to", "21",
                         *(["--json"] if as_json else []))
    assert code == 0, err
    assert [r.url for r in fake_http.requests] == list(routes)
    assert "page two copy" in out and "page three copy" not in out
    if as_json:
        import json
        shown = [p for p in json.loads(out)["data"]["posts"] if "number" in p]
        assert [p["number"] for p in shown] == [20, 21]
        assert shown[0]["text"] == "page two copy"
    else:
        assert out.count("[reader #20]") == 1
        assert "[reader #21]" in out


def test_wuaipojie_has_no_search_and_says_so_without_a_request(fake_http, cli):
    code, out, _ = cli("wuaipojie", "--help")
    assert code == 0 and any(line.startswith("  search,read,\"Not available on 52pojie") for line in out.splitlines())
    code, out, _ = cli("wuaipojie", "search", "--query", "脱壳")
    assert code == 1 and "`site:52pojie.cn`" in out and fake_http.requests == []
    assert "help: Find threads with `esum wuaipojie forum --id <id> --order" in out


def test_forum_type_filter(fake_http, cli):
    url = forum_php(mod="forumdisplay", fid=24, filter="typeid", typeid=33)
    fake_http.routes[url] = page(url, "forum.html")
    code, out, err = cli("wuaipojie", "forum", "--id", "24", "--type", "33")
    assert code == 0, err
    assert "type: 33" in out and "count: 5\n" in out + "\n"   # a filtered list has no board-wide total
    code, out, _ = cli("wuaipojie", "forum", "--id", "24", "--type", "33", "--digest")
    assert code == 2 and "not both" in out


def test_missing_thread_as_404_notice_is_no_such_item(fake_http, cli):
    url = forum_php(mod="viewthread", tid=99999999)
    body = '<div id="messagetext" class="alert_error"><p>抱歉，指定的主题不存在或已被删除或正在被审核</p></div>'
    fake_http.routes[url] = Response(404, url, {"content-type": "text/html; charset=gbk"}, body.encode("gbk"))
    code, out, _ = cli("wuaipojie", "thread", "--id", "99999999")
    assert code == 1 and "指定的主题不存在" in out and "check the id or name" in out
