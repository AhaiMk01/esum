"""Discourse engine, offline on its scrubbed fixtures (tests/fixtures/discourse/).

Parsers are tested on the captured responses directly. Actions run through the CLI on `forum`, a
synthetic site that is nothing but a base URL, against `ForumFake` (tests/support/discourse.py), a
backend that answers Discourse URLs from the same fixtures; its post-number paging
(`posts.json?post_number=N&asc=…`) is checked against the captured pages first. A real Discourse
site's own cases are in that site's folder.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit


from esum.core.errors import EXIT_ERROR, EXIT_LOGIN_NEEDED, EXIT_OK, EXIT_USAGE
from esum.core.posts import format_post
from esum.engines.discourse import parse
from esum.transport import Response, is_cloudflare_challenge
from conftest import json_response
from esum import Options
from esum import site
import esum.engines.discourse as discourse
import io

from support.discourse import (BASE, FIX, TOPIC, WALLED, WALLED_CATEGORIES, ForumFake, all_posts,  # noqa: F401
                               backend, cli, engine_sites, forum_registry, json_resp, load, site_fake, walled)


# --- parsers -------------------------------------------------------------------------------------------

def test_topic_post_parsing():
    topic = load("topic.json")
    raw = parse.stream_posts(topic)
    op = parse.op_user_id(topic)
    first = parse.to_post(raw[0], base_url=BASE, topic_id=TOPIC, slug=topic["slug"], op_id=op)
    assert (first.number, first.user, first.is_op, first.likes) == (1, "user1", True, 38)
    assert first.url == f"{BASE}/t/modernizing-the-foundation-theme/{TOPIC}/1"
    assert first.date.year == 2026 and first.text.startswith("The design team")
    reply = parse.to_post(raw[4], base_url=BASE, topic_id=TOPIC, op_id=op)
    assert reply.reply_to == 4 and not reply.is_op


def test_small_action_post_and_quotes():
    topic = load("topic.json")
    action = next(p for p in parse.stream_posts(topic) if p["post_type"] == parse.SMALL_ACTION)
    assert parse.post_text(action) == "[moderator action: pinned_globally.enabled]"
    quoted = next(p for p in all_posts() if 'class="quote' in p["cooked"])
    assert parse.post_text(quoted).startswith("> @user1 #1: Categories\n")


def test_lightbox_chrome_is_dropped():
    html = ('<div class="lightbox-wrapper"><a class="lightbox" href="https://cdn.invalid/big.png">'
            '<img src="x" alt="shot"><div class="meta"><span class="filename">shot</span>'
            '<span class="informations">1320×465 48.7 KB</span></div></a></div>')
    assert parse.post_text({"cooked": html, "post_type": 1}) == "[image: shot https://cdn.invalid/big.png]"


def quote(user: str, post: int, text: str, topic: int = 7, full: bool = False) -> str:
    return (f'<aside class="quote no-group" data-username="{user}" data-post="{post}" data-topic="{topic}"'
            + (' data-full="true"' if full else "") + '>\n<div class="title">\n<div class="quote-controls"></div>\n'
            f'<img class="avatar"> {user}:</div>\n<blockquote>\n{text}\n</blockquote>\n</aside>')


def test_quotes_keep_lines_and_existing_caps():
    def text(html):
        return parse.post_text({"cooked": html, "post_type": 1, "topic_id": 7})

    assert text(quote("ann", 3, "<p>hello <b>there</b></p><p>second</p>") + "<p>reply</p>") \
        == "> @ann #3: hello there\n> second\nreply"
    long = "<p>" + "word " * 100 + "</p>"
    selected = text(quote("ann", 3, long)).split("\n")[0]
    assert len(selected) == len("> @ann #3: ") + parse.QUOTE_CHARS and selected.endswith("…")
    whole = text(quote("ann", 3, long, full=True))
    assert len(whole) == len("> @ann #3: ") + parse.FULL_QUOTE_CHARS
    assert text(quote("bob", 5, "<p>x</p>", topic=99)) == "> @bob (topic 99 #5): x"
    # a quote inside the quote is left out; only the outer quoter's words stay
    nested = quote("bob", 5, quote("ann", 3, "<p>inner</p>") + "<p>outer</p>")
    assert text(nested + "<p>me</p>") == "> @bob #5: outer\nme"
    no_user = '<aside class="quote" data-post="13" data-topic="7"><blockquote><p>y</p></blockquote></aside>'
    assert text(no_user) == "> #13: y"


def test_link_previews_and_emoji():
    onebox = ('<aside class="onebox allowlistedgeneric" data-onebox-src="https://example.com/a">'
              '<header><a href="https://example.com/">example.com</a></header>'
              '<article><h3>A long preview title</h3><p>Preview body text</p></article></aside>')
    html = f'<p>see <img class="emoji" alt=":smile:" src="x.png"></p>{onebox}'
    assert parse.post_text({"cooked": html, "post_type": 1}) == "see :smile:\n[link: https://example.com/a]"
    other = '<aside class="custom"><p>kept</p></aside>'
    assert parse.post_text({"cooked": other, "post_type": 1}) == "kept"


def test_likes_count_hearts_only():
    # linux.do counts every reaction as a like action; only the heart is a like
    post = {"reactions": [{"id": "heart", "count": 569}, {"id": "+1", "count": 92}],
            "actions_summary": [{"id": 2, "count": 661}], "reaction_users_count": 661}
    assert parse.likes_of(post) == 569
    assert parse.likes_of({"reactions": [{"id": "+1", "count": 3}], "reaction_users_count": 3}) == 0
    assert parse.likes_of({"like_count": 2}) == 2
    assert parse.likes_of({"actions_summary": [{"id": 2, "count": 7}], "reaction_users_count": 9}) == 7
    assert parse.likes_of({"actions_summary": [], "reaction_users_count": 4}) == 0


def test_stream_ids_and_megatopic():
    topic = load("topic.json")
    ids = parse.stream_ids(topic)
    assert len(ids) == 100 and ids[0] == parse.stream_posts(topic)[0]["id"]
    mega = copy.deepcopy(topic)
    del mega["post_stream"]["stream"]
    assert parse.stream_ids(mega) is None
    assert parse.select_ids(ids, "medium", 30, 30) == ids[:30] + ids[-30:]
    assert parse.select_ids(ids, "full", 30, 30) == ids


def test_fake_paging_matches_captured_pages():
    fake = ForumFake()
    for name, asc in (("posts_after_50.json", True), ("posts_before_50.json", False)):
        captured = [p["post_number"] for p in parse.stream_posts(load(name))]
        assert [p["post_number"] for p in fake.page(50, asc)] == captured


def test_search_results():
    data = load("search.json")
    items = parse.search_results(data, BASE)
    assert len(items) == len(data["topics"]) and parse.search_more(data)
    with_hit = [i for i in items if i.get("blurb")]
    assert with_hit, "fixture should link at least one topic to its matching post"
    hit = with_hit[0]
    assert hit["url"].endswith(f"/{hit['id']}/{hit['post_number']}") and hit["post_user"].startswith("user")
    row = parse.topic_row(hit, search=True)
    assert row["post_user"].startswith("user") and row["snippet"] and len(row["snippet"]) <= 301
    assert row["date"] is None or (row["date"].tzinfo is not None and row["date"].utcoffset().total_seconds() == 0)


def test_search_term():
    assert parse.search_term("csr", category="5", order="latest", after="2026-01-01", user="@bob") == \
        "csr category:5 @bob after:2026-01-01 order:latest"
    assert parse.search_term("csr") == "csr"
    assert parse.days_ago(60, today=__import__("datetime").date(2026, 10, 26)) == "2026-08-27"


def test_topic_lists():
    latest = parse.topic_list(load("latest.json"), BASE)
    assert latest and all(t["url"].startswith(f"{BASE}/t/") for t in latest)
    hot = load("hot.json")
    pinned = sum(1 for t in hot["topic_list"]["topics"] if t.get("pinned"))
    assert len(parse.topic_list(hot, BASE, include_pinned=False)) == len(hot["topic_list"]["topics"]) - pinned
    assert parse.has_more(load("latest.json"))


def test_categories_from_site():
    cats = parse.categories_from_site(load("site.json"))
    child = next(c for c in cats if c["parent_id"])
    assert " / " in child["path"]
    parent_pos = next(i for i, c in enumerate(cats) if c["id"] == child["parent_id"])
    assert parent_pos < cats.index(child), "a parent sorts before its children"
    assert parse.find_category(cats, child["slug"].upper())["id"] == child["id"]
    assert parse.find_category(cats, child["path"])["id"] == child["id"]
    assert parse.find_category(cats, str(child["id"]))["id"] == child["id"]
    assert parse.find_category(cats, "no such category") is None


def test_cloudflare_challenge_detection():
    page = (FIX / "cf_challenge.html").read_text(encoding="utf-8")
    assert is_cloudflare_challenge(403, {"cf-mitigated": "challenge"}, page)
    assert is_cloudflare_challenge(403, {}, page)
    assert not is_cloudflare_challenge(200, {}, '{"topic_list": {}}')
    assert not is_cloudflare_challenge(404, {}, "not found")


# --- actions -------------------------------------------------------------------------------------------

def test_any_discourse_forum_is_just_a_base_url(forum_registry):
    forum = forum_registry.get("forum")
    assert forum.engine == "discourse" and {"search", "thread", "bookmark"} <= set(forum.all_actions)


def test_thread_medium_reads_head_and_tail(cli, backend):
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC))
    assert code == EXIT_OK
    assert "· 60 of 100 posts shown" in out and "Read: first 30 and last 30 of 100 visible posts" in out
    assert "not read …]" in out and "deleted or hidden …]" in out
    assert "[user1 #1] (2026-02-06" in out and "OP" in out.splitlines()[5]
    by_ids = [r for r in backend.requests if "post_ids" in r.url]
    assert len(by_ids) == 1, "head posts come inline; the tail is one batch"


def test_thread_full_covers_every_visible_post(cli, backend):
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC), "--mode", "full", "--json")
    assert code == EXIT_OK
    data = json.loads(out)["data"]
    posts = [i for i in data["posts"] if not i.get("gap")]
    gaps = [i for i in data["posts"] if i.get("gap")]
    assert len(posts) == 100 and [p["number"] for p in posts] == sorted(p["number"] for p in posts)
    assert gaps and all(g["reason"] == "deleted or hidden" for g in gaps)
    assert sum(g["count"] for g in gaps) + len(posts) == 117


def test_thread_range_pages_by_post_number(cli, backend):
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC), "--from", "49", "--to", "75", "--json")
    assert code == EXIT_OK
    items = json.loads(out)["data"]["posts"]
    numbers = [i["number"] for i in items if not i.get("gap")]
    assert numbers[0] == 49 and numbers[-1] <= 75 and 50 not in numbers
    assert items[2] == {"gap": True, "start": 50, "end": 50, "count": 1, "reason": "deleted or hidden"}
    assert [(i["start"], i["end"]) for i in items if i.get("gap") and i["reason"] == "not read"] == [(1, 48), (76, 117)]
    assert not any("post_ids" in r.url for r in backend.requests)
    expected = [p["post_number"] for p in all_posts() if 49 <= p["post_number"] <= 75]
    assert numbers == expected


def test_thread_range_inside_inline_posts_needs_one_request(cli, backend):
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC), "--from", "3", "--to", "6")
    assert code == EXIT_OK and " #3]" in out and " #6]" in out and " #7]" not in out
    assert backend.paths() == [f"/t/{TOPIC}.json"]


def test_megatopic_medium_pages_both_ends(cli, backend, action_runner):
    mega = ForumFake(mega=True)
    action_runner.backend = mega
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC), "--json")
    assert code == EXIT_OK
    data = json.loads(out)["data"]
    numbers = [i["number"] for i in data["posts"] if not i.get("gap")]
    every = [p["post_number"] for p in all_posts()]
    assert data["megatopic"] and numbers == every[:30] + every[-30:]
    assert f"/t/{TOPIC}/last.json" in mega.paths()
    assert not any("post_ids" in r.url for r in mega.requests)


def test_megatopic_full_is_refused_with_a_range_hint(cli, action_runner, backend):
    mega = ForumFake(mega=True)
    action_runner.backend = mega
    code, err, _ = cli("forum", "thread", "--id", str(TOPIC), "--mode", "full")
    assert code == 2 and "megatopic" in err and "--from/--to" in err


def test_search_action_builds_the_query(cli, backend):
    code, out, _ = cli("forum", "search", "--query", "bookmark reminder", "--category", "5",
                       "--recent", "--days", "30", "--user", "someone")
    lines = out.splitlines()
    assert code == EXIT_OK and any(line.startswith("topics[") and line.endswith("]{id,title,post_number,date,snippet}:")
                                   for line in lines)
    assert any("--page 2` for the next page" in line for line in lines)
    assert "  Run `esum forum thread --id <id> --from <post_number> --to <post_number>` to read the matching post" in lines
    q = parse_qs(urlsplit(backend.requests[-1].url).query)["q"][0]
    assert q.startswith("bookmark reminder category:5 @someone after:") and q.endswith("order:latest")


def test_search_by_category_name_uses_site_json(cli, backend):
    cats = parse.categories_from_site(load("site.json"))
    child = next(c for c in cats if c["parent_id"])
    code, _, _ = cli("forum", "search", "--query", "x y z", "--category", child["name"])
    assert code == EXIT_OK
    assert backend.paths() == ["/site.json", "/search.json"]
    assert f"category:{child['id']}" in parse_qs(urlsplit(backend.requests[-1].url).query)["q"][0]


def test_unknown_category_is_a_usage_error(cli, backend):
    code, err, _ = cli("forum", "latest", "--category", "nope")
    assert code == EXIT_USAGE and "unknown category 'nope'" in err and "help: Run `esum forum categories`" in err


def test_latest_in_category_and_created_order(cli, backend):
    code, out, _ = cli("forum", "latest", "--category", "67", "--order", "created")
    lines = out.splitlines()
    assert code == EXIT_OK and "list: latest" in lines and "order: created" in lines
    assert "category: category 67" in lines
    assert any(line.startswith("topics[") and line.endswith("]{id,title,posts,created}:") for line in lines)
    assert "  Run `esum forum thread --id <id>` to read a topic" in lines
    assert backend.requests[-1].url == f"{BASE}/c/67/l/latest.json?order=created"


def test_top_and_hot(cli, backend):
    assert cli("forum", "top", "--period", "yearly")[0] == EXIT_OK
    assert backend.requests[-1].url == f"{BASE}/top.json?period=yearly"
    code, out, _ = cli("forum", "hot", "--page", "1")
    assert code == EXIT_OK and backend.requests[-1].url == f"{BASE}/hot.json?page=1"


def test_search_rows_carry_the_matching_posts_time(cli, backend):
    code, out, _ = cli("forum", "search", "--query", "bookmark reminder")
    assert code == EXIT_OK and "times: UTC" in out.splitlines()
    assert re.search(r'^  \d+,.*,"2020-04-05 18:21",', out, re.M)   # to the minute, not just the day


def test_search_takes_filters_without_a_query(cli, backend):
    code, out, _ = cli("forum", "search", "--user", "someone", "--after", "2026-10-01")
    assert code == EXIT_OK
    assert parse_qs(urlsplit(backend.requests[-1].url).query)["q"][0] == "@someone after:2026-10-01 order:latest"
    code, out, _ = cli("forum", "search")
    assert code == EXIT_USAGE and "search needs --query or a filter" in out and "help: add --query" in out


def test_latest_can_leave_out_pinned_topics(cli, backend):
    url = f"{BASE}/latest.json"
    data = load("latest.json")
    data["topic_list"]["topics"][0]["pinned"] = True
    backend.overrides[url] = json_resp(url, data)
    _, with_pinned, _ = cli("forum", "latest")
    _, without, _ = cli("forum", "latest", "--no-pinned")
    first = data["topic_list"]["topics"][0]["id"]
    assert f"\n  {first}," in with_pinned and f"\n  {first}," not in without


def test_posts_reads_back_to_since_across_pages(cli, backend):
    newest = load("latest_posts.json")["latest_posts"]
    code, out, _ = cli("forum", "posts", "--since", "2026-10-30T14:00:00Z")
    lines = out.splitlines()
    assert code == EXIT_OK and 'since: "2026-10-30 14:00"' in lines and "count: 5" in lines
    assert "posts[5]{date,topic,number,text}:" in lines
    assert lines[lines.index("posts[5]{date,topic,number,text}:") + 1].startswith('  "2026-10-30 16:39",416118,4,')
    older = load("latest_posts_before.json")["latest_posts"]
    assert [r.url.removeprefix(f"{BASE}/posts.json") for r in backend.requests] == [
        "", f"?before={newest[-1]['id']}", f"?before={older[-1]['id']}"]   # the last page confirms the end
    assert "next page" not in out   # reached --since
    assert "  Run `esum forum thread --id <topic> --from <number>` to read a post in its topic" in lines


def test_posts_limit_ends_with_a_next_page_hint(cli, backend):
    newest = load("latest_posts.json")["latest_posts"]
    code, out, _ = cli("forum", "posts", "--limit", "2")
    assert code == EXIT_OK and "count: 2" in out.splitlines()
    assert f"  Run `esum forum posts --limit 2 --before {newest[1]['id']}` for the next page" in out.splitlines()
    code, out, _ = cli("--json", "forum", "posts", "--limit", "1")
    row = json.loads(out)["data"]["posts"][0]
    assert row["date"].startswith("2026-10-30T16:39:28") and row["text"] and row["url"].endswith(
        f"/{row['topic']}/{row['number']}")


def test_user_profile_has_the_account_facts(cli, backend):
    code, out, _ = cli("forum", "user", "--name", "@user1")
    lines = out.splitlines()
    assert code == EXIT_OK
    for line in ("created: 2013-02-05 04:44", "last_seen: 2026-07-10 07:18", "trust_level: 2", "admin: true",
                 "read_hours: 4669.2", "days_visited: 3359", "posts: 19071",
                 "top_categories: Feature; Product Support; Bug", "times: UTC"):
        assert line in lines, line
    assert "  Run `esum forum user-posts --name user1` to read their latest topics and replies" in lines
    [profile] = json.loads(cli("--json", "forum", "user", "--name", "user1")[1])["data"]["users"]
    assert profile["likes_received"] == 44063 and profile["url"] == f"{BASE}/u/user1"


def test_user_profile_without_its_summary(cli, backend):
    url = f"{BASE}/u/user1/summary.json"
    backend.overrides[url] = Response(403, url, {"content-type": "application/json"}, b'{"errors": ["hidden"]}')
    code, out, _ = cli("forum", "user", "--name", "user1")
    assert code == EXIT_OK and "trust_level: 2" in out and "days_visited" not in out


def test_a_hidden_profile_says_so_and_points_at_search(cli, backend):
    url = f"{BASE}/u/user1.json"
    backend.overrides[url] = json_resp(url, {"user": {"username": "user1", "profile_hidden": True}})
    code, out, _ = cli("forum", "user", "--name", "user1")
    assert code == EXIT_OK and "profile_hidden: true" in out and "note: the user hides their profile" in out
    assert "/u/user1/summary.json" not in backend.paths()
    acts = f"{BASE}/user_actions.json?username=user1&filter=4%2C5&offset=0"
    backend.overrides[acts] = Response(404, acts, {"content-type": "application/json"}, b'{"errors": ["x"]}')
    code, out, _ = cli("forum", "user-posts", "--name", "user1")
    assert code == EXIT_USAGE and "user1 hides their profile" in out and "search --user user1" in out


def test_user_posts_lists_every_post_with_offset_paging(cli, backend):
    acts = load("user_actions.json")["user_actions"]
    code, out, _ = cli("forum", "user-posts", "--name", "user1")
    lines = out.splitlines()
    assert code == EXIT_OK and f"posts[{len(acts)}]{{date,topic,number,excerpt}}:" in lines
    assert lines[lines.index(f"posts[{len(acts)}]{{date,topic,number,excerpt}}:") + 1].startswith(
        '  "2023-08-21 18:29",')
    offsets = [parse_qs(urlsplit(r.url).query)["offset"][0] for r in backend.requests]
    assert offsets == ["0", "3", "5"]   # 30 per page on a real forum; the fake serves 3
    code, out, _ = cli("forum", "user-posts", "--name", "user1", "--limit", "2")
    assert "  Run `esum forum user-posts --name user1 --limit 2 --offset 2` for the next page" in out.splitlines()
    code, out, _ = cli("forum", "user-posts", "--name", "user1", "--since", "2023-06-01T14:25:00Z")
    assert "count: 3" in out.splitlines() and "next page" not in out   # 14:28 is in, 14:24 is not


def test_thread_since_starts_at_the_first_post_after_the_time(cli, backend):
    first = next(p for p in all_posts() if p["created_at"] >= "2026-03-01")
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC), "--since", "2026-03-01")
    assert code == EXIT_OK, out
    assert "#1]" in out.split("omitted")[0]   # the first post sets the context
    assert f"[... omitted {first['post_number'] - 2} post numbers (#2–#{first['post_number'] - 1}), before 2026-03-01 00:00 UTC …]" in out
    assert f"#{first['post_number']}]" in out and "Read: posts since 2026-03-01 00:00 UTC" in out
    assert "Times: UTC" in out
    assert any(path.startswith(f"/posts/by-date/{TOPIC}/2026-03-01T00:00:00Z") for path in backend.paths())


def test_thread_since_after_the_last_post_reads_nothing(cli, backend):
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC), "--since", "2026-10-01")
    assert code == EXIT_OK and "0 of 100 posts shown" in out and "no posts since 2026-10-01 00:00 UTC" in out
    assert "omitted 117 post numbers (#1–#117), before 2026-10-01" in out
    assert not any("/posts.json" in path for path in backend.paths())


def test_thread_bad_ranges_and_ids_are_usage_errors(cli, backend):
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC), "--from", "10", "--to", "5")
    assert code == EXIT_USAGE and "--from 10 is after --to 5" in out
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC), "--from", "500")
    assert code == EXIT_USAGE and "has posts #1–#117" in out and f"help: Run `esum forum thread --id {TOPIC} --from 88 --to 117`" in out
    code, out, _ = cli("forum", "thread", "--id", "999")
    assert code == 1 and "no topic 999 on forum" in out and "<div" not in out
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC), "--since", "2099-01-01")
    assert code == EXIT_USAGE and "is in the future" in out


def test_search_dates_take_relative_days_and_reject_garbage(cli, backend):
    code, out, _ = cli("forum", "search", "--query", "x", "--after", "7d")
    q = parse_qs(urlsplit(backend.requests[-1].url).query)["q"][0]
    assert code == EXIT_OK and re.fullmatch(r"x after:\d{4}-\d{2}-\d{2}", q)
    code, out, _ = cli("forum", "search", "--query", "x", "--after", "10/01/2026")
    assert code == EXIT_USAGE and "--after '10/01/2026' is not a time" in out
    code, out, _ = cli("forum", "search", "--query", "x", "--after", "0d")   # a whole day: today
    q = parse_qs(urlsplit(backend.requests[-1].url).query)["q"][0]
    assert code == EXIT_OK and q == f"x after:{datetime.now(timezone.utc):%Y-%m-%d}"


def test_posts_since_now_is_a_usage_error(cli, backend):
    code, out, _ = cli("forum", "posts", "--since", "0d")
    assert code == EXIT_USAGE and "--since '0d' is now" in out and "help:" in out and not backend.requests


def test_search_stops_at_discourse_page_ten(cli, backend):
    code, out, _ = cli("forum", "search", "--query", "bookmark reminder", "--page", "10")
    lines = out.splitlines()
    assert code == EXIT_OK and "next page" not in out
    assert any(line.startswith("note: ") and "stops at page 10" in line for line in lines)
    code, out, _ = cli("forum", "search", "--query", "bookmark reminder", "--page", "11")
    assert code == EXIT_USAGE and "at most 10 pages" in out


def test_hot_and_top_can_leave_out_pinned_topics(cli, backend):
    url = f"{BASE}/hot.json"
    data = load("hot.json")
    data["topic_list"]["topics"][0]["pinned"] = True
    backend.overrides[url] = json_resp(url, data)
    _, out, _ = cli("forum", "hot", "--no-pinned")
    assert f"\n  {data['topic_list']['topics'][0]['id']}," not in out
    assert "times: UTC" in cli("forum", "hot")[1] and "times: UTC" not in cli("forum", "top")[1]


def test_posts_text_reads_like_thread(cli, backend):
    out = json.loads(cli("--json", "forum", "posts", "--limit", "5")[1])["data"]["posts"]
    quoting = next(r for r in out if r["id"] == load("latest_posts_before.json")["latest_posts"][0]["id"])
    assert quoting["text"].startswith("> @someone #6: ")   # a quote, not the author's own words


def test_user_takes_several_names_and_names_a_missing_one(cli, backend):
    code, out, _ = cli("forum", "user", "--name", "user1", "@user1")
    assert code == EXIT_OK and out.count("username: user1") == 2
    url = f"{BASE}/u/ghost.json"
    backend.overrides[url] = Response(404, url, {"content-type": "application/json"}, b'{"errors": ["x"]}')
    code, out, _ = cli("forum", "user", "--name", "ghost")
    assert code == 1 and "no user 'ghost' on forum" in out


def test_search_without_hits_is_an_empty_list(cli, backend):
    url = f"{BASE}/search.json?q=zzz+nothing"
    backend.overrides[url] = json_resp(url, {"posts": [], "topics": [], "grouped_search_result": {}})
    code, out, _ = cli("forum", "search", "--query", "zzz nothing")
    assert code == EXIT_OK and "topics: \"0 found: no topics match 'zzz nothing'; every word must match, so try fewer\"" in out.splitlines()


def test_cloudflare_challenge_on_t1_names_the_fix(cli, backend):
    backend.overrides[f"{BASE}/t/{TOPIC}.json"] = Response(
        403, f"{BASE}/t/{TOPIC}.json", {"content-type": "text/html", "cf-mitigated": "challenge"},
        (FIX / "cf_challenge.html").read_bytes())
    code, err, _ = cli("forum", "thread", "--id", str(TOPIC))
    assert code == 1 and "error: ChallengeError: HTTP 403" in err
    assert "a bot check page (Cloudflare challenge) instead of the site's response" in err
    assert "body (untrusted):" not in err and "<!DOCTYPE" not in err
    assert "help: forum is behind a Cloudflare challenge" in err
    assert "transport=Transport.BROWSER_HTTP (T2) or IN_PAGE (T3)" in err


def test_not_logged_in_read_is_login_needed(cli, backend):
    backend.overrides[f"{BASE}/t/{TOPIC}.json"] = json_resp(f"{BASE}/t/{TOPIC}.json",
                                                             load("not_logged_in.json"), status=403)
    code, err, _ = cli("forum", "thread", "--id", str(TOPIC))
    assert code == EXIT_LOGIN_NEEDED and "LOGIN NEEDED" in err


# --- the bookmark write ----------------------------------------------------------------------------

def test_bookmark_topic_uses_t2_for_a_t1_site(cli, backend, action_runner):
    used = []
    action_runner.backend_factory = lambda site, options: used.append(site.transport) or backend
    code, out, _ = cli("forum", "bookmark", "--topic", str(TOPIC))
    assert code == EXIT_OK
    # the output (and the write log) name the bookmark and the account it went to
    assert f"done 1. Bookmark topic #{TOPIC} “Modernizing the Foundation theme” for @SCRUBBED on {BASE}" in out
    post = backend.requests[-1]
    assert (post.method, urlsplit(post.url).path) == ("POST", "/bookmarks.json")
    assert json.loads(post.content) == {"bookmarkable_id": TOPIC, "bookmarkable_type": "Topic"}
    assert post.headers["X-CSRF-Token"] == "SCRUBBED"
    assert used and {t.value for t in used} == {"browser_http"}


def test_bookmark_posts_one_write_each(cli, backend):
    code, out, _ = cli("forum", "bookmark", "--topic", str(TOPIC), "--posts", "3", "5")
    assert code == EXIT_OK and "done 1. Bookmark post #3 by user" in out
    bodies = [json.loads(r.content) for r in backend.requests if r.method == "POST"]
    ids = {p["post_number"]: p["id"] for p in all_posts()}
    assert bodies == [{"bookmarkable_id": ids[3], "bookmarkable_type": "Post"},
                      {"bookmarkable_id": ids[5], "bookmarkable_type": "Post"}]


def test_bookmark_logged_out_asks_for_login_before_writing(cli, backend):
    backend.logged_in = False
    code, err, _ = cli("forum", "bookmark", "--topic", str(TOPIC))
    assert code == EXIT_LOGIN_NEEDED and "LOGIN NEEDED" in err
    assert not any(r.method == "POST" for r in backend.requests)


# --- a forum behind a bot wall, with a static category map (`walled`) ---------------------------------

def test_a_static_category_map_needs_no_request(cli, walled):
    code, out, _ = cli("walled", "categories")
    lines = out.splitlines()
    assert code == EXIT_OK and f"categories[{len(WALLED_CATEGORIES)}]{{id,path,slug}}:" in lines
    assert "  5,玩卡 / 信用卡,credit-cards" in lines
    assert walled.requests == []


def test_a_write_transport_site_reads_over_cookieless_t2_and_bookmarks_over_t3(cli, walled):
    assert cli("walled", "thread", "--id", str(TOPIC))[0] == EXIT_OK
    assert walled.used == [("T2", False)]  # no browser for reads
    assert cli("walled", "bookmark", "--topic", str(TOPIC))[0] == EXIT_OK
    assert set(walled.used[1:]) == {("T3", True)}
    assert all(r.url.startswith(WALLED) for r in walled.requests)
    assert any(r.method == "POST" for r in walled.requests)


def test_a_cookieless_t2_site_writes_with_the_browser_cookies(engine_sites, backend, action_runner, capsys):
    """Without a write transport, a T2 site that reads without cookies still bookmarks logged in,
    and a challenge on its cookieless reads points at T3."""
    registry = engine_sites("discourse", fp=f'base_url="{BASE}", label="FP forum", '
                            'transport=Transport.BROWSER_HTTP, extras={"browser_cookies": False}')
    used = []
    action_runner.backend_factory = lambda site, options: used.append(site.extras.get("browser_cookies", True)) or backend
    assert action_runner.cli(["fp", "bookmark", "--topic", str(TOPIC)], registry=registry) == EXIT_OK
    assert used == [True]  # one client carries cookies through planning and the writes
    backend.overrides[f"{BASE}/t/{TOPIC}.json"] = Response(
        403, f"{BASE}/t/{TOPIC}.json", {"content-type": "text/html", "cf-mitigated": "challenge"},
        (FIX / "cf_challenge.html").read_bytes())
    capsys.readouterr()
    assert action_runner.cli(["fp", "thread", "--id", str(TOPIC)], registry=registry) == EXIT_ERROR
    assert "Chrome's network fingerprint (T2) no longer gets past fp's" in capsys.readouterr().out


def test_static_category_names_in_listings(cli, walled):
    code, out, _ = cli("walled", "latest", "--category", "credit-cards")
    assert code == EXIT_OK and "category: 玩卡 / 信用卡" in out.splitlines()
    assert walled.requests[-1].url == f"{WALLED}/c/5/l/latest.json"


# --- data that must survive (dogfooding round 3) ---------------------------------------------------

def test_emoji_keep_their_name_because_they_carry_meaning():
    cooked = ('<p>搬个 5<img src="https://x.invalid/brick.png" title=":brick:" class="emoji" alt=":brick:"> '
              '就够了<img src="https://x.invalid/t.png" class="emoji emoji-custom" alt=":troll:"></p>')
    assert parse.post_text({"cooked": cooked}) == "搬个 5:brick: 就够了:troll:"


def test_an_image_keeps_its_original_url():
    cooked = ('<p><div class="lightbox-wrapper"><a class="lightbox" href="https://x.invalid/original/a.jpeg" '
              'title="image"><img src="https://x.invalid/optimized/a_2_386x500.png" alt="image" width="386">'
              '<div class="meta"><span class="filename">image</span><span class="informations">491×730 89.6 KB'
              '</span></div></a></div></p><p>plain <img src="https://x.invalid/b.png" alt="收据"></p>')
    text = parse.post_text({"cooked": cooked})
    assert "[image: https://x.invalid/original/a.jpeg]" in text
    assert "[image: 收据 https://x.invalid/b.png]" in text
    assert "89.6 KB" not in text


def test_a_link_preview_url_is_not_double_escaped():
    cooked = '<aside class="onebox" data-onebox-src="https://x.invalid/a?b=1&amp;c=2"><p>card</p></aside>'
    assert parse.post_text({"cooked": cooked}) == "[link: https://x.invalid/a?b=1&c=2]"


def test_a_quote_keeps_the_links_it_quotes():
    cooked = ('<aside class="quote" data-username="a" data-post="14"><blockquote><p>'
              '<a href="https://x.invalid/apply?x=1">链接</a></p></blockquote></aside><p>谢谢</p>')
    text = parse.post_text({"cooked": cooked, "topic_id": 1})
    assert "> @a #14: 链接" in text and "[link: https://x.invalid/apply?x=1]" in text and "谢谢" in text


def test_posts_since_reads_past_a_post_with_an_old_date(cli, backend):
    newest = load("latest_posts.json")["latest_posts"]
    older = load("latest_posts_before.json")["latest_posts"]
    held_back = dict(newest[1], created_at="2026-10-01T00:00:00.000Z")   # released late: a new id, an old date
    url = f"{BASE}/posts.json"
    backend.overrides[url] = json_resp(url, {"latest_posts": [newest[0], held_back, *newest[2:]]})
    code, out, _ = cli("forum", "posts", "--since", "2026-10-30T14:00:00Z")
    assert code == EXIT_OK and "count: 4" in out.splitlines()   # 3 of the 4 newest, then #5 below them
    assert str(older[0]["topic_id"]) in out


def test_posts_at_the_limit_say_they_stopped_early(cli, backend, monkeypatch):
    newest = load("latest_posts.json")["latest_posts"]
    monkeypatch.setenv("ESUM_SPILL_CHARS", "10")
    code, out, _ = cli("--json", "forum", "posts", "--limit", "2")
    head = json.loads(out)
    assert code == EXIT_OK and head["summary"].startswith("2 posts")
    assert f"--before {newest[1]['id']}" in head["summary"]


def test_searching_a_member_only_category_as_a_guest_is_login_needed(cli, walled):
    code, out, _ = cli("walled", "search", "--query", "Hyatt", "--category", "钛金")
    assert code == EXIT_LOGIN_NEEDED and "钛金 is a member-only category" in out
    assert not any("/search.json" in p for p in walled.paths())


def test_forum_wide_reads_say_a_guest_sees_less(cli, walled):
    note = "scope: \"guest view: member-only categories are not included\""
    assert note in cli("walled", "search", "--query", "bookmark reminder")[1].splitlines()
    assert note in cli("walled", "posts", "--limit", "1")[1].splitlines()
    assert note not in cli("walled", "search", "--query", "x", "--category", "信用卡")[1].splitlines()


def test_the_header_shows_how_old_the_first_post_is():
    first = {"post_number": 1, "wiki": True, "version": 3, "updated_at": "2026-05-01T00:00:00.000Z"}
    line = parse.topic_meta_line({"post_stream": {"posts": [first]}, "created_at": "2020-01-01T00:00:00Z"})
    assert "Wiki" in line and "Created: 2020-01-01" in line and "First post edited: 2026-05-01" in line
    never = {"post_number": 1, "version": 1, "updated_at": "2026-05-01T00:00:00.000Z"}
    assert "edited" not in parse.topic_meta_line({"post_stream": {"posts": [never]}})


def test_tags_list_names_for_the_search_operator(cli, backend):
    url = f"{BASE}/tags.json"
    backend.overrides[url] = json_resp(url, {
        "tags": [{"name": "hyatt凯悦", "slug": "hyatt", "count": 5}, {"name": "chase摩根大通", "slug": "chase", "count": 9}],
        "extras": {"tag_groups": [{"tags": [{"name": "amex美国运通", "slug": "amex", "count": 7}]}]}})
    code, out, _ = cli("forum", "tags")
    lines = out.splitlines()
    i = lines.index("tags[3]{name,count}:")
    assert code == EXIT_OK and lines[i + 1:i + 4] == ["  chase摩根大通,9", "  amex美国运通,7", "  hyatt凯悦,5"]
    assert "tags[1]{name,count}:" in cli("forum", "tags", "--query", "hyatt")[1]


def test_search_inside_one_topic_lists_every_matching_post(cli, backend):
    hits = load("search.json")["posts"]
    code, out, _ = cli("forum", "search", "--query", "bookmark", "--topic", "11604", "--after", "2026-07-01")
    lines = out.splitlines()
    assert code == EXIT_OK and f"posts[{len(hits)}]{{post_number,date,user,snippet}}:" in lines
    q = parse_qs(urlsplit(backend.requests[-1].url).query)["q"][0]
    assert q == "bookmark topic:11604 after:2026-07-01 order:latest"   # newest first unless asked otherwise
    assert "  Run `esum forum thread --id 11604 --posts <post_number>` to read a matching post" in lines


def test_search_can_leave_out_the_snippet(cli, backend):
    code, out, _ = cli("forum", "search", "--query", "bookmark reminder", "--no-snippet")
    assert code == EXIT_OK and any(line.endswith("]{id,title,post_number,date}:") for line in out.splitlines())


def test_posts_rows_name_their_category(cli, walled):
    data = load("latest_posts.json")["latest_posts"]
    data[0]["category_id"] = 5
    url = f"{WALLED}/posts.json"
    walled.overrides[url] = json_resp(url, {"latest_posts": data})
    out = cli("--json", "walled", "posts", "--limit", "1")[1]
    assert json.loads(out)["data"]["posts"][0]["category"] == "玩卡 / 信用卡"


def test_a_partly_read_topic_says_how_to_read_more(cli, backend):
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC))
    assert code == EXIT_OK and "to read the posts left out" in out
    assert "to read the posts left out" not in cli("forum", "thread", "--id", str(TOPIC), "--mode", "full")[1]


def test_a_cut_cell_says_where_the_whole_text_is(cli, backend):
    data = load("latest_posts.json")["latest_posts"]
    data[0]["cooked"] = "<p>" + "长" * 400 + "</p>"
    url = f"{BASE}/posts.json"
    backend.overrides[url] = json_resp(url, {"latest_posts": data})
    code, out, _ = cli("forum", "posts", "--limit", "1")
    assert code == EXIT_OK and "text cut at 300 characters; `--json` has all of it" in out


def test_unknown_flag_help_names_the_no_form(cli, backend):
    code, out, _ = cli("forum", "latest", "--bogus")
    assert code == EXIT_USAGE and "--[no-]pinned" in out


def test_user_posts_default_columns_fit_a_list(cli, backend):
    code, out, _ = cli("forum", "user-posts", "--name", "user1", "--limit", "1")
    assert "posts[1]{date,topic,number,excerpt}:" in out.splitlines()


POLL = ('<div class="poll" data-poll-name="poll"><div class="poll-container"><ul><li>CSP 3倍</li><li>AMEX Gold 4倍</li>'
        '</ul></div><div class="poll-info"><div class="poll-info_counts"><span class="info-number">0</span>'
        '<span class="info-label">投票人</span></div></div></div>')


def test_a_poll_shows_its_real_votes_not_the_static_zero():
    polls = [{"name": "poll", "voters": 332, "options": [{"html": "CSP 3倍", "votes": 186},
                                                         {"html": "AMEX Gold 4倍", "votes": 146}]}]
    text = parse.post_text({"cooked": f"<p>你选哪个</p>{POLL}<p>说说理由</p>", "polls": polls})
    assert "[poll: CSP 3倍 186 · AMEX Gold 4倍 146 · 332 voters]" in text
    assert "投票人" not in text and "你选哪个" in text and "说说理由" in text


def test_a_poll_without_its_data_says_so():
    text = parse.post_text({"cooked": POLL})
    assert "[poll, votes not available: CSP 3倍 · AMEX Gold 4倍]" in text and "投票人" not in text


def test_search_dates_are_whole_days_so_hours_are_refused(cli, backend):
    code, out, _ = cli("forum", "search", "--query", "x", "--after", "24h")
    assert code == EXIT_USAGE and "whole days" in out and "posts --since" in out
    assert backend.requests == []


def test_a_field_with_no_values_is_an_error_not_a_column_of_nulls(cli, backend):
    code, out, _ = cli("forum", "search", "--query", "bookmark reminder", "--fields", "likes")
    assert code == EXIT_USAGE and "unknown field likes" in out


def test_thread_hint_names_the_posts_it_left_out(cli, backend):
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC))
    hint = re.search(r"--from (\d+) --to (\d+)` to read the posts left out", out)
    assert code == EXIT_OK and hint and int(hint[1]) > 30
    assert f"--query <words> --topic {TOPIC}" in out


def test_a_small_gap_is_read_rather_than_left_out():
    ids = list(range(65))
    assert parse.select_ids(ids, "medium", 30, 30) == ids
    assert len(parse.select_ids(list(range(100)), "medium", 30, 30)) == 60


def test_thread_since_with_nothing_new_leaves_the_first_post_out(cli, backend):
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC), "--since", "2026-10-01")
    assert code == EXIT_OK and "0 of 100 posts shown" in out and "omitted 117 post numbers (#1–#117)" in out


def test_top_and_hot_say_a_guest_sees_less(cli, walled):
    note = "scope: \"guest view: member-only categories are not included\""
    assert note in cli("walled", "top")[1].splitlines() and note in cli("walled", "hot")[1].splitlines()


def test_thread_reads_scattered_posts(cli, backend):
    numbers = [p["post_number"] for p in all_posts()]
    a, b = numbers[2], numbers[-1]
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC), "--posts", str(a), str(b))
    assert code == EXIT_OK and f"#{a}]" in out and f"#{b}]" in out and "2 of 100 posts shown" in out
    assert f"Read: posts #{a}, #{b}" in out
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC), "--posts", "3", "--from", "1")
    assert code == EXIT_USAGE and "--posts" in out


def test_other_reactions_are_counted_beside_the_likes():
    post = {"reactions": [{"id": "heart", "count": 188}, {"id": "+1", "count": 27}, {"id": "laughing", "count": 8}]}
    assert parse.likes_of(post) == 188 and parse.other_reactions(post) == 35
    assert parse.other_reactions({"like_count": 2}) == 0
    shown = parse.to_post({"post_number": 3, "username": "a", "cooked": "<p>x</p>", **post}, base_url="https://f", topic_id=1)
    assert format_post(shown).startswith("[a #3] (👍188 · +35 reactions): x")


def test_a_quote_header_that_is_not_a_username_gets_no_at():
    cooked = ('<aside class="quote" data-username="部署细节 (单卡 16 GB)" data-post="1" data-topic="9">'
              '<blockquote><p>IQ3</p></blockquote></aside>')
    text = parse.post_text({"cooked": cooked, "topic_id": 1})
    assert text.startswith("> 部署细节 (单卡 16 GB) (topic 9 #1): IQ3") and "@" not in text


def test_a_site_without_a_category_map_still_says_a_guest_sees_less(cli, backend):
    code, out, _ = cli("forum", "top")
    assert code == EXIT_OK and "no saved key: categories only members see may be missing" in out


def test_a_numbered_category_a_guest_cannot_open_says_so(cli, backend):
    url = f"{BASE}/c/9/l/latest.json"
    backend.overrides[url] = Response(404, url, {"content-type": "application/json"}, b'{"errors": ["x"]}')
    code, out, _ = cli("forum", "latest", "--category", "9")
    assert code == EXIT_USAGE and "category 9" in out and "may need a login" in out
    assert "HttpError" not in out


def test_thread_leaves_out_a_search_hint_a_guest_cannot_run(cli, backend, forum_registry, monkeypatch):
    assert "--topic" in cli("forum", "thread", "--id", str(TOPIC))[1]
    monkeypatch.setitem(forum_registry.get("forum").extras, "guest_search", False)
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC))
    assert code == EXIT_OK and "to read the posts left out" in out and "--topic" not in out


# --- next-step help (dogfood: every help line is a runnable command) ----------------------------------

def test_range_and_since_reads_offer_the_neighbouring_ranges(cli, backend):
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC), "--from", "49", "--to", "75")
    lines = out.splitlines()
    assert code == EXIT_OK
    assert f"  Run `esum forum thread --id {TOPIC} --from 1 --to 48` to read the posts before these" in lines
    assert f"  Run `esum forum thread --id {TOPIC} --from 76 --to 117` to read the posts after these" in lines
    first = next(p for p in all_posts() if p["created_at"] >= "2026-03-01")["post_number"]
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC), "--since", "2026-03-01", "--json")
    assert code == EXIT_OK
    # read backwards from the cutoff, without --since (it would override the range)
    assert (f"Run `esum forum thread --id {TOPIC} --from 2 --to {first - 1}` to read the posts before these"
            in json.loads(out)["help"])


def test_full_refusals_are_complete_and_name_esums_own_limit(cli, backend, action_runner, monkeypatch):
    from esum.engines import discourse
    monkeypatch.setattr(discourse, "MAX_POSTS", 50)
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC), "--mode", "full")
    assert code == EXIT_USAGE
    assert out.splitlines() == [
        f"error: topic {TOPIC} has 100 visible posts; --mode full reads at most 50, so read it in ranges "
        f"with --from/--to",
        f"help: Run `esum forum thread --id {TOPIC} --from 1 --to 50` to read the first chunk"]
    action_runner.backend = ForumFake(mega=True)
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC), "--mode", "full")
    assert code == EXIT_USAGE and f"topic {TOPIC} is a megatopic (highest post #117)" in out
    assert f"help: Run `esum forum thread --id {TOPIC} --from 1 --to 50` to read the first chunk" in out
    assert "rejected" not in out


def test_an_inverted_range_is_refused_before_any_request(cli, backend):
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC), "--from", "30", "--to", "5")
    assert code == EXIT_USAGE and out.splitlines() == [
        "error: --from 30 is after --to 5", "help: Give --from less than or equal to --to"]
    assert backend.requests == []


# --- Regressions for review fixes in text extraction and the parsers (offline) -----------------------

# --- Discourse: --from N when post N-1 is gone -----------------------------------------------------------

def test_from_n_fetches_post_n_when_the_anchor_resolves_to_it(action_runner, fake_http):
    """A deleted anchor 41 resolves to 42; the near request must recover post 42."""
    posts = [{"id": 1000 + n, "post_number": n} for n in range(1, 80) if n != 41]
    base = "https://forum.invalid"
    fake_http.routes.update({
        base + "/t/1.json": json_response(base, {"id": 1, "highest_post_number": 79,
                                                  "post_stream": {"posts": posts[:20]}}),
        base + "/t/1/posts.json?post_number=41&asc=true": json_response(base, {
            "post_stream": {"posts": [p for p in posts if p["post_number"] > 42][:20]}}),
        base + "/t/1/posts.json?post_number=42": json_response(base, {
            "post_stream": {"posts": [p for p in posts if 32 <= p["post_number"] < 52]}}),
    })
    forum = site(name="range", engine="discourse", base_url=base, label="Range")
    forum.engine_obj = discourse.discourse
    output = io.StringIO()
    action_runner(forum, forum.all_actions["thread"], {"id": 1, "from_": 42, "to": 60},
                  Options(json=True), stream=output)
    got = json.loads(output.getvalue())["data"]["posts"]
    assert [p["number"] for p in got if "number" in p] == list(range(42, 61))
    assert len(fake_http.requests) == 3  # topic plus one asc page and one near request


def test_from_n_costs_no_extra_request_when_n_is_on_the_first_page(action_runner, fake_http):
    posts = [{"id": 1000 + n, "post_number": n} for n in range(1, 80)]
    base = "https://forum.invalid"
    fake_http.routes.update({
        base + "/t/1.json": json_response(base, {"id": 1, "highest_post_number": 79,
                                                  "post_stream": {"posts": posts[:20]}}),
        base + "/t/1/posts.json?post_number=41&asc=true": json_response(base, {
            "post_stream": {"posts": [p for p in posts if p["post_number"] > 41][:20]}}),
    })
    forum = site(name="range", engine="discourse", base_url=base, label="Range")
    forum.engine_obj = discourse.discourse
    output = io.StringIO()
    action_runner(forum, forum.all_actions["thread"], {"id": 1, "from_": 42, "to": 50},
                  Options(json=True), stream=output)
    got = json.loads(output.getvalue())["data"]["posts"]
    assert [p["number"] for p in got if "number" in p] == list(range(42, 51))
    assert [r.url for r in fake_http.requests] == [base + "/t/1.json",
                                                  base + "/t/1/posts.json?post_number=41&asc=true"]
