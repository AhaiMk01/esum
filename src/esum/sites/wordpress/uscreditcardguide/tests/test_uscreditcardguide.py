"""US Credit Card Guide (uscreditcardguide) on the WordPress engine: its actions on its scrubbed responses, in tests/fixtures/ beside this file, and those the engine's parser tests read too in the engine's
fixtures (the checkout's tests/fixtures/wordpress/).
The parser tests are in the checkout's tests/test_parsers_wordpress.py."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import pytest

from esum.core.registry import Registry
from esum.transport import Response

from conftest import json_response
from support import engine_fixtures, find_fixture

FIX = Path(__file__).parent / "fixtures"
USCCG = "https://www.uscreditcardguide.com"
USCCG_POST_URL = "https://www.uscreditcardguide.com/citi-aadvantage-executive/"
MEB_LIST_FIELDS = "id,link,title,date_gmt,excerpt,categories,lang"
FAKE_KEY = "TESTONLYnotARealDisqusKey0000000"
BUNDLE = "https://c.disquscdn.com/embedv2/latest/embedv2.js"


def fixture(name: str):
    text = find_fixture(name, FIX, engine_fixtures("wordpress")).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


@pytest.fixture
def registry() -> Registry:
    return Registry.discover(custom_root=None)


@pytest.fixture
def cli(capsys, registry, action_runner):
    def _run(*argv: str):
        code = action_runner.cli(argv, registry=registry)
        out = capsys.readouterr()
        return code, out.out, out.err

    return _run


def html_response(url: str, text: str) -> Response:
    return Response(200, url, {"content-type": "text/html; charset=utf-8"}, text.encode("utf-8"))


def wp(base: str, path: str, **params) -> str:
    from urllib.parse import urlencode
    return f"{base}/wp-json/wp/v2/{path}" + (f"?{urlencode(params)}" if params else "")


# --- whole actions on a fake backend -----------------------------------------------------------------


def test_usccg_thread_reads_disqus_with_unreachable_middle(fake_http, cli):
    fake_http.routes[wp(USCCG, "posts", slug="citi-aadvantage-executive",
                        _fields="id,link,title,date_gmt,modified_gmt,content,comment_status")] = \
        json_response("x", [fixture("usccg_post.json")])
    fake_http.routes[USCCG_POST_URL] = html_response(USCCG_POST_URL, fixture("usccg_post_page.html"))
    ident = "1610 http://uscreditcardguide.com/?p=1610"
    for order in ("asc", "desc"):
        url = (f"https://disqus.com/embed/comments/?base=default&f=creditcards-physixfan&t_i={quote(ident)}"
               f"&t_u={quote(USCCG_POST_URL, safe='')}&s_o={order}")
        fake_http.routes[url] = html_response(url, fixture(f"usccg_disqus_{order}.html"))
    code, out, err = cli("uscreditcardguide", "thread", "--id", USCCG_POST_URL, "--mode", "full")
    assert code == 0, err
    assert "Times: UTC" in out
    assert "Comments from Disqus" in out and "Republished:" in out
    assert "[yangzi #1]" in out and "[ThunderFat #2] (2015-06-11 23:29 · ↩#1)" in out
    assert "· OP" in out.split("[physixfan", 1)[1].split("\n", 1)[0]
    assert "Disqus serves only its oldest and newest 50 threads" in out
    assert "#120]" in out   # the newest comment keeps the thread's last number


def test_usccg_thread_since_after_the_newest_comment_omits_them_all(fake_http, cli):
    fake_http.routes[wp(USCCG, "posts", slug="citi-aadvantage-executive",
                        _fields="id,link,title,date_gmt,modified_gmt,content,comment_status")] =         json_response("x", [fixture("usccg_post.json")])
    fake_http.routes[USCCG_POST_URL] = html_response(USCCG_POST_URL, fixture("usccg_post_page.html"))
    ident = "1610 http://uscreditcardguide.com/?p=1610"
    for order in ("asc", "desc"):
        url = (f"https://disqus.com/embed/comments/?base=default&f=creditcards-physixfan&t_i={quote(ident)}"
               f"&t_u={quote(USCCG_POST_URL, safe='')}&s_o={order}")
        fake_http.routes[url] = html_response(url, fixture(f"usccg_disqus_{order}.html"))
    code, out, err = cli("uscreditcardguide", "thread", "--id", USCCG_POST_URL, "--since", "2026-10-01")
    assert code == 0, err
    assert "[... omitted 120 post numbers (#1–#120), before 2026-10-01 00:00 UTC …]" in out and "1 of 121 posts shown" in out


DISQUS_IDENT = "1610 http://uscreditcardguide.com/?p=1610"


def api_url(order: str, cursor: str | None = None, key: str = FAKE_KEY) -> str:
    from urllib.parse import urlencode
    params = {"api_key": key, "thread": "1", "limit": 100, "order": order}
    return "https://disqus.com/api/3.0/posts/list.json?" + urlencode({**params, **({"cursor": cursor} if cursor else {})})


def posted_on(n: int) -> datetime:
    from datetime import timedelta
    return datetime(2020, 1, 1) + timedelta(days=n)


def api_page(numbers, more: str | None) -> dict:
    posts = [{"id": str(1000 + n), "parent": None, "likes": 0, "isDeleted": False, "isSpam": False,
              "createdAt": posted_on(n).strftime("%Y-%m-%dT%H:%M:%S"),
              "message": f"<p>comment {n}</p>", "author": {"name": f"user{n}"}} for n in numbers]
    return {"code": 0, "response": posts, "cursor": {"hasNext": more is not None, "next": more}}


def route_disqus_api(fake_http, key: str = FAKE_KEY, bundle_js: str | None = None):
    fake_http.routes[wp(USCCG, "posts", slug="citi-aadvantage-executive",
                        _fields="id,link,title,date_gmt,modified_gmt,content,comment_status")] = \
        json_response("x", [fixture("usccg_post.json")])
    fake_http.routes[USCCG_POST_URL] = html_response(USCCG_POST_URL, fixture("usccg_post_page.html"))
    script = f"<script>var a = document.createElement('script'); a.src = '{BUNDLE}';</script>"
    for order in ("asc", "desc"):
        url = (f"https://disqus.com/embed/comments/?base=default&f=creditcards-physixfan&t_i={quote(DISQUS_IDENT)}"
               f"&t_u={quote(USCCG_POST_URL, safe='')}&s_o={order}")
        fake_http.routes[url] = html_response(url, fixture(f"usccg_disqus_{order}.html") + script)
    bundle_js = f'x={{VITE_API_URL:"https://disqus.com/api/3.0",VITE_API_KEY:"{key}"}}' if bundle_js is None else bundle_js
    if bundle_js:
        fake_http.routes[BUNDLE] = Response(200, BUNDLE, {"content-type": "application/javascript"}, bundle_js.encode())


def test_usccg_thread_pages_the_disqus_api_with_no_gap(fake_http, cli):
    route_disqus_api(fake_http)
    fake_http.routes[api_url("asc")] = json_response("x", api_page(range(1, 101), "c2"))
    fake_http.routes[api_url("asc", "c2")] = json_response("x", api_page(range(101, 121), None))
    code, out, err = cli("uscreditcardguide", "thread", "--id", USCCG_POST_URL, "--mode", "full")
    assert code == 0, err
    assert "121 of 121 posts shown" in out and "omitted" not in out
    assert "[user1 #1]" in out and "[user120 #120]" in out
    assert FAKE_KEY not in out + err


def test_usccg_thread_since_walks_the_api_newest_first_and_stops(fake_http, cli):
    route_disqus_api(fake_http)
    fake_http.routes[api_url("desc")] = json_response("x", api_page(range(120, 20, -1), "c2"))
    code, out, err = cli("uscreditcardguide", "thread", "--id", USCCG_POST_URL, "--since", f"{posted_on(91):%Y-%m-%d}")
    assert code == 0, err
    assert "[user120 #120]" in out and "[user92 #92]" in out and "[user91 #91]" not in out
    assert "Republished:" in out and "First comment: 2015-" in out
    assert not [r for r in fake_http.requests if "cursor=c2" in r.url]
    assert FAKE_KEY not in out + err


def test_usccg_thread_falls_back_to_the_embed_when_the_api_refuses_the_key(fake_http, cli):
    route_disqus_api(fake_http)
    fake_http.routes[api_url("asc")] = json_response(api_url("asc"), {"code": 5, "response": "Invalid API key"}, status=403)
    code, out, err = cli("uscreditcardguide", "thread", "--id", USCCG_POST_URL, "--mode", "full")
    assert code == 0, err
    assert "Disqus serves only its oldest and newest 50 threads without an API key, and the API route failed: HTTP 403" in out
    assert "Invalid API key" in out and "<api_key>" in out
    assert FAKE_KEY not in out + err
    assert "#120]" in out


def test_usccg_thread_falls_back_when_the_script_has_no_key(fake_http, cli):
    route_disqus_api(fake_http, bundle_js="no key in this bundle")
    code, out, err = cli("uscreditcardguide", "thread", "--id", USCCG_POST_URL, "--mode", "full")
    assert code == 0, err
    assert "the API route failed: no public API key in the Disqus embed script" in out
    assert not [r for r in fake_http.requests if "api/3.0" in r.url]


def test_usccg_thread_numbers_newest_first_pages_from_the_end(fake_http, cli):
    route_disqus_api(fake_http)
    fake_http.routes[api_url("desc")] = json_response(api_url("desc"), api_page(range(120, 20, -1), "c2"))
    code, out, err = cli("uscreditcardguide", "thread", "--id", USCCG_POST_URL, "--from", "115", "--to", "120")
    assert code == 0, err
    assert "[user115 #115]" in out and "[user120 #120]" in out and "[user114" not in out
    assert "[... omitted 114 post numbers (#1–#114), not read …]" in out
    assert "Republished:" in out and "First comment: 2015-" in out
    assert not [r for r in fake_http.requests if "order=asc" in r.url]


def test_usccg_thread_scrubs_a_key_that_the_error_message_cuts_short(fake_http, cli):
    long_key = "TESTONLY" + "x" * 200
    route_disqus_api(fake_http, key=long_key)
    fake_http.routes[api_url("asc", key=long_key)] = json_response(api_url("asc", key=long_key), {}, status=403)
    code, out, err = cli("uscreditcardguide", "thread", "--id", USCCG_POST_URL, "--mode", "full")
    assert code == 0, err
    assert "api_key=<api_key>" in out and "TESTONLYxxxx" not in out + err


def test_usccg_thread_falls_back_when_the_api_answers_200_with_an_error(fake_http, cli):
    route_disqus_api(fake_http)
    fake_http.routes[api_url("asc")] = json_response(api_url("asc"), {"code": 2, "response": "Invalid argument"})
    code, out, err = cli("uscreditcardguide", "thread", "--id", USCCG_POST_URL, "--mode", "full")
    assert code == 0, err
    assert "the API route failed: ValueError: Disqus API answered {'code': 2, 'response': 'Invalid argument'}" in out
    assert "#120]" in out


def test_usccg_search_defaults_to_chinese_edition(fake_http, cli):
    url = wp(USCCG, "posts", search="chase sapphire", orderby="relevance", per_page=3, page=1,
             _fields="id,link,title,date_gmt,excerpt,categories,lang", lang="zh")
    fake_http.routes[url] = json_response(url, fixture("usccg_search.json"), headers={"x-wp-total": "262"})
    code, out, err = cli("uscreditcardguide", "search", "--query", "chase sapphire", "--limit", "3")
    assert code == 0, err
    lines = out.splitlines()
    assert "times: UTC" in lines
    assert "query: chase sapphire" in lines and "lang: zh" in lines and "count: 3 of 262 total" in lines
    assert "posts[3]{id,date,title,excerpt}:" in lines
    assert any(line.startswith("  20925,2026-09-17,Chase Sapphire Reserve (CSR)") for line in lines)
    assert ("  Run `esum uscreditcardguide search --query 'chase sapphire' --limit 3 --page 2` "
            "for the next page") in lines
    assert "  Run `esum uscreditcardguide thread --id <id>` to read a post with its comments" in lines


@pytest.mark.parametrize("lang, api", [("en", {"lang": "en"}), ("all", {})])
def test_usccg_search_without_match_keeps_the_language_for_recent(fake_http, cli, lang, api):
    url = wp(USCCG, "posts", search="zzq", orderby="relevance", per_page=20, page=1,
             _fields=MEB_LIST_FIELDS, **api)
    fake_http.routes[url] = json_response(url, [], headers={"x-wp-total": "0"})
    code, out, err = cli("uscreditcardguide", "search", "--query", "zzq", "--lang", lang)
    assert code == 0, err
    assert f"  Run `esum uscreditcardguide recent --lang {lang}` to list the newest posts instead" in out.splitlines()


def test_usccg_categories_listing(fake_http, cli):
    url = wp(USCCG, "categories", per_page=100, page=1, orderby="count", order="desc",
             _fields="id,name,slug,count,parent")
    fake_http.routes[url] = json_response(url, fixture("usccg_categories.json"))
    code, out, err = cli("uscreditcardguide", "categories")
    assert code == 0, err
    lines = out.splitlines()
    n = len(fixture("usccg_categories.json"))
    assert f"categories[{n}]{{id,slug,count,name}}:" in lines
    assert "  Run `esum uscreditcardguide recent --category <slug>` to list a category's recent posts" in lines
    code, out, err = cli("uscreditcardguide", "categories", "--query", "zzz-none")
    assert code == 0, err
    assert "categories: \"0 found: no category name or slug contains 'zzz-none'\"" in out
