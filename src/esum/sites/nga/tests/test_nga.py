"""Offline tests for the NGA module on saved (trimmed, account-scrubbed) JSON pages."""

from __future__ import annotations

from esum import ArgumentError

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from esum import Transport
from esum.core.posts import compact_text
from esum.core.registry import Registry
from esum.sites.nga import parse
from esum.transport import Response
from esum.sites.nga import parse as nga

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


# --- parsers -----------------------------------------------------------------------------------------

def test_forum_directory():
    found = parse.forums(load("category.json"))
    by_fid = {f["fid"]: f for f in found if not f["stid"]}
    assert by_fid[-7]["name"] == "网事杂谈" and by_fid[-7]["category"] == "网事杂谈"
    assert by_fid[7]["info"] == "魔兽主讨论区" and by_fid[7]["url"] == "https://bbs.nga.cn/thread.php?fid=7"
    collection = next(f for f in found if f["stid"])
    assert collection["url"].endswith(f"stid={collection['stid']}")
    assert [f["fid"] for f in parse.match_forums(found, "魔兽主")] == [7]
    assert parse.match_forums(found, "魔兽主")[0]["category"] == "魔兽世界"


def test_topic_page_rows():
    data = parse.topic_page(load("list_fid-7.json"))
    assert data["forum"] == "网事杂谈" and data["fid"] == -7 and data["per_page"] == 35 and data["total"] > 10**6
    denied, *rest = data["threads"]
    assert denied["denied"] == "帖子发布或回复时间超过限制"
    assert denied["title"] == "(hidden from this account: 帖子发布或回复时间超过限制)"
    sub = next(t for t in rest if t["forum"])
    assert sub["fid"] != -7 and sub["author"] and sub["url"] == f"https://bbs.nga.cn/read.php?tid={sub['id']}"


def test_search_and_hot_rows():
    data = parse.topic_page(load("search.json"))
    assert data["total"] == 80522 and data["forum"] is None
    first = data["threads"][0]
    assert first["id"] == 47797564 and first["title"] == "目前显卡涨的这么猛，还建议配电脑吗" and first["fid"] == 334
    assert (first["replies"], first["author"]) == (13, "nukumizu")
    hot = parse.hot_topics(load("hot_fid-7.json"))
    assert hot[0]["id"] == 47790179 and hot[0]["replies"] == 194 and hot[0]["author"] == "Mmimm117"
    assert hot[0]["date"] == "2026-10-26T14:18:37+00:00"
    as_object = load("hot_fid-7.json")
    as_object["data"] = {"0": as_object["data"][0]}
    assert parse.hot_topics(as_object) == hot


def test_thread_page_floors_and_names():
    p = parse.thread_page(load("read_47790179_p1.json"))
    assert p.tid == 47790179 and p.fid == -7 and p.forum == "网事杂谈" and p.total == 195 and p.page == 1
    assert p.title == "笑死了 起号女误入二次元了" and p.author == "Mmimm117"
    op = p.floors[0]
    assert op.floor == 0 and op.pid == 0 and op.likes == 60 and op.user == "UID:67361017"
    assert op.date.isoformat() == "2026-10-26T14:18:37+00:00"   # from the topic: the OP row has no timestamp
    assert "[image: https://img.nga.cn/attachments/mon_202610/26/-7Q89-d3u6K1yT1kShs-13i.jpg]" in op.body.text
    assert p.names[67361017] == "Mmimm117"                     # the topic row's real name
    assert parse.thread_page(load("read_47790179_p3.json")).names[66341686] == "向日葵哒哒哒"  # a quote header's


def test_quotes_become_one_line():
    body = parse.post_body("[quote][pid=885844351,47790179,2]Reply[/pid] [b]Post by [uid=66341686]向日葵哒哒哒[/uid] "
                           "(2026-10-26 23:01):[/b]<br/>别他么啥都扣二次元。[/quote]<br/>这就是现代的二次元[s:ac:偷笑]")
    assert body.reply.pid == 885844351 and body.reply.page == 2 and body.reply.user == "向日葵哒哒哒"
    assert compact_text(parse.render_body(body, {})) == "> @向日葵哒哒哒 (page 2: #20–#39): 别他么啥都扣二次元。\n这就是现代的二次元:偷笑:"
    assert compact_text(parse.render_body(body, {885844351: 25})).startswith("> @向日葵哒哒哒 #25: 别他么")


def test_quote_of_the_topic_cut_mid_image():
    body = parse.post_body("[quote][tid=47790179]Topic[/tid] [b]Post by [uid=67361017]Mmimm117[/uid] (2026-10-26 "
                           "22:18):[/b]<br/><br/>[img]./mon_1.jpg[/img]看图[img]./mon_202610/26/-7Q89-9fa[/quote]<br/>跟黑话一样")
    assert body.reply.pid == 0
    assert compact_text(parse.render_body(body, {0: 0})) == "> @Mmimm117 #0: 看图\n跟黑话一样"


def test_nested_quotes_keep_one_level():
    inner = "[quote][pid=2,9,1]Reply[/pid] [b]Post by [uid=5]a[/uid] (x):[/b]内层[/quote]"
    body = parse.post_body(f"[quote][pid=3,9,1]Reply[/pid] [b]Post by [uid=6]b[/uid] (x):[/b]{inner}外层[/quote]正文")
    assert body.reply.pid == 3
    assert compact_text(parse.render_body(body, {2: 2, 3: 3})) == "> @b #3: 外层\n正文"


def test_reply_header():
    body = parse.post_body("[b]Reply to [pid=885975865,47790179,9]Reply[/pid] Post by [uid=38727987]跳脚尖椒[/uid] "
                           "(2026-10-27 23:40)[/b]<br/>你得考虑")
    assert compact_text(parse.render_body(body, {})) == "↩ @跳脚尖椒 (page 9: #160–#179)\n你得考虑"
    assert compact_text(parse.render_body(body, {885975865: 170})) == "你得考虑"   # ↩#170 goes in the post line


def test_replies_to_the_opening_post():
    for head in ("[tid=47790179]Topic[/tid]", "[pid=0,47790179,1]Reply[/pid]"):
        body = parse.post_body(f"[b]Reply to {head} Post by [uid=67361017]Mmimm117[/uid] (2026-10-26 22:18)[/b]<br/>同意")
        assert body.reply.pid == 0 and body.reply.user == "Mmimm117" and body.text.strip() == "同意"
        floor = parse.Floor(floor=5, pid=9, uid=1, user="UID:1", date=None, likes=0, body=body)
        assert parse.to_post(floor, 47790179, 2, {}, {0: 0}).reply_to == 0


def test_headerless_and_anonymous_quotes():
    body = parse.post_body("[quote]没有作者[/quote]正文")
    assert compact_text(parse.render_body(body, {})) == "> 没有作者\n正文"
    body = parse.post_body("[quote][pid=7,9,2]Reply[/pid] [b]Post by [uid=-1]#anony_3f2a[/uid] (x):[/b]匿名说[/quote]"
                           "[uid=-1]#anony_3f2a[/uid] 好")
    assert compact_text(parse.render_body(body, {})) == "> @anonymous#3f2a (page 2: #20–#39): 匿名说\n@anonymous#3f2a 好"
    assert parse.user_name("#anony_9c1b77") == "anonymous#9c1b" and parse.user_name("#anony") == "anonymous"


def test_markup():
    text = parse.post_body("[url=https://a.example/x]链接[/url] [url]https://b.example[/url] [uid=1]某人[/uid]<br/>"
                           "[collapse=剧透]内容[/collapse][del]删[/del] &amp;#128020; [list][*]一[*]二[/list]").text
    assert "链接 (https://a.example/x)" in text and "https://b.example" in text and "@某人" in text
    assert "剧透: 内容" in text and "删" in text and "🐔" in text and "- 一" in text and "- 二" in text


def test_refs_and_urls():
    assert parse.thread_ref("47790179") == (47790179, None)
    assert parse.thread_ref("https://ngabbs.com/read.php?tid=47790179&page=3") == (47790179, None)
    assert parse.thread_ref("nga.178.com/read.php?pid=885980179") == (None, 885980179)
    assert parse.thread_ref("pid:885980179") == (None, 885980179)
    with pytest.raises(ArgumentError):
        parse.thread_ref("https://example.com/read.php?tid=1")
    assert parse.floor_url(47790179, 45) == "https://bbs.nga.cn/read.php?tid=47790179&page=3#l45"
    assert parse.error_of(load("error_not_found.json")) == "找不到主题"


# --- whole actions on a fake backend standing in for the T2 transport -------------------------------

GUEST = json.dumps({"error": ["15:访客不能直接访问", "<br/><script>…</script>"]}).encode()
NO_REPUTATION = json.dumps({"error": ["2048:注册用户/威望大于0方可使用搜索"]}).encode()
NO_BOARD = json.dumps({"error": ["2048:版面ID99999999不存在"]}).encode()
NO_POST = json.dumps({"error": ["2048:未指定帖子"]}).encode()


class PathBackend:
    def __init__(self):
        self.requests = []
        self.guest = False

    def send(self, request, *, before_send=lambda: None):
        before_send()
        self.requests.append(request)
        parts = urlsplit(request.url)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        name, status = None, 200
        if parts.path == "/app_api.php":
            name = {"category": "category.json", "hot": "hot_fid-7.json"}.get(q.get("__act"))
        elif parts.path == "/thread.php":
            if q.get("key") == "威望":
                return Response(403, request.url, {"content-type": "text/json;charset=UTF-8"}, NO_REPUTATION)
            if self.guest:
                return Response(403, request.url, {"content-type": "text/json;charset=UTF-8"}, GUEST)
            name = "search.json" if "key" in q else "list_fid-7.json" if q.get("fid") == "-7" else None
            if q.get("fid") == "99999999":
                return Response(200, request.url, {"content-type": "text/json;charset=UTF-8"}, NO_BOARD)
            if q.get("fid") == "-8":
                return Response(403, request.url, {"content-type": "text/json;charset=UTF-8"}, GUEST)
        elif parts.path == "/read.php":
            if q.get("pid") == "9999999999":
                return Response(200, request.url, {"content-type": "text/json;charset=UTF-8"}, NO_POST)
            if q.get("tid") == "47790179" or q.get("pid") == "885980179":
                name = {"1": "read_47790179_p1.json", "3": "read_47790179_p3.json",
                        "10": "read_47790179_p10.json"}.get(q.get("page", "10" if "pid" in q else "1"))
            else:
                name = "error_not_found.json"
        if name is None:
            return Response(404, request.url, {"content-type": "text/plain"}, b"Not Found")
        return Response(status, request.url, {"content-type": "text/json;charset=UTF-8"}, (FIX / name).read_bytes())

    def close(self):
        pass


@pytest.fixture
def run(action_runner, capsys):
    backend = PathBackend()
    action_runner.backend = backend
    reg = Registry.discover(custom_root=None)

    def _run(*argv):
        code = action_runner.cli(["nga", *argv], registry=reg)
        captured = capsys.readouterr()
        return code, captured.out + captured.err, backend

    return _run


def test_site_declares_browser_transport_with_cookies():
    s = Registry.discover(custom_root=None).sites["nga"]
    assert s.transport is Transport.BROWSER_HTTP and s.extras.get("browser_cookies", True) and s.fallback is None


def test_forums_action(run):
    code, out, backend = run("forums", "--query", "魔兽主")
    lines = out.splitlines()
    assert code == 0 and "count: 1 of 17 total" in lines and "forums[1]{fid,stid,name,group}:" in lines
    assert "  7,null,艾泽拉斯议事厅,艾泽拉斯议事厅" in lines
    assert "  Run `esum nga listing --fid <fid>` to list a board's topics (or `--stid <stid>` for a collection)" in lines
    assert parse_qs(urlsplit(backend.requests[0].url).query)["__output"] == ["11"]
    code, out, _ = run("forums", "--query", "zzz")
    assert code == 0 and "forums: \"0 found: no board matches 'zzz'\"" in out


def test_listing_action(run):
    code, out, backend = run("listing", "--fid", "-7", "--order", "newest", "--recommend")
    lines = out.splitlines()
    assert code == 0 and "board: 网事杂谈" in lines and "featured: yes" in lines and "sort: newest" in lines
    assert "count: 6 of 10083248 total" in lines and "threads[6]{id,title,forum,replies,date}:" in lines
    assert '  44191387,"(hidden from this account: 帖子发布或回复时间超过限制)",null,0,null' in lines
    assert any(line.startswith("  47795676,") and ",国际新闻,9," in line for line in lines)
    assert "  Run `esum nga listing --fid=-7 --page 2 --order newest --recommend` for the next page" in lines
    assert "  Run `esum nga thread --id <id>` to read a topic" in lines
    code, out, _ = run("listing", "--fid", "-7", "--json")
    data = json.loads(out)["data"]
    assert data["page"] == 1 and data["pages"] == 288093 and data["forum"] == "网事杂谈" and len(data["threads"]) == 6
    q = parse_qs(urlsplit(backend.requests[0].url).query)
    assert q["order_by"] == ["postdatedesc"] and q["recommend"] == ["1"] and q["fid"] == ["-7"]
    code, out, _ = run("listing")
    assert code == 2 and "pass one of --fid" in out


def test_guest_needs_login(run):
    code, out, _ = run("listing", "--fid", "-8")
    assert code == 3 and "LOGIN NEEDED" in out and "访客不能直接访问" in out


def test_search_action(run):
    code, out, backend = run("search", "--query", "显卡", "--fid=-7,7")
    lines = out.splitlines()
    assert code == 0 and "query: 显卡" in lines and 'fid: "-7,7"' in lines and "count: 4 of 80522 total" in lines
    assert "threads[4]{id,title,fid,replies,date}:" in lines and "page: 1 of 2301" in lines
    q = parse_qs(urlsplit(backend.requests[0].url).query)
    assert q["key"] == ["显卡"] and q["fid"] == ["-7,7"] and q["__inchst"] == ["UTF8"]
    code, out, _ = run("search", "--query", "x", "--content")
    assert code == 2 and "--content needs --fid" in out
    code, out, _ = run("search", "--query", "x", "--fid", "abc")
    assert code == 2 and "not a board id" in out


def test_search_without_reputation(run):
    code, out, backend = run("search", "--query", "威望")    # a member with 威望 0: the site's message
    assert code == 1 and "注册用户/威望大于0方可使用搜索" in out and "LOGIN NEEDED" not in out
    backend.guest = True                                      # a guest: log in
    code, out, _ = run("search", "--query", "威望")
    assert code == 3 and "LOGIN NEEDED" in out


def test_hot_action(run):
    code, out, backend = run("hot", "--fid", "-7", "--period", "week")
    lines = out.splitlines()
    assert code == 0 and "period: week" in lines and "count: 4" in lines and "threads[4]{id,title,replies,date}:" in lines
    assert '  47790179,笑死了 起号女误入二次元了,194,"2026-10-26 14:18"' in lines
    assert parse_qs(urlsplit(backend.requests[0].url).query)["days"] == ["7"]


def test_thread_action_fetches_only_needed_pages(run):
    code, out, backend = run("thread", "--id", "47790179", "--from", "40", "--to", "46")
    assert code == 0 and "194 replies in 10 page(s) of 20 · OP Mmimm117" in out
    assert "> @米想Orion #11: 这不是二次元吧" in out                   # floor 11 is on page 1, fetched anyway
    assert "[... omitted 39 post numbers (#1–#39), not read …]" in out and "#0]" not in out
    pages = [parse_qs(urlsplit(r.url).query).get("page") for r in backend.requests]
    assert pages == [["1"], ["10"], ["3"]]


def test_thread_action_links_quotes_to_fetched_floors(run):
    code, out, _ = run("thread", "--id", "https://ngabbs.com/read.php?tid=47790179", "--to", "19", "--from", "0")
    assert code == 0 and "[Mmimm117 #0] (2026-10-26 14:18 · 👍60 · OP):" in out


def test_thread_by_pid_and_missing_topic(run):
    code, out, backend = run("thread", "--id", "pid:885980179", "--from", "186", "--to", "186")
    assert code == 0 and "[UID:64223435 #186] (2026-10-27 16:45 · ↩#9): > @jiayongbei #9: 这是智取威虎山对暗号吗" in out
    code, out, _ = run("thread", "--id", "123")
    assert code == 1 and "nga topic 123: 找不到主题" in out
    assert "check the id or name" in out and "may have been removed" in out


def test_missing_board_and_post_are_not_found(run):
    code, out, _ = run("listing", "--fid", "99999999")
    assert code == 1 and "error: nga board 99999999: 版面ID99999999不存在" in out
    assert "help: No such board or collection: `esum nga forums --query <name>` lists their fid and stid" in out
    code, out, _ = run("thread", "--id", "pid:9999999999")
    assert code == 1 and "error: nga post 9999999999: 未指定帖子" in out
    assert "The site has no such item" in out and "refused" not in out

@pytest.mark.parametrize('as_json', [False, True])
def test_thread_range_past_total_keeps_only_mandatory_probes(run, as_json):
    code, out, backend = run('thread', '--id', '47790179', '--from', '999', '--to', '1000',
                             *(['--json'] if as_json else []))
    assert code == 2
    assert 'ends at #194' in out
    assert 'esum nga thread --id 47790179 --from 165 --to 194' in out
    pages = [parse_qs(urlsplit(r.url).query)['page'][0] for r in backend.requests]
    assert pages == ['1', '10']


# --- What the page says must survive: code, struck text, images, attachments, and honest gaps --------

def test_nga_keeps_sticker_names_struck_text_and_topic_links():
    assert nga.post_body("好算数[s:ac:呆]").text == "好算数:呆:"
    assert nga.post_body("原价100，[del]现在50[/del]现在80").text == "原价100，~~现在50~~现在80"
    assert nga.post_body("详见[tid=123456]这个帖子[/tid]").text == "详见这个帖子 (tid 123456)"
