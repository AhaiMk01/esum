"""Miles Earn and Burn (meb) on the WordPress engine: its actions on its scrubbed responses, in tests/fixtures/ beside this file.
The parser tests are in the checkout's tests/test_parsers_wordpress.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from esum.core.registry import Registry
from esum.transport import Response

from conftest import json_response

FIX = Path(__file__).parent / "fixtures"
MEB = "https://milesearnandburn.com"


def fixture(name: str):
    text = (FIX / name).read_text(encoding="utf-8")
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


def test_meb_thread_by_index_php_permalink(fake_http, cli):
    fake_http.routes[wp(MEB, "posts", slug="tuesday-triple-29",
                        _fields="id,link,title,date_gmt,modified_gmt,content,comment_status")] = \
        json_response("x", [fixture("meb_post.json")])
    fake_http.routes[wp(MEB, "comments", post=6728, per_page=100, page=1, orderby="date_gmt", order="asc",
                        _fields="id,parent,author,author_name,date_gmt,content")] = \
        json_response("x", fixture("meb_comments.json"), headers={"x-wp-total": "4", "x-wp-totalpages": "1"})
    code, out, err = cli("meb", "thread", "--id", "https://milesearnandburn.com/index.php/2023/03/28/tuesday-triple-29/")
    assert code == 0, err
    assert "https://milesearnandburn.com/index.php/2023/03/28/tuesday-triple-29/ · 5 of 5 posts shown" in out
    assert "Published: 2023-03-28 · Updated: 2023-04-19 · Comments: closed" in out
    assert "[MEAB #0] (2023-03-28 10:00 · OP): 1. The Citi Shop Your Way Rewards Mastercard" in out
    assert "[Jeff #1] (2023-03-28 13:04): Today is also T-Mobile Tuesday" in out
    assert "[Jeff #2] (2023-03-29 12:37 · ↩#1): Edit: Code found" in out
    assert "[Kerberos #3] (2023-03-29 17:40): Love this blog!" in out
    assert "[Conan #4] (2023-03-29 21:20): Wen Slack Membership" in out
    assert "- https://www.lowesgiftcardbonus.com/ — cited by [MEAB #0]" in out


MEB_POST_FIELDS = "id,link,title,date_gmt,modified_gmt,content,comment_status"


def test_meb_thread_missing_id_names_the_post_and_how_to_find_one(fake_http, cli):
    url = wp(MEB, "posts/99999999", _fields=MEB_POST_FIELDS)
    fake_http.routes[url] = json_response(url, {"code": "rest_post_invalid_id", "message": "Invalid post ID.",
                                                "data": {"status": 404}}, status=404)
    code, out, err = cli("meb", "thread", "--id", "99999999")
    assert code == 1
    lines = out.splitlines()
    assert lines[0] == "error: no post with id 99999999 on https://milesearnandburn.com"
    assert "HttpError" not in out
    help_line = next(line for line in lines if line.startswith("help:"))
    assert "esum meb search --query <words>" in help_line and "esum meb recent" in help_line


@pytest.mark.parametrize("status, body", [
    (404, {"code": "rest_no_route", "message": "No route was found."}),
    (500, {"code": "internal_server_error"}),
])
def test_meb_thread_other_http_errors_keep_their_raw_message(fake_http, cli, status, body):
    url = wp(MEB, "posts/99999999", _fields=MEB_POST_FIELDS)
    fake_http.routes[url] = json_response(url, body, status=status)
    code, out, err = cli("meb", "thread", "--id", "99999999")
    assert code == 1
    assert out.splitlines()[0].startswith(f"error: HttpError: HTTP {status} from ")
    assert body["code"] in out


def test_meb_thread_missing_slug_names_how_to_find_one(fake_http, cli):
    fake_http.routes[wp(MEB, "posts", slug="no-such-post", _fields=MEB_POST_FIELDS)] = json_response("x", [])
    code, out, err = cli("meb", "thread", "--id", "no-such-post")
    assert code == 1
    lines = out.splitlines()
    assert lines[0] == "error: no post with slug 'no-such-post' on https://milesearnandburn.com"
    help_line = next(line for line in lines if line.startswith("help:"))
    assert "esum meb search --query <words>" in help_line and "esum meb recent" in help_line


MEB_LIST_FIELDS = "id,link,title,date_gmt,excerpt,categories,lang"


def test_meb_search_lists_roundups_with_ids_for_thread(fake_http, cli):
    url = wp(MEB, "posts", search="amex", orderby="relevance", per_page=3, page=1, _fields=MEB_LIST_FIELDS)
    fake_http.routes[url] = json_response(url, fixture("meb_search.json"), headers={"x-wp-total": "444"})
    code, out, err = cli("meb", "search", "--query", "amex", "--limit", "3")
    assert code == 0, err
    lines = out.splitlines()
    assert "count: 3 of 444 total" in lines and "posts[3]{id,date,title,excerpt}:" in lines
    assert any(line.startswith('  14538,2026-11-05,"Thursday: Kroger+mPerks, 40% Miles & Smiles Bonus, and Amex Offers"')
               for line in lines)
    assert "  Run `esum meb search --query amex --limit 3 --page 2` for the next page" in lines
    assert "  Run `esum meb thread --id <id>` to read a post with its comments" in lines


def test_meb_recent_in_a_category_by_slug(fake_http, cli):
    cat = wp(MEB, "categories", slug="ms-hints", _fields="id,slug")
    fake_http.routes[cat] = json_response(cat, [{"id": 12, "slug": "ms-hints"}])
    url = wp(MEB, "posts", orderby="date", per_page=3, page=1, categories=12, _fields=MEB_LIST_FIELDS)
    fake_http.routes[url] = json_response(url, fixture("meb_ms_hints.json"), headers={"x-wp-total": "250"})
    code, out, err = cli("meb", "recent", "--category", "ms-hints", "--limit", "3")
    assert code == 0, err
    lines = out.splitlines()
    assert "category: ms-hints" in lines and "count: 3 of 250 total" in lines
    rows = [line.split(",")[0].strip() for line in lines if line[:3].strip().isdigit()]
    assert rows == ["14454", "14421", "14398"]
    assert "  Run `esum meb recent --category ms-hints --limit 3 --page 2` for the next page" in lines


def test_meb_categories_filter_by_query(fake_http, cli):
    url = wp(MEB, "categories", per_page=100, page=1, orderby="count", order="desc",
             _fields="id,name,slug,count,parent")
    fake_http.routes[url] = json_response(url, fixture("meb_categories.json"), headers={"x-wp-totalpages": "1"})
    code, out, err = cli("meb", "categories", "--query", "hints")
    assert code == 0, err
    lines = out.splitlines()
    assert "categories[1]{id,slug,count,name}:" in lines and "  12,ms-hints,250,MS Hints" in lines
    assert "  Run `esum meb recent --category <slug>` to list a category's recent posts" in lines


def test_meb_unknown_category_names_the_site_and_how_to_list_slugs(fake_http, cli):
    url = wp(MEB, "categories", slug="not-a-category", _fields="id,slug")
    fake_http.routes[url] = json_response(url, [])
    code, out, err = cli("meb", "recent", "--category", "not-a-category")
    assert code == 2
    lines = out.splitlines()
    assert lines[0] == "error: no category with slug 'not-a-category' on https://milesearnandburn.com"
    assert "help: Run `esum meb categories` to list the category slugs" in lines


def test_meb_search_without_match_points_to_recent(fake_http, cli):
    url = wp(MEB, "posts", search="zzq", orderby="relevance", per_page=3, page=1, _fields=MEB_LIST_FIELDS)
    fake_http.routes[url] = json_response(url, [], headers={"x-wp-total": "0"})
    code, out, err = cli("meb", "search", "--query", "zzq", "--limit", "3")
    assert code == 0, err
    lines = out.splitlines()
    assert "posts: \"0 found: no posts match 'zzq'\"" in lines
    assert "  Run `esum meb recent` to list the newest posts instead" in lines
    assert not any("thread --id" in line for line in lines)


def test_meb_recent_past_the_last_page_points_to_page_one(fake_http, cli):
    url = wp(MEB, "posts", orderby="date", per_page=5, page=999, _fields=MEB_LIST_FIELDS)
    fake_http.routes[url] = json_response(url, {"code": "rest_post_invalid_page_number"}, status=400)
    code, out, err = cli("meb", "recent", "--page", "999", "--limit", "5")
    assert code == 0, err
    assert "  Run `esum meb recent --limit 5 --page 1` to start from the first page" in out.splitlines()


def test_meb_recent_empty_with_filters_points_to_recent_without_them(fake_http, cli):
    cat = wp(MEB, "categories", slug="ms-hints", _fields="id,slug")
    fake_http.routes[cat] = json_response(cat, [{"id": 7, "slug": "ms-hints"}])
    url = wp(MEB, "posts", orderby="date", per_page=20, page=1, after="2026-01-01T00:00:00+00:00",
             categories=7, _fields=MEB_LIST_FIELDS)
    fake_http.routes[url] = json_response(url, [], headers={"x-wp-total": "0"})
    code, out, err = cli("meb", "recent", "--since", "2026-01-01", "--category", "ms-hints")
    assert code == 0, err
    assert "  Run `esum meb recent` to list the newest posts without the filters" in out.splitlines()
