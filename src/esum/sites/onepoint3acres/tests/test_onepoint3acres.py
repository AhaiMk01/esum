"""一亩三分地 (onepoint3acres) on scrubbed API answers in tests/fixtures/ beside this file."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from esum import ArgumentError
from esum.core.registry import Registry
from esum.sites.onepoint3acres import parse
from esum.transport import Response, form_encode

FIX = Path(__file__).parent / "fixtures"
API = "https://api.1point3acres.com"


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def answer(url: str, name: str, status: int = 200) -> Response:
    return Response(status, url, {"content-type": "application/json"}, (FIX / name).read_bytes())


def api(path: str, **params) -> str:
    query = form_encode({k: v for k, v in params.items() if v is not None}, None)
    return f"{API}{path}" + (f"?{query}" if query else "")


def replies(tid: int, page: int) -> str:
    return api(f"/api/threads/{tid}/nested-posts", pg=page, ps=20, order="time_asc", sub_limit=50, with_total=1)


@pytest.fixture
def cli(capsys, monkeypatch, action_runner):
    monkeypatch.setenv("ESUM_SPILL_CHARS", "10000000")
    registry = Registry.discover(custom_root=None)

    def _run(*argv: str):
        code = action_runner.cli(argv, registry=registry)
        return code, capsys.readouterr().out

    return _run


# --- pure parsing ------------------------------------------------------------------------------------

def test_thread_refs():
    assert parse.thread_ref("1196058") == 1196058
    assert parse.thread_ref("https://www.1point3acres.com/bbs/thread-1196058-1-1.html") == 1196058
    assert parse.thread_ref("https://www.1point3acres.com/home/thread/1196058") == 1196058
    assert parse.thread_ref("https://www.1point3acres.com/bbs/forum.php?mod=viewthread&tid=1196058&page=2") == 1196058
    with pytest.raises(ArgumentError):
        parse.thread_ref("https://www.1point3acres.com/bbs/forum-145-1.html")


def test_link_wrappers_are_unwrapped():
    assert parse.unwrap("https://link.1p3a.com/?url=https%3A%2F%2Fx.com%2Fa%2Fstatus%2F1") == "https://x.com/a/status/1"
    camo = "https://link.1p3a.com/camo/beef/" + "https://i.example/p.png".encode().hex()
    assert parse.unwrap(camo) == "https://i.example/p.png"
    assert parse.unwrap("https://link.1p3a.com/camo/beef/" + "data:image/png;base64,AAAA".encode().hex()) is None
    assert parse.unwrap("forum.php?mod=redirect&pid=1") == "https://www.1point3acres.com/bbs/forum.php?mod=redirect&pid=1"


def test_bbcode_body():
    raw = ("[b]内推流程[/b] for [L5] <role>\n[url=https://link.1p3a.com/?url=https%3A%2F%2Fjobs.example%2F]官网[/url]\n"
           "[quote][size=2][url=forum.php?mod=redirect&goto=findpost&pid=9][color=#999999]alice 发表于 2026-11-6 09:00"
           "[/color][/url][/size]\n原来的话[/quote]回复\n[code]x = 1\n  y = 2[/code]\n[list][*]一[*]二[/list]"
           "[color=red][size=5]红[/size][/color][attach]7[/attach][img]https://i.example/a.png[/img]")
    text = parse.html_to_body(parse.to_html(raw, {7: "https://oss.example/7.jpeg"}))
    assert text.startswith("内推流程 for [L5] <role>")   # a literal [L5] stays; no HTML is injected
    assert "官网" in text and "https://jobs.example/" in text and "link.1p3a.com" not in text
    assert "> @alice: 原来的话" in text and "\n回复" in text
    assert "x = 1\n  y = 2" in text and "红" in text and "[size" not in text
    assert "https://oss.example/7.jpeg" in text and "https://i.example/a.png" in text


def test_bbcode_inside_code_stays_literal():
    src = ('[b]literal[/b] print("[img]https://example.com/a.png[/img]")\n'
           "[url=https://a.example]x[/url] [quote]q[/quote] [hide=200]h[/hide] [color=red]c[/color] a<b && c")
    text = parse.body_text({"message_bbcode": f"[b]bold[/b]\n[code]{src}[/code]"})
    assert text.startswith("bold")
    assert src in text
    quoted = parse.body_text({"message_bbcode": "[quote]see [code][b]x[/b] <a>[/code][/quote]"})
    assert "> [b]x[/b] <a>" in quoted


def test_points_lock():
    t = load("locked.json")["thread"]
    assert parse.locked_points(t) == 200
    assert parse.body_text(t) == "[hidden: needs more than 200 points (积分)]"
    assert parse.locked_points(load("thread.json")["thread"]) is None


def test_attachment_image_and_outbound_link():
    text = parse.body_text(load("attach.json")["thread"])
    assert "https://oss.1p3a.com/forum/202611/08/13932vx1gusf5atoeskgs.jpeg" in text
    assert "https://x.com/whyyoutouzhele/status/2118971715598598472" in text and "link.1p3a.com" not in text


def test_forum_rows_follow_the_tree():
    rows = parse.forum_rows(load("forums.json")["forums"])
    by_id = {r["id"]: r for r in rows}
    assert [r["id"] for r in rows if r["group"] == "海外求职"] == [28, 145, 335]   # the sub-board after its parent
    assert by_id[335]["parent"] == 145 and by_id[145]["parent"] is None
    assert "532 选组选Offer" in by_id[28]["types"] and by_id[79]["group"] == "留学申请"
    assert parse.split_types(by_id[28]["types"])[532] == "选组选Offer"


def test_thread_rows():
    first = parse.thread_row(load("hot.json")["threads"][0])
    assert first["id"] and first["forum"] and isinstance(first["date"], datetime)
    assert first["date"].tzinfo is timezone.utc and first["url"].endswith(f"thread-{first['id']}-1-1.html")


# --- actions -----------------------------------------------------------------------------------------

def test_site_config():
    s = Registry.discover(custom_root=None).sites["onepoint3acres"]
    assert s.base_url == API and s.extras["min_interval"] == 1.0


def test_thread_reads_floors_with_their_nested_replies(fake_http, cli):
    fake_http.routes[api("/api/v3/home-threads/1196058")] = answer(api("/"), "thread.json")
    fake_http.routes[replies(1196058, 1)] = answer(api("/"), "posts_p1.json")
    fake_http.routes[replies(1196058, 2)] = answer(api("/"), "posts_p2.json")
    code, out = cli("onepoint3acres", "thread", "--id", "https://www.1point3acres.com/bbs/thread-1196058-1-1.html")
    assert code == 0, out
    assert "# [跳槽] aws和tiktok哪个压力更大" in out
    assert "Board: 职场达人 (98) · " in out and "48 replies (26 floors, the rest nested) · Tags: Amazon, Bytedance" in out
    assert "[地里匿名用户QYUD2 #1] (2026-11-06 17:22 · 👍1 · OP · +3 reactions): 楼主现在在aws" in out
    assert "[地里匿名用户3IJGM #2] (2026-11-06 17:26 · 👍2 · +2 reactions): 40个sev2 怎么活啊" in out
    assert "    ↳ 地里匿名用户QYUD2 (2026-11-06 17:52 · 👍1 · OP): 从早忙到晚，抽空吃饭" in out
    assert "↳ 地里匿名用户3IJGM (2026-11-06 18:24): @地里匿名用户QYUD2 全是ops issue" in out
    assert "[... omitted 16 post numbers (#6–#21), deleted or hidden …]" in out   # trimmed fixture pages
    assert "#22]" in out and "#23]" in out
    assert [r.url for r in fake_http.requests] == [api("/api/v3/home-threads/1196058"), replies(1196058, 1),
                                                   replies(1196058, 2)]


def test_thread_range_reads_only_its_page(fake_http, cli):
    fake_http.routes[api("/api/v3/home-threads/1196058")] = answer(api("/"), "thread.json")
    fake_http.routes[replies(1196058, 1)] = answer(api("/"), "posts_p1.json")
    code, out = cli("onepoint3acres", "thread", "--id", "1196058", "--to", "3")
    assert code == 0, out
    assert "#3]" in out and "#4]" not in out and len(fake_http.requests) == 2
    assert "esum onepoint3acres thread --id 1196058 --from 4 --to 27" in out


def test_locked_thread_notes_the_points(fake_http, cli):
    fake_http.routes[api("/api/v3/home-threads/1196170")] = answer(api("/"), "locked.json")
    fake_http.routes[replies(1196170, 1)] = Response(200, api("/"), {}, b'{"errno":0,"msg":"OK","posts":[],"total":0}')
    code, out = cli("onepoint3acres", "thread", "--id", "1196170")
    assert code == 0, out
    assert "The opening post is locked: it needs more than 200 points (积分)." in out
    assert "OP): [hidden: needs more than 200 points (积分)]" in out


def test_missing_thread_and_login_answers(fake_http, cli):
    fake_http.routes[api("/api/v3/home-threads/99999999")] = answer(api("/"), "missing.json", 404)
    code, out = cli("onepoint3acres", "thread", "--id", "99999999")
    assert code == 1 and "您要查看的内容不存在" in out
    fake_http.routes[api("/api/v3/home-threads/1")] = answer(api("/"), "login.json", 401)
    code, out = cli("onepoint3acres", "thread", "--id", "1")
    assert code == 3 and "LOGIN NEEDED" in out and "请先登录" in out


def test_forums_and_forum_by_name_and_type(fake_http, cli):
    fake_http.routes[api("/api/forums")] = answer(api("/"), "forums.json")
    code, out = cli("onepoint3acres", "forums", "--query", "面经")
    assert code == 0, out
    assert "  145,海外面经,海外求职" in out and "  335,PM面经,海外求职" in out and "  28,求职（非面经）,海外求职" in out
    url = api("/api/forums/28/threads", pg=1, ps=30, typeid=532)
    fake_http.routes[url] = answer(url, "forum145.json")
    code, out = cli("onepoint3acres", "forum", "--id", "求职（非面经）", "--type", "选组")
    assert code == 0, out
    assert "id: 28" in out and "name: 求职（非面经）" in out and "threads[3]{id,title,replies,last_post}:" in out
    code, out = cli("onepoint3acres", "forum", "--id", "28", "--type", "nothing")
    assert code == 2 and "532 选组选Offer" in out


def test_latest_hot(fake_http, cli):
    url = api("/api/threads", type="hot", pg=1, ps=30, includes="forum_name")
    fake_http.routes[url] = answer(url, "hot.json")
    code, out = cli("onepoint3acres", "latest")
    assert code == 0, out
    assert "threads[3]{id,title,forum,replies}:" in out and "next page" not in out   # a short page is the last


def test_search_sends_the_trpc_batch_and_pages_by_cursor(fake_http, cli):
    payload = {"query": "租车", "type": "keywords", "days": None, "fids": None, "sortBy": "relevance",
               "direction": "forward"}
    item = {"json": payload, "meta": {"values": {"days": ["undefined"], "fids": ["undefined"]}}}
    url = "https://trpc.1point3acres.com/trpc/search.search?" + form_encode(
        {"batch": 1, "input": json.dumps({"0": item}, ensure_ascii=False, separators=(",", ":"))}, None)
    fake_http.routes[url] = answer(url, "search.json")
    code, out = cli("onepoint3acres", "search", "--query", "租车", "--type", "keywords")
    assert code == 0, out
    assert "threads[3]{id,title,forum,date}:" in out
    assert '  1159213,csp 的租车保险能在欧洲用吗,兴趣爱好,"2025-12-25 14:08"' in out
    assert "Run `esum onepoint3acres search --query '租车' --type keywords --cursor 20` for the next page" in out


def test_empty_board_search_points_to_the_board_list(fake_http, cli):
    payload = {"query": "Google", "type": "keywords", "days": None, "fids": "145", "sortBy": "dateline",
               "direction": "forward"}
    item = {"json": payload, "meta": {"values": {"days": ["undefined"]}}}
    url = "https://trpc.1point3acres.com/trpc/search.search?" + form_encode(
        {"batch": 1, "input": json.dumps({"0": item}, ensure_ascii=False, separators=(",", ":"))}, None)
    empty = b'[{"result":{"data":{"json":{"data":[],"cursor":null},"meta":{"values":{"cursor":["undefined"]}}}}}]'
    fake_http.routes[url] = Response(200, url, {"content-type": "application/json"}, empty)
    code, out = cli("onepoint3acres", "search", "--query", "Google", "--type", "keywords", "--sort", "newest",
                    "--forum", "145")
    assert code == 0, out
    assert "search leaves some boards out for guests (the 面经 boards)" in out
    assert "Run `esum onepoint3acres forum --id 145` to list board 145's threads instead" in out


# --- dogfood fixes -----------------------------------------------------------------------------------

def test_a_board_that_hides_views_shows_none_not_zero(fake_http, cli):
    url = api("/api/forums/145/threads", pg=1, ps=30)
    fake_http.routes[url] = answer(url, "forum145.json")   # the 面经 boards send `views: 0` on every thread
    code, out = cli("onepoint3acres", "forum", "--id", "145", "--json")
    assert code == 0, out
    data = json.loads(out)["data"]
    assert [t["views"] for t in data["threads"]] == [None, None, None]
    code, out = cli("onepoint3acres", "forum", "--id", "145")
    assert code == 0 and "views: null where the site hides a thread's view count" in out
    fake_http.routes[api("/api/v3/home-threads/1196170")] = answer(api("/"), "locked.json")
    fake_http.routes[replies(1196170, 1)] = Response(200, api("/"), {}, b'{"errno":0,"msg":"OK","posts":[],"total":0}')
    code, out = cli("onepoint3acres", "thread", "--id", "1196170")
    assert code == 0, out
    assert "0 views" not in out and "Board: 海外面经 (145) · 7 replies" in out


def search_url(payload: dict) -> str:
    meta = {"values": {k: ["undefined"] for k, v in payload.items() if v is None}}
    return "https://trpc.1point3acres.com/trpc/search.search?" + form_encode(
        {"batch": 1, "input": json.dumps({"0": {"json": payload, "meta": meta}}, ensure_ascii=False,
                                         separators=(",", ":"))}, None)


def test_names_with_html_entities_are_decoded(fake_http, cli):
    url = search_url({"query": "包", "type": "hybrid", "days": 30, "fids": None, "sortBy": "relevance",
                      "direction": "forward"})
    hit = {"tid": 1196043, "fid": 237, "subject": "谷歌 staff swe 包", "forum_name": "&#128273;晒工资抖包袱",
           "author": "A&amp;B", "dateline": 1794000000, "views": 0, "replies": 3}
    body = json.dumps([{"result": {"data": {"json": {"data": [hit], "cursor": None}}}}]).encode()
    fake_http.routes[url] = Response(200, url, {"content-type": "application/json"}, body)
    code, out = cli("onepoint3acres", "search", "--query", "包", "--days", "30", "--fields", "author")
    assert code == 0, out
    assert "1196043,谷歌 staff swe 包,🔑晒工资抖包袱," in out and ",A&B" in out and "&#" not in out


@pytest.mark.parametrize("argv", [("--days", "-1"), ("--days", "0"), ("--cursor", "-5")])
def test_search_refuses_out_of_range_days_and_cursor(fake_http, cli, argv):
    code, out = cli("onepoint3acres", "search", "--query", "x", *argv)
    assert code == 2 and out.startswith("error: ") and "\nhelp: " in out
    assert fake_http.requests == []


def test_forums_hint_names_the_columns_the_doc_names(fake_http, cli):
    fake_http.routes[api("/api/forums")] = answer(api("/"), "forums.json")
    code, out = cli("onepoint3acres", "forums")
    assert code == 0, out
    assert "Add `--fields parent,today,types`" in out


def test_thread_header_mentions_nesting_only_when_there_is_some(fake_http, cli):
    fake_http.routes[api("/api/v3/home-threads/1196209")] = answer(api("/"), "attach.json")   # 1 reply
    one = {"errno": 0, "msg": "OK", "total": 1,
           "posts": [{"pid": 5, "author": "someone", "message_bbcode": "谢谢", "dateline": 1794000000}]}
    fake_http.routes[replies(1196209, 1)] = Response(200, api("/"), {}, json.dumps(one).encode())
    code, out = cli("onepoint3acres", "thread", "--id", "1196209")
    assert code == 0, out
    assert " · 1 reply" in out and "nested" not in out and "1 replies" not in out
