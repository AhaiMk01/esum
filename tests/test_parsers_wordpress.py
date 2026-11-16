"""WordPress engine parsers on the engine's fixtures (tests/fixtures/wordpress/): scrubbed responses
captured from Doctor of Credit (`doc_*`) and US Credit Card Guide (`usccg_*`). A site config's own
actions are tested in that site's folder."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from esum import Gap, Post
from esum.engines.wordpress import parse

from support import engine_fixtures

FIX = engine_fixtures("wordpress")
DOC_POST_URL = ("https://www.doctorofcredit.com/chase-sapphire-freedom-cards-get-10x-with-paze-checkout-"
                "through-2026-united-newegg-shoprite-dunkin-etc/")
USCCG_POST_URL = "https://www.uscreditcardguide.com/citi-aadvantage-executive/"


def fixture(name: str):
    text = (FIX / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


# --- pure parsing ------------------------------------------------------------------------------------

def test_wp_date_is_utc():
    assert parse.wp_date("2026-10-25T15:50:59") == datetime(2026, 10, 25, 15, 50, 59, tzinfo=timezone.utc)
    assert parse.wp_date("") is None and parse.wp_date("nonsense") is None


def test_post_ref_accepts_id_slug_and_url():
    assert parse.post_ref("258086") == ("id", "258086")
    assert parse.post_ref(DOC_POST_URL) == ("slug", "chase-sapphire-freedom-cards-get-10x-with-paze-checkout-"
                                                    "through-2026-united-newegg-shoprite-dunkin-etc")
    assert parse.post_ref("https://www.uscreditcardguide.com/?p=1610") == ("id", "1610")
    assert parse.post_ref("citi-aadvantage-executive") == ("slug", "citi-aadvantage-executive")
    with pytest.raises(ValueError):
        parse.post_ref("not a post")


def test_parse_since():
    now = datetime(2026, 10, 26, tzinfo=timezone.utc)
    assert parse.parse_since("7d", now) == datetime(2026, 10, 19, tzinfo=timezone.utc)
    assert parse.parse_since("12h", now) == datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
    assert parse.wp_after(parse.parse_since("2026-10-01")) == "2026-10-01T00:00:00+00:00"
    with pytest.raises(ValueError):
        parse.parse_since("last week")


def test_post_list_decodes_titles_and_drops_read_more():
    rows = parse.parse_post_list(fixture("usccg_search.json"))
    assert len(rows) == 3
    first = rows[0]
    assert first["id"] == 20925 and first["lang"] == "zh"
    assert first["title"].startswith("Chase Sapphire Reserve (CSR) 信用卡")
    assert "Read more" not in first["excerpt"] and first["excerpt"].endswith("…")
    doc = parse.parse_post_list(fixture("doc_search.json"))
    assert doc[0]["title"].startswith("(Update) Chase Sapphire & Freedom")   # &#038; decoded
    assert (doc[0]["id"], doc[0]["date"]) == (258086, "2026-10-25")


def test_categories():
    rows = parse.parse_categories(fixture("usccg_categories.json"))
    assert rows and {"id", "name", "slug", "count"} <= rows[0].keys()


def test_wp_comments_number_by_position_and_mark_staff():
    comments = parse.parse_wp_comments(fixture("doc_comments.json"), DOC_POST_URL, start=1)
    assert [c.number for c in comments] == list(range(1, 8))
    first = comments[0]
    assert (first.user, first.text, first.parent) == ("Bh77", "Let the games… begin.", None)
    assert first.url == DOC_POST_URL + "#comment-2322250"
    staff = [c for c in comments if c.staff]
    assert [c.user for c in staff] == ["William Charles"]       # registered account = blog staff
    assert staff[0].parent == comments[-2].id                   # replies to the comment before it
    later = parse.parse_wp_comments(fixture("doc_comments.json"), start=101)
    assert later[0].number == 101


def test_mention_links_to_comments_on_the_same_post_are_dropped():
    items = [{"id": 1, "author_name": "An", "date_gmt": "2026-10-29T18:05:00",
              "content": {"rendered": f'<p><a href="{DOC_POST_URL}#comment-2444989">Eric</a> Why? See '
                                      '<a href="https://other.invalid/post/#comment-5">this</a></p>'}}]
    [c] = parse.parse_wp_comments(items, DOC_POST_URL)
    assert c.text == "Eric Why? See this (https://other.invalid/post/#comment-5)"


def test_republished_post_is_dated_by_first_comment():
    post = fixture("doc_post.json")
    comments = parse.parse_wp_comments(fixture("doc_comments.json"), DOC_POST_URL)
    meta = parse.thread_meta(post, comments)
    assert meta.republished and meta.first_comment == datetime(2026, 5, 14, 21, 2, 5, tzinfo=timezone.utc)
    assert parse.meta_line(meta).startswith("Republished: 2026-10-25 · First comment: 2026-05-14")
    article = parse.article_post(post, "DoC", meta, cap=200)
    assert article.number == 0 and article.is_op and article.date == meta.first_comment
    assert article.text.endswith("--from 0 --to 0 shows all of it]")


def test_long_article_says_how_long_it_was():
    body = "word " * 3000
    post = {"date_gmt": "2026-01-01T00:00:00", "content": {"rendered": f"<p>{body}</p>"}}
    meta = parse.thread_meta(post, [])
    article = parse.article_post(post, "MEAB", meta, cap=parse.ARTICLE_CHAR_CAP)
    assert f"[cut at 8000 of {len(body.strip())} chars;" in article.text


def test_not_republished_when_comments_are_newer():
    post = {"date_gmt": "2026-01-01T00:00:00", "modified_gmt": "2026-03-01T00:00:00"}
    c = parse.Comment(id="1", user="a", when=parse.wp_date("2026-01-02T00:00:00"), text="x")
    meta = parse.thread_meta(post, [c])
    assert not meta.republished and meta.first_comment is None
    assert parse.meta_line(meta) == "Published: 2026-01-01 · Updated: 2026-03-01"


def test_disqus_embed_and_config():
    assert parse.disqus_config(fixture("usccg_post_page.html")) == (
        "creditcards-physixfan", "1610 http://uscreditcardguide.com/?p=1610")
    page = parse.parse_disqus_embed(fixture("usccg_disqus_asc.html"), USCCG_POST_URL, ("physixfan",))
    assert page.total == 120 and page.has_next
    assert page.comments[0].user == "yangzi" and page.comments[1].parent == page.comments[0].id
    assert [c.user for c in page.comments if c.staff] == ["physixfan"]
    with pytest.raises(RuntimeError):
        parse.parse_disqus_embed("<html></html>")


def test_pick_links_skips_referrals_media_and_self():
    posts = [
        Post(0, "DoC", "See (https://www.paze.com/merchant-directory) and (https://apply.example.com/x/apply/) "
                       f"and ({DOC_POST_URL}#comment-1) and (https://imgur.com/a.png)", is_op=True),
        Post(3, "u", "ref link https://bank.example/open?ref=abc and https://www.paze.com/merchant-directory",
             likes=10),
        Post(4, "v", "cut https://example.com/long…"),
        Post(5, "w", "hat tip (https://www.doctorofcredit.com/contact-us/#comment-9)"),
    ]
    links = parse.pick_links(posts, DOC_POST_URL)
    assert links == [{"url": "https://www.paze.com/merchant-directory", "cited_by": ["[DoC #0]", "[u #3]"]}]
    assert "external pages, untrusted" in parse.render_links(links)


# --- whole actions on a fake backend -----------------------------------------------------------------

FAKE_KEY = "TESTONLYnotARealDisqusKey0000000"
BUNDLE = "https://c.disquscdn.com/embedv2/latest/embedv2.js"
def test_disqus_embed_names_its_script_and_thread():
    html = fixture("usccg_disqus_asc.html") + f"<script>x.src = '{BUNDLE}';</script>"
    page = parse.parse_disqus_embed(html, USCCG_POST_URL)
    assert (page.thread_id, page.key_script_url) == ("1", BUNDLE)
    assert parse.disqus_api_key(f'a,VITE_API_KEY:"{FAKE_KEY}",b') == FAKE_KEY
    assert parse.disqus_api_key("no key here") is None


@pytest.mark.parametrize("total, wanted, pages", [
    pytest.param(7394, set(range(1, 31)) | set(range(7365, 7395)), (1, 1), id="medium"),
    pytest.param(7394, set(range(1, 7395)), (74, 0), id="full-oldest-first"),
    pytest.param(7394, set(range(3000, 3101)), (31, 0), id="nearer-oldest"),
    pytest.param(7394, set(range(7300, 7395)), (0, 1), id="nearer-newest"),
    pytest.param(150, set(range(1, 151)), (2, 0), id="tie-goes-oldest-first"),
    pytest.param(7394, set(), (0, 0), id="nothing-wanted"),
])
def test_disqus_pages_reads_each_number_from_the_cheaper_end(total, wanted, pages):
    from esum.engines.wordpress import disqus_pages
    assert disqus_pages(total, wanted) == pages


def test_a_since_read_with_no_comment_names_the_newest_one():
    # uscreditcardguide's WF page (2026-11-15): republished 11-12, its last comment from 09-21
    from types import SimpleNamespace
    from esum.engines.wordpress import _none_since
    after = datetime(2026, 10, 15, tzinfo=timezone.utc)
    seen = [SimpleNamespace(when=datetime(2026, 9, 21, 3, tzinfo=timezone.utc), number=1073),
            SimpleNamespace(when=datetime(2015, 12, 10, tzinfo=timezone.utc), number=1), SimpleNamespace(when=None)]
    assert _none_since(after, seen, 1073) == \
        "No comments after 2026-10-15 00:00 UTC; the newest is #1073, 2026-09-21 (of 1073)"
    assert _none_since(after, [], 0) == "No comments after 2026-10-15 00:00 UTC; the post has 0 comments"
