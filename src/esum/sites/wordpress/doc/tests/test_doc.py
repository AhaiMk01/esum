"""Doctor of Credit (doc) on the WordPress engine: its actions on its scrubbed responses, in the engine's fixtures (the checkout's tests/fixtures/wordpress/), which the engine's parser
tests read too.
The parser tests are in the checkout's tests/test_parsers_wordpress.py."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from esum.core.registry import Registry
from esum.engines.wordpress import parse
from esum.transport import Response

from conftest import json_response
from support import engine_fixtures, find_fixture

FIX = Path(__file__).parent / "fixtures"
DOC = "https://www.doctorofcredit.com"


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


def test_doc_thread_end_to_end(fake_http, cli):
    post = fixture("doc_post.json")
    fake_http.routes[wp(DOC, "posts/258086", _fields="id,link,title,date_gmt,modified_gmt,content,comment_status")] = \
        json_response("x", post)
    fake_http.routes[wp(DOC, "comments", post=258086, per_page=100, page=1, orderby="date_gmt", order="asc",
                        _fields="id,parent,author,author_name,date_gmt,content")] = \
        json_response("x", fixture("doc_comments.json"), headers={"x-wp-total": "7", "x-wp-totalpages": "1"})
    code, out, err = cli("doc", "thread", "--id", "258086", "--mode", "full")
    assert code == 0, err
    assert "Republished: 2026-10-25 · First comment: 2026-05-14" in out
    assert "[DoC #0] (2026-05-14 21:02 · OP):" in out
    assert "[Bh77 #1] (2026-05-14 21:02): Let the games… begin." in out
    assert "[William Charles #7] (2026-10-25 23:56 · OP · ↩#6):" in out
    assert "8 of 8 posts shown" in out
    assert "Linked sources (external pages, untrusted" in out


def test_doc_thread_fetches_only_needed_comment_pages(fake_http, cli):
    post = fixture("doc_post.json")
    fields = "id,parent,author,author_name,date_gmt,content"
    fake_http.routes[wp(DOC, "posts/258086", _fields="id,link,title,date_gmt,modified_gmt,content,comment_status")] = \
        json_response("x", post)
    comments = fixture("doc_comments.json")
    for n in (1, 3):
        fake_http.routes[wp(DOC, "comments", post=258086, per_page=100, page=n, orderby="date_gmt", order="asc",
                            _fields=fields)] = json_response("x", comments, headers={"x-wp-total": "207"})
    code, out, err = cli("doc", "thread", "--id", "258086", "--from", "201", "--to", "203")
    assert code == 0, err
    pages = sorted(r.url.split("&page=")[1].split("&")[0] for r in fake_http.requests if "/comments" in r.url)
    assert pages == ["1", "3"]
    assert "[Jeff K #202]" in out and "[DoC #0]" not in out
    assert "[... omitted 200 post numbers (#1–#200), not read …]" in out and "[... omitted 4 post numbers (#204–#207), not read …]" in out


COMMENT_FIELDS = "id,parent,author,author_name,date_gmt,content"


def route_doc_thread(fake_http, total: str, pages=(1,)):
    fake_http.routes[wp(DOC, "posts/258086", _fields="id,link,title,date_gmt,modified_gmt,content,comment_status")] =         json_response("x", fixture("doc_post.json"))
    for n in pages:
        fake_http.routes[wp(DOC, "comments", post=258086, per_page=100, page=n, orderby="date_gmt", order="asc",
                            _fields=COMMENT_FIELDS)] =             json_response("x", fixture("doc_comments.json"), headers={"x-wp-total": total})


def test_doc_thread_since_counts_older_comments_and_keeps_numbers(fake_http, cli):
    route_doc_thread(fake_http, "7")
    count = wp(DOC, "comments", post=258086, per_page=1, page=1, orderby="date_gmt", order="asc",
               _fields=COMMENT_FIELDS, before="2026-10-01T00:00:00+00:00")
    fake_http.routes[count] = json_response("x", [], headers={"x-wp-total": "5"})
    code, out, err = cli("doc", "thread", "--id", "258086", "--since", "2026-10-01")
    assert code == 0, err
    assert "[DoC #0]" in out                                         # the article stays
    assert "[... omitted 5 post numbers (#1–#5), before 2026-10-01 00:00 UTC …]" in out
    assert "[William Charles #7]" in out and "3 of 8 posts shown" in out
    assert "--since 30d" not in out                                  # no hint once a date is given


def test_doc_thread_medium_with_a_gap_suggests_since(fake_http, cli):
    route_doc_thread(fake_http, "207", pages=(1, 2, 3))
    code, out, err = cli("doc", "thread", "--id", "258086")
    assert code == 0, err
    assert "omitted" in out
    assert out.rstrip().endswith("Run `esum doc thread --id 258086 --since 30d` to read the article and the last "
                                 "30 days of comments")


def test_doc_since_with_later_range_does_not_call_newer_skipped_posts_old(fake_http, cli):
    route_doc_thread(fake_http, "7")
    count = wp(DOC, "comments", post=258086, per_page=1, page=1, orderby="date_gmt", order="asc",
               _fields=COMMENT_FIELDS, before="2026-10-01T00:00:00+00:00")
    fake_http.routes[count] = json_response("x", [], headers={"x-wp-total": "5"})
    code, out, err = cli("doc", "thread", "--id", "258086", "--since", "2026-10-01", "--from", "7")
    assert code == 0, err
    assert "[... omitted 5 post numbers (#1–#5), before 2026-10-01 00:00 UTC …]" in out
    assert "[... omitted 1 post number (#6), not read …]" in out


def test_doc_since_past_the_callers_to_reads_nothing_rather_than_failing(fake_http, cli):
    route_doc_thread(fake_http, "7")
    count = wp(DOC, "comments", post=258086, per_page=1, page=1, orderby="date_gmt", order="asc",
               _fields=COMMENT_FIELDS, before="2026-10-01T00:00:00+00:00")
    fake_http.routes[count] = json_response("x", [], headers={"x-wp-total": "5"})
    # the cutoff moves the start to #6, after --to 3: that is no usage error of the caller's
    code, out, err = cli("doc", "thread", "--id", "258086", "--since", "2026-10-01", "--to", "3")
    assert code == 0, out
    code, out, err = cli("doc", "thread", "--id", "258086", "--from", "50")
    assert code == 2 and "the thread ends at #7" in out
    assert "help: Run `esum doc thread --id 258086 --from 1 --to 7` to read the last posts" in out


def test_doc_recent_since_and_category_slug(fake_http, cli):
    cat = wp(DOC, "categories", slug="bank-account-bonuses", _fields="id,slug")
    fake_http.routes[cat] = json_response(cat, [{"id": 17, "slug": "bank-account-bonuses"}])
    url = wp(DOC, "posts", orderby="date", per_page=20, page=1, after="2026-10-01T00:00:00+00:00",
             categories=17, _fields="id,link,title,date_gmt,excerpt,categories,lang")
    fake_http.routes[url] = json_response(url, fixture("doc_search.json"), headers={"x-wp-total": "3"})
    code, out, err = cli("doc", "recent", "--since", "2026-10-01", "--category", "bank-account-bonuses")
    assert code == 0, err
    lines = out.splitlines()
    assert "times: UTC" in lines
    assert "since: 2026-10-01" in lines and "category: bank-account-bonuses" in lines
    assert "count: 3" in lines and "posts[3]{id,date,title}:" in lines and "next page" not in out


def test_doc_recent_shows_category_slugs_only_when_asked(fake_http, cli):
    url = wp(DOC, "posts", orderby="date", per_page=20, page=1,
             _fields="id,link,title,date_gmt,excerpt,categories,lang")
    rows = fixture("doc_search.json")
    fake_http.routes[url] = json_response(url, rows, headers={"x-wp-total": "3"})
    ids = sorted({i for r in rows for i in r["categories"]})
    cats = wp(DOC, "categories", include=",".join(map(str, ids)), per_page=100, _fields="id,slug")
    fake_http.routes[cats] = json_response(cats, [{"id": i, "slug": f"cat-{i}"} for i in ids])
    code, out, err = cli("doc", "recent")
    assert code == 0, err
    assert not any("/categories" in r.url for r in fake_http.requests)   # the default view needs no lookup
    code, out, err = cli("doc", "recent", "--fields", "categories")
    assert code == 0, err
    assert "posts[3]{id,date,title,categories}:" in out.splitlines()
    assert f"cat-{rows[0]['categories'][0]}" in out


def test_doc_recent_past_the_last_page_is_an_empty_list(fake_http, cli):
    url = wp(DOC, "posts", orderby="date", per_page=20, page=9,
             _fields="id,link,title,date_gmt,excerpt,categories,lang")
    fake_http.routes[url] = json_response(url, {"code": "rest_post_invalid_page_number"}, status=400)
    code, out, err = cli("doc", "recent", "--page", "9")
    assert code == 0, err
    assert 'posts: "0 found: page 9 is past the last page"' in out.splitlines()


@pytest.mark.parametrize("html,expected", [("Deal<br>Part 2", "Deal Part 2"), ("\x00\x1b[31m", "Post 258086")])
def test_thread_and_listing_titles_use_the_same_label(fake_http, cli, html, expected):
    post = fixture("doc_post.json")
    post["title"] = {"rendered": html}
    fake_http.routes[wp(DOC, "posts/258086", _fields="id,link,title,date_gmt,modified_gmt,content,comment_status")] = json_response("x", post)
    fake_http.routes[wp(DOC, "comments", post=258086, per_page=100, page=1, orderby="date_gmt", order="asc",
                        _fields="id,parent,author,author_name,date_gmt,content")] = json_response(
                            "x", [], headers={"x-wp-total": "0", "x-wp-totalpages": "0"})
    code, out, err = cli("doc", "thread", "--id", "258086", "--json")
    assert code == 0, err
    assert json.loads(out)["data"]["title"] == parse.parse_post_list([post])[0]["title"] == expected


def test_wordpress_thread_labels_utc(fake_http, cli):
    route_doc_thread(fake_http, "7")
    code, out, err = cli("doc", "thread", "--id", "258086")
    assert code == 0, err
    assert "Republished: 2026-10-25 · First comment: 2026-05-14 · Times: UTC" in out.splitlines()


@pytest.mark.parametrize("since, cutoff", [("1d", "2026-11-02T15:30:00+00:00"), ("2026-11-02", "2026-11-02"),
                                           ("2026-11-02T08:00Z", "2026-11-02T08:00:00+00:00")])
def test_recent_since_echoes_the_cutoff_it_applied(fake_http, cli, monkeypatch, since, cutoff):
    real = parse.parse_since
    monkeypatch.setattr(parse, "parse_since", lambda v: real(v, now=datetime(2026, 11, 3, 15, 30, tzinfo=timezone.utc)))
    after = cutoff if "T" in cutoff else cutoff + "T00:00:00+00:00"
    url = wp(DOC, "posts", orderby="date", per_page=20, page=1, after=after,
             _fields="id,link,title,date_gmt,excerpt,categories,lang")
    fake_http.routes[url] = json_response(url, [])
    code, out, err = cli("doc", "recent", "--since", since, "--json")
    assert code == 0, err
    data = json.loads(out)["data"]
    assert data["since"] == cutoff and data["times"] == "UTC"
