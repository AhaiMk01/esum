"""Offline tests for the V2EX module on saved v1 API and sov2ex responses."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from esum import Post
from esum.core.registry import Registry
from esum.sites.v2ex.parse import match_nodes, reply_target, thread_from, topic_from_api, topic_from_hit
from esum.transport import Response
from esum.core.posts import format_post
from esum.sites.v2ex import parse as v2

FIX = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def test_api_topic_numbers_come_as_strings():
    t = topic_from_api(load("topics_hot.json")[0])
    assert t.id == 1251868 and t.replies == 94 and t.node == "career" and t.author == "vergilgao"
    assert t.date is not None and t.date.year == 2026
    assert t.url == "https://www.v2ex.com/t/1251868" and t.text


def test_sov2ex_hit_has_utc_time_and_no_node_name():
    t = topic_from_hit(load("sov2ex_search.json")["hits"][0])
    assert t.id == 114961 and t.title == "关于学习 Python" and t.author == "typing"
    assert t.node is None and t.replies == 19
    assert t.date.isoformat() == "2014-05-29T23:51:36+00:00"


def test_thread_numbers_floors_and_follows_mentions():
    th = thread_from(load("topic_1251868.json")[0], load("replies_1251868.json"))
    assert [p.number for p in th.posts] == list(range(1, 14))
    op = th.posts[0]
    assert op.is_op and op.user == "vergilgao" and op.url == "https://www.v2ex.com/t/1251868"
    assert th.node == "career"
    by_number = {p.number: p for p in th.posts}
    # floor 2 (post #3) is the OP answering "@SssaltedFish", who wrote post #2
    assert by_number[3].is_op and by_number[3].reply_to == 2
    assert by_number[4].reply_to == 3          # "@vergilgao" → the OP's latest post before it
    assert by_number[2].reply_to is None
    assert by_number[2].url.startswith("https://www.v2ex.com/t/1251868#r_")


def test_reply_target_rules():
    posts = [Post(1, "op", "t", is_op=True), Post(2, "a", "x"), Post(3, "b", "y"), Post(4, "a", "z")]
    assert reply_target("@a 你好", 5, posts) == 4          # their latest post
    assert reply_target("@b #2 同意", 5, posts) == 3       # floor 2 = post #3
    assert reply_target("@op 谢谢", 5, posts) is None      # answering the topic itself: no ↩
    assert reply_target("@a #9 未来", 5, posts) is None    # a floor that doesn't exist yet
    assert reply_target("中间 @a 提到", 5, posts) is None   # only a leading mention counts


def test_match_nodes_filters_and_sorts_by_topics():
    found = match_nodes(load("nodes_all.json"), "优惠")
    assert [n["name"] for n in found] == ["deals", "discount"]
    assert len(match_nodes(load("nodes_all.json"), None)) == 5


# --- whole actions on a fake T1 backend --------------------------------------------------------------

class PathBackend:
    def __init__(self):
        self.requests = []

    def send(self, request, *, before_send=lambda: None):
        before_send()
        self.requests.append(request)
        u = urlsplit(request.url)
        q = parse_qs(u.query)
        routes = {
            ("www.v2ex.com", "/api/topics/hot.json"): lambda: load("topics_hot.json"),
            ("www.v2ex.com", "/api/topics/show.json"): lambda: (
                load("topic_1251868.json") if q.get("id") == ["1251868"]
                else [] if "id" in q else load("topics_hot.json")),
            ("www.v2ex.com", "/api/replies/show.json"): lambda: load("replies_1251868.json"),
            ("www.v2ex.com", "/api/nodes/all.json"): lambda: load("nodes_all.json"),
            ("www.sov2ex.com", "/api/search"): lambda: load("sov2ex_search.json"),
        }
        route = routes.get((u.hostname, u.path))
        if route is None:
            return Response(404, request.url, {}, b"not found")
        return Response(200, request.url, {"content-type": "application/json"},
                        json.dumps(route(), ensure_ascii=False).encode())

    def close(self):
        pass


@pytest.fixture
def run(action_runner, capsys):
    backend = PathBackend()
    action_runner.backend = backend
    reg = Registry.discover(custom_root=None)

    def _run(*argv):
        code = action_runner.cli(["v2ex", *argv], registry=reg)
        captured = capsys.readouterr()
        return code, captured.out + captured.err, backend

    return _run


def test_thread_action(run):
    code, out, _ = run("thread", "--id", "1251868", "--from", "2", "--to", "4")
    assert code == 0
    assert "# 媳妇上个月辞职出来创业了 [career]" in out and "3 of 13 posts shown" in out
    assert "[vergilgao #3] (2026-10-26 05:40 · OP · ↩#2):" in out


def test_missing_topic_is_an_error(run):
    code, out, _ = run("thread", "--id", "1")
    assert code == 1 and "V2EX topic 1 not found" in out


def test_search_action_builds_sov2ex_query(run):
    code, out, backend = run("search", "--query", "python", "--node", "python,go", "--sort", "created",
                             "--page", "2", "--limit", "5")
    lines = out.splitlines()
    assert code == 0 and "query: python" in lines and "page: 2" in lines and "count: 2 of 67065 total" in lines
    assert "results[2]{id,title,date}:" in lines
    assert ("  Run `esum v2ex search --query python --node python,go --sort created --page 3 --limit 5` "
            "for the next page") in lines
    q = parse_qs(urlsplit(backend.requests[0].url).query)
    assert q["sort"] == ["created"] and q["from"] == ["10"] and q["size"] == ["5"]
    assert q["node"] == ["python,go"] and "username" not in q


def test_topics_action_feed_and_node(run):
    code, out, backend = run("topics", "--feed", "hot", "--limit", "2")
    lines = out.splitlines()
    assert code == 0 and "scope: hot topics" in lines and "count: 2 of 3 total" in lines
    assert "topics[2]{id,title,node,replies}:" in lines and any(line.startswith("  1251868,") for line in lines)
    assert "  Run `esum v2ex thread --id <id>` to read a topic with every reply" in lines
    code, out, backend = run("topics", "--node", "career")
    assert "scope: node career" in out.splitlines()
    assert parse_qs(urlsplit(backend.requests[-1].url).query) == {"node_name": ["career"]}


def test_nodes_action(run):
    code, out, _ = run("nodes", "--query", "优惠")
    lines = out.splitlines()
    assert code == 0 and "query: 优惠" in lines and "nodes[2]{name,title,topics}:" in lines
    assert "  deals,优惠信息,2858" in lines
    assert "  Run `esum v2ex topics --node <name>` to list a node's latest topics" in lines
    code, out, _ = run("nodes", "--query", "zzz")
    assert code == 0 and "nodes: \"0 found: no node matches 'zzz'\"" in out


# --- V2EX and NodeSeek against their raw pages: nothing unknown shown as zero, nothing silently missing ---

PAGE = ('<div class="topic_content">正文</div>'
        '<div class="subtle"><span class="fade">Supplement 1 &nbsp;·&nbsp; '
        '<span title="2026-04-09 22:57:24 +08:00">Apr 9</span></span><div class="sep5"></div>'
        '<div class="topic_content">步骤<br />1. <a href="https://x.invalid/">x</a></div></div>'
        '<div class="subtle"><span class="fade">第 2 条附言 &nbsp;·&nbsp; '
        '<span title="2026-04-10 17:55:24 +08:00">Apr 10</span></span><div class="sep5"></div>'
        '<div class="topic_content"><div class="markdown_body"><p>终止试用</p></div></div></div>'
        '<div class="box">下面是回复</div>')


def test_v2ex_reads_supplements_from_the_page():
    found = v2.supplements(PAGE)
    assert [s.number for s in found] == [1, 2]
    assert found[0].date == "2026-04-09 14:57"          # UTC, like the rest of the thread
    assert "1. x" in found[0].text and found[1].text == "终止试用"


def test_v2ex_supplements_join_the_opening_post():
    th = v2.thread_from({"id": 1, "title": "t", "content": "正文", "member": {"username": "a"}, "created": 1}, [])
    v2.add_supplements(th, v2.supplements(PAGE))
    assert th.posts[0].text.startswith("正文\n[supplement 1, 2026-04-09 14:57]\n步骤")
    assert th.posts[0].text.endswith("[supplement 2, 2026-04-10 09:55]\n终止试用")


def test_v2ex_unknown_thanks_are_null_not_zero():
    th = v2.thread_from({"id": 1, "title": "t", "content": "x", "member": {"username": "a"}, "created": 1},
                        [{"id": 2, "content": "y", "member": {"username": "b"}, "created": 2}])
    assert [p.likes for p in th.posts] == [None, None]
    assert "👍" not in format_post(th.posts[1])


def test_v2ex_search_rows_leave_out_the_index_reply_count():
    hit = {"_id": "1", "_source": {"id": 1, "title": "t", "replies": 0, "created": "2026-10-01T00:00:00"}}
    assert "replies" not in v2.search_row(v2.topic_from_hit(hit))
