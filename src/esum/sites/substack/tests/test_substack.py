"""Offline tests for the Substack module on saved publication API responses."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from esum.core.registry import Registry
from esum.sites.substack.parse import (Ref, article_from, check_cursor, post_ref, publication_origin, publication_ref,
                                       split_post, thread_from)
from esum.transport import Response
from esum import html_to_text
from esum.sites.substack import parse as sub
from html import escape

FIX = Path(__file__).parent / "fixtures"
ORIGIN = "https://www.noahpinion.blog"


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("given, origin", [
    ("noahpinion", "https://noahpinion.substack.com"),
    ("Noahpinion.substack.com", "https://noahpinion.substack.com"),
    ("www.noahpinion.blog", ORIGIN),
    ("https://www.noahpinion.blog/p/some-post?utm=x", ORIGIN),
])
def test_publication_origin(given, origin):
    assert publication_origin(given) == origin


def test_split_post():
    assert split_post(f"{ORIGIN}/p/france-and-the-limits-of-liberalism", None) == (
        ORIGIN, "france-and-the-limits-of-liberalism")
    assert split_post("some-slug", "noahpinion") == ("https://noahpinion.substack.com", "some-slug")
    with pytest.raises(ValueError, match="--publication"):
        split_post("some-slug", None)
    with pytest.raises(ValueError, match="/p/<slug>"):
        split_post(f"{ORIGIN}/about", None)


@pytest.mark.parametrize("given, ref", [
    ("@noahsmith", Ref(handle="noahsmith")),
    ("https://substack.com/@noahsmith", Ref(handle="noahsmith")),
    ("https://open.substack.com/pub/noahpinion/p/x?r=1", Ref(origin="https://noahpinion.substack.com")),
])
def test_publication_ref_reads_substack_link_shapes(given, ref):
    assert publication_ref(given) == ref


def test_reader_pages_are_not_publications():
    with pytest.raises(ValueError, match="reader app, not a publication"):
        publication_ref("https://substack.com/home")
    with pytest.raises(ValueError, match="profile, not a publication"):
        publication_origin("@noahsmith")


@pytest.mark.parametrize("given, expected", [
    ("https://substack.com/home/post/p-221583894", (Ref(post_id=221583894), None)),
    ("https://substack.com/@noahsmith/p-221583894?utm_source=share", (Ref(post_id=221583894), None)),
    ("https://open.substack.com/pub/noahpinion/p/some-post?r=abc",
     (Ref(origin="https://noahpinion.substack.com"), "some-post")),
    ("noahpinion.substack.com/p/some-post/comments", (Ref(origin="https://noahpinion.substack.com"), "some-post")),
])
def test_post_ref_reads_share_and_open_links(given, expected):
    assert post_ref(given, None) == expected


def test_check_cursor_refuses_foreign_and_garbled_cursors():
    check_cursor("eyJvZmZzZXQiOjN9", "offset")                               # {"offset":3}
    check_cursor("eyJwb3N0X2RhdGUiOiIyMDI2LTExLTA1In0=", "post_date")       # {"post_date":"2026-11-05"}
    with pytest.raises(ValueError, match="different listing"):
        check_cursor("eyJvZmZzZXQiOjN9", "post_date")
    with pytest.raises(ValueError, match="not a cursor Substack issued"):
        check_cursor("garbage!!!", "offset")


def test_archive_rows_mark_paid_posts():
    rows = [article_from(p, ORIGIN) for p in load("archive.json")]
    assert [r.paid for r in rows] == [True, False, True]
    r = rows[1]
    assert r.title == "France and the limits of liberalism" and r.author == "Noah Smith"
    assert r.comments == 80 and r.likes > 0 and r.words > 0
    assert r.url == f"{ORIGIN}/p/france-and-the-limits-of-liberalism"


def test_thread_orders_comments_by_time_with_reply_links():
    th = thread_from(load("post_france.json"), load("comments_france.json")["comments"], ORIGIN)
    assert th.title == "France and the limits of liberalism" and not th.preview
    op = th.posts[0]
    assert op.is_op and op.user == "Noah Smith" and op.number == 1
    assert "[image: " in op.text and "substackcdn.com" in op.text
    assert "Photo by Jorge Jaramillo" in op.text
    comments = th.posts[1:]
    assert [p.number for p in th.posts] == list(range(1, len(th.posts) + 1))
    dates = [p.date for p in comments]
    assert dates == sorted(dates)
    replies = [p for p in comments if p.reply_to]
    assert replies and all(1 < p.reply_to < p.number for p in replies)
    assert all(p.url.startswith(f"{ORIGIN}/p/france-and-the-limits-of-liberalism/comment/") for p in comments)


def test_paid_post_without_readable_comments_says_so():
    post = dict(load("post_france.json"), audience="only_paid", comment_count=72)
    th = thread_from(post, [], ORIGIN)
    assert th.preview and len(th.posts) == 1
    assert "[paid post: only the free preview is shown]" in th.posts[0].text
    assert "[72 comments on the site are not readable from this session (paid or hidden)]" in th.posts[0].text


def test_deleted_comment_kept_only_with_replies():
    comments = [
        {"id": 1, "name": "a", "body": None, "date": "2026-01-01T00:00:00Z", "children": [
            {"id": 2, "name": "b", "body": "reply", "date": "2026-01-01T01:00:00Z", "children": []}]},
        {"id": 3, "name": "c", "body": None, "date": "2026-01-01T02:00:00Z", "children": []}]
    th = thread_from(dict(load("post_france.json"), comment_count=3), comments, ORIGIN)
    assert [(p.number, p.user, p.reply_to) for p in th.posts[1:]] == [(2, "[deleted]", None), (3, "b", 2)]


# --- whole actions on a fake T1 backend --------------------------------------------------------------

NEXT_CURSOR = "eyJvZmZzZXQiOjN9"   # base64 of {"offset":3}, as Substack sends for `top`


READER_ROUTES = {
    ("substack.com", "/api/v1/posts/by-id/221583894"):
        (200, {"post": {"id": 221583894, "canonical_url": f"{ORIGIN}/p/france-and-the-limits-of-liberalism"}}),
    ("substack.com", "/api/v1/user/noahsmith/public_profile"):
        (200, {"primaryPublication": {"subdomain": "noahpinion", "custom_domain": "www.noahpinion.blog"}}),
    ("substack.com", "/api/v1/user/nobody/public_profile"): (404, {"error": "profile not found"}),
}


class PathBackend:
    def __init__(self):
        self.requests = []

    def send(self, request, *, before_send=lambda: None):
        before_send()
        self.requests.append(request)
        u = urlsplit(request.url)
        routes = {
            "/api/v1/archive": "archive.json",
            "/api/v1/posts/france-and-the-limits-of-liberalism": "post_france.json",
            "/api/v1/post/221583894/comments": "comments_france.json",
        }
        if (u.hostname, u.path) in READER_ROUTES:
            status, body = READER_ROUTES[(u.hostname, u.path)]
            return Response(status, request.url, {"content-type": "application/json"}, json.dumps(body).encode())
        if u.hostname == "ghost.substack.com":   # a <name>.substack.com with no publication: empty 404
            return Response(404, request.url, {}, b"")
        name = routes.get(u.path) if u.hostname == "www.noahpinion.blog" else None
        if name is None:
            return Response(404, request.url, {}, b"not found")
        data = load(name)
        if "search" in parse_qs(u.query):   # a search comes back in relevance order
            data = [data[1], data[2], data[0]]
        if name == "archive.json":   # the API pages with an opaque cursor (null on the last page)
            data = {"posts": data, "nextCursor": None if "cursor" in parse_qs(u.query) else NEXT_CURSOR}
        return Response(200, request.url, {"content-type": "application/json"},
                        json.dumps(data, ensure_ascii=False).encode())

    def close(self):
        pass


@pytest.fixture
def run(action_runner, capsys):
    backend = PathBackend()
    action_runner.backend = backend
    reg = Registry.discover(custom_root=None)

    def _run(*argv):
        code = action_runner.cli(["substack", *argv], registry=reg)
        return code, capsys.readouterr().out, backend

    return _run


def test_archive_action(run):
    code, out, backend = run("archive", "--publication", "www.noahpinion.blog", "--sort", "top", "--limit", "3")
    lines = out.splitlines()
    assert code == 0 and f'publication: "{ORIGIN}"' in lines and "sort: top" in lines
    assert "posts[3]{slug,title,date,comments}:" in lines
    assert any(re.fullmatch(r"  france-and-the-limits-of-liberalism,France and the limits of liberalism,"
                            r"\"?2026-10-22 \d\d:\d\d\"?,80", line) for line in lines)
    assert ("  Run `esum substack archive --publication www.noahpinion.blog --sort top "
            f"--cursor {NEXT_CURSOR} --limit 3` for the next page") in lines
    assert f"  Run `esum substack thread --post <slug> --publication {ORIGIN}` to read a post with its comments" in lines
    code, out, _ = run("archive", "--publication", "www.noahpinion.blog", "--limit", "3", "--fields", "paid", "--json")
    assert code == 0 and [p["paid"] for p in json.loads(out)["data"]["posts"]] == [True, False, True]
    q = parse_qs(urlsplit(backend.requests[0].url).query)
    assert q == {"sort": ["top"], "limit": ["3"]}   # no `offset`: the API rejects it (HTTP 400)


def test_archive_cursor_pages_to_the_end(run):
    code, out, backend = run("archive", "--publication", "www.noahpinion.blog", "--sort", "top", "--cursor", NEXT_CURSOR)
    assert code == 0 and parse_qs(urlsplit(backend.requests[0].url).query)["cursor"] == [NEXT_CURSOR]
    assert f"cursor: {NEXT_CURSOR}" in out.splitlines() and "for the next page" not in out


@pytest.mark.parametrize("limit", ["0", "26"])
def test_archive_limit_beyond_the_api_is_a_usage_error(run, limit):
    code, out, backend = run("archive", "--publication", "www.noahpinion.blog", "--limit", limit)
    assert code == 2 and "--limit: must be 1 to 25" in out and not backend.requests


def test_search_action_sends_query(run):
    code, out, backend = run("search", "--publication", ORIGIN, "--query", "france")
    assert code == 0 and "query: france" in out.splitlines()
    assert parse_qs(urlsplit(backend.requests[0].url).query)["search"] == ["france"]


def test_thread_action(run):
    code, out, backend = run("thread", "--post", f"{ORIGIN}/p/france-and-the-limits-of-liberalism", "--to", "3")
    assert code == 0 and "# France and the limits of liberalism" in out
    assert "[Noah Smith #1]" in out and "OP" in out
    assert parse_qs(urlsplit(backend.requests[1].url).query)["all_comments"] == ["true"]


@pytest.mark.parametrize("sort, key, expected", [
    ("new", "date", ["the-internet-breaks", "france-and-the-limits-of-liberalism", "you-can-see-everything-and-the-nature"]),
    ("top", "likes", ["france-and-the-limits-of-liberalism", "the-internet-breaks", "you-can-see-everything-and-the-nature"]),
])
def test_search_sorts_the_returned_matches(run, sort, key, expected):
    code, out, backend = run("search", "--publication", ORIGIN, "--query", "france", "--sort", sort, "--json")
    assert code == 0
    query = parse_qs(urlsplit(backend.requests[0].url).query)
    # the archive's `top` order ignores the search, so a search always asks for the default order
    assert query["search"] == ["france"] and query["sort"] == ["new"]
    data = json.loads(out)["data"]
    assert [r["slug"] for r in data["posts"]] == expected
    assert data["sort"] == sort and data["sort_scope"] == "returned search page"


def test_unknown_publication_help_names_archive(run):
    code, out, _ = run("archive", "--publication", "missing")
    assert code == 1
    help_line = next(line for line in out.splitlines() if line.startswith("help:"))
    assert "no search across publications" in help_line and "`esum substack archive --publication <domain>`" in help_line
    assert "check the id or name" not in out


def test_missing_post_help_names_the_slug_and_archive(run):
    code, out, _ = run("thread", "--post", f"{ORIGIN}/p/no-such-post")
    assert code == 1
    help_line = next(line for line in out.splitlines() if line.startswith("help:"))
    assert "post's slug" in help_line and "archive --publication" in help_line


@pytest.mark.parametrize("status", [200, 404])
def test_a_non_substack_page_is_a_one_line_error(action_runner, capsys, status):
    class HTMLBackend:
        def send(self, request, *, before_send):
            before_send()
            return Response(status, request.url, {"content-type": "text/html"},
                            b"<!doctype html><html><body>" + b"irrelevant " * 100 + b"</body></html>")

        def close(self):
            pass

    action_runner.backend = HTMLBackend()
    code = action_runner.cli(["substack", "thread", "--post", "https://example.com/p/foo"],
                             registry=Registry.discover(custom_root=None))
    out = capsys.readouterr().out
    assert code == 1
    error = next(line for line in out.splitlines() if line.startswith("error:"))
    assert "html" not in out.lower() and "irrelevant" not in out
    assert "a web page" in error and "https://example.com/api/v1/posts/foo" in error and len(error) < 200
    assert "across publications" in out


def test_archive_refuses_a_cursor_from_another_listing_before_any_request(run):
    code, out, backend = run("archive", "--publication", "www.noahpinion.blog", "--cursor", NEXT_CURSOR)
    assert code == 2 and "different listing" in out and not backend.requests


def test_archive_by_author_handle_resolves_their_publication(run):
    code, out, backend = run("archive", "--publication", "https://substack.com/@noahsmith", "--limit", "3")
    assert code == 0 and f'publication: "{ORIGIN}"' in out.splitlines()
    assert [urlsplit(r.url).hostname for r in backend.requests] == ["substack.com", "www.noahpinion.blog"]
    code, out, _ = run("archive", "--publication", "@nobody")
    assert code == 1 and "substack.com/@nobody has no profile" in out


def test_archive_short_page_says_more_may_follow(run):
    code, out, _ = run("archive", "--publication", "www.noahpinion.blog", "--limit", "10")
    assert code == 0 and "the next page may still hold more" in out


def test_archive_on_a_missing_substack_name_says_so(run):
    code, out, _ = run("archive", "--publication", "ghost")
    assert code == 1 and "no Substack publication at ghost.substack.com" in out


def test_thread_from_an_app_share_link(run):
    code, out, backend = run("thread", "--post", "https://substack.com/home/post/p-221583894", "--to", "2")
    assert code == 0 and "# France and the limits of liberalism" in out
    assert urlsplit(backend.requests[0].url).path == "/api/v1/posts/by-id/221583894"


# --- C batch: date windows, --sort comments, --root ---------------------------------------------------

def _cursor_of(request) -> dict:
    raw = parse_qs(urlsplit(request.url).query)["cursor"][0]
    return json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))


def test_archive_window_keeps_the_posts_between_since_and_until(run):
    code, out, backend = run("archive", "--publication", ORIGIN, "--since", "2026-10-22", "--until", "2026-10-24", "--json")
    data = json.loads(out)["data"]
    assert code == 0 and [p["slug"] for p in data["posts"]] == ["france-and-the-limits-of-liberalism"]
    assert data["window"] == "2026-10-22 to 2026-10-24" and data["total"] == 1 and data["more"] is None
    # a bare --until date covers that whole day: the walk starts at the next midnight
    assert _cursor_of(backend.requests[0])["post_date"].startswith("2026-10-25T00:00:00")


def test_archive_sort_comments_orders_the_window(run):
    code, out, _ = run("archive", "--publication", ORIGIN, "--since", "2026-10-01", "--until", "2026-10-31",
                       "--sort", "comments", "--json")
    assert code == 0 and [p["comments"] for p in json.loads(out)["data"]["posts"]] == [80, 72, 47]


@pytest.mark.parametrize("argv, message", [
    (["--sort", "comments"], "--sort comments needs a window"),
    (["--since", "2026-10-24", "--until", "2026-10-22"], "is not before --until"),
    (["--since", "yesterday-ish"], "--since 'yesterday-ish' is not a time"),
])
def test_archive_window_argument_errors(run, argv, message):
    code, out, backend = run("archive", "--publication", ORIGIN, *argv)
    assert code == 2 and message in out and not backend.requests


def test_thread_root_shows_one_sub_thread(run):
    code, out, _ = run("thread", "--post", f"{ORIGIN}/p/france-and-the-limits-of-liberalism", "--root", "4", "--json")
    data = json.loads(out)["data"]
    posts = [p for p in data["posts"] if not p.get("gap")]
    shown = [p["number"] for p in posts]
    assert code == 0 and data["root"] == 4 and shown[0] == 4 and {6, 8, 10, 11, 12, 19} <= set(shown)
    by_number = {p["number"]: p for p in posts}
    assert all(by_number[n]["reply_to"] in by_number for n in shown[1:])   # every post hangs under #4
    code, out, _ = run("thread", "--post", f"{ORIGIN}/p/france-and-the-limits-of-liberalism", "--root", "4")
    assert "outside #4's sub-thread" in out and "Sub-thread of #4:" in out
    code, out, _ = run("thread", "--post", f"{ORIGIN}/p/france-and-the-limits-of-liberalism", "--root", "999")
    assert code == 2 and "--root 999 is not a post in this thread" in out


def test_thread_names_its_largest_sub_threads_with_root_steps(run):
    code, out, _ = run("thread", "--post", f"{ORIGIN}/p/france-and-the-limits-of-liberalism")
    note = next(line for line in out.splitlines() if line.startswith("Largest sub-threads"))
    biggest = re.search(r"#(\d+) .*? \((\d+)\)", note)
    assert code == 0 and biggest
    assert (f"--root {biggest.group(1)}` to read #{biggest.group(1)}'s sub-thread ({biggest.group(2)} replies)"
            in out)


def test_root_steps_leave_out_mode(run):
    _, out, _ = run("thread", "--post", f"{ORIGIN}/p/france-and-the-limits-of-liberalism", "--mode", "full")
    steps = [line for line in out.splitlines() if "--root" in line]
    assert steps and not any("--mode" in line for line in steps)


def test_outline_lists_top_level_comments_by_reply_count(run):
    code, out, _ = run("outline", "--post", f"{ORIGIN}/p/france-and-the-limits-of-liberalism", "--limit", "2")
    lines = out.splitlines()
    assert code == 0 and "count: 2 of 6 total" in out
    rows = lines[lines.index(next(l for l in lines if l.startswith("comments[2]"))) + 1:][:2]
    assert rows[0].startswith("  2,Bruce Raben,25,77,") and rows[1].startswith("  7,Fallingknife,3,39,")
    assert "--root <number>` to read one comment and every reply under it" in out
    assert "--limit 6` to show every top-level comment" in out


def test_outline_sorts_by_likes_and_thread_offers_it_for_big_threads(run, monkeypatch):
    import esum.sites.substack as substack
    _, out, _ = run("outline", "--post", f"{ORIGIN}/p/france-and-the-limits-of-liberalism", "--sort", "likes")
    rows = [l for l in out.splitlines() if l.startswith("  ") and l[2].isdigit()]
    likes = [int(r.split(",")[3]) for r in rows]
    assert likes == sorted(likes, reverse=True) and len(rows) == 6
    monkeypatch.setattr(substack, "OUTLINE_AFTER", 10)
    _, out, _ = run("thread", "--post", f"{ORIGIN}/p/france-and-the-limits-of-liberalism")
    help_lines = out[out.index("help["):].splitlines()
    assert help_lines[1].startswith("  Run `esum substack outline --post ") and "without the text" in help_lines[1]


# --- 吾爱破解 and Substack against raw pages: lifted posts, embeds, images, list ends and edits ----------

def attrs(value: dict) -> str:
    return escape(json.dumps(value))


def test_substack_keeps_tweets_and_videos():
    html = (f'<p>前文</p><div class="twitter-embed" data-attrs="{attrs({"url": "https://x.com/a/status/1", "full_text": "My  take", "username": "a"})}"></div>'
            f'<div id="youtube2-z" class="youtube-wrap" data-attrs="{attrs({"videoId": "wp84"})}"><div class="youtube-inner"><iframe></iframe></div></div>'
            '<p>后文</p>')
    text = html_to_text(sub.embeds(html))
    assert "@a: My take (https://x.com/a/status/1)" in text and "[video: https://youtu.be/wp84]" in text
    assert text.startswith("前文") and text.endswith("后文")


def test_substack_keeps_images():
    html = ('<a class="image-link image2 is-viewable-img" href="https://cdn.invalid/full.png"><div><picture>'
            '<img src="https://cdn.invalid/small.png" alt="A chart"></picture></div></a><figcaption>Source: OWID</figcaption>')
    text = html_to_text(sub.embeds(html))
    assert "[image: A chart https://cdn.invalid/full.png]" in text and "Source: OWID" in text


def test_substack_keeps_comment_edits_and_bans():
    post = {"id": 1, "title": "t", "slug": "t", "canonical_url": "https://s.invalid/p/t", "body_html": "<p>x</p>",
            "comment_count": 1}
    comments = [{"id": 2, "name": "a", "body": "hi", "date": "2026-10-01T00:00:00Z",
                 "edited_at": "2026-10-02T03:04:05Z", "user_banned_for_comment": True}]
    th = sub.thread_from(post, comments, "https://s.invalid")
    assert th.posts[1].edited == "2026-10-02 03:04" and th.posts[1].text.startswith("[user banned for this comment]")
