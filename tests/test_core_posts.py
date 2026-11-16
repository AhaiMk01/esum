from datetime import date, datetime, timezone

import pytest

from esum import Gap, Post, chunked, format_post, html_to_text, render_posts, render_thread, \
    select_numbers, to_spans, with_gaps
from esum.core.errors import ArgumentError
from esum.core.posts import format_gap, parse_date, parse_since
from esum.core.text import short_url


def test_compact_post_format_full():
    p = Post(number=5, user="alice", text="  hello\n\n  world  ", date=date(2026, 10, 26), likes=3,
             is_op=True, reply_to=2)
    assert format_post(p) == "[alice #5] (2026-10-26 · 👍3 · OP · ↩#2): hello\n    world"


def test_compact_post_format_minimal_and_datetime():
    assert format_post(Post(1, "bob", "hi")) == "[bob #1]: hi"
    dt = datetime(2026, 10, 26, 15, 4, tzinfo=timezone.utc)
    assert format_post(Post(2, "bob", "x", date=dt)) == "[bob #2] (2026-10-26 15:04): x"
    assert parse_date("2026-10-26T15:04:05.000Z") == datetime(2026, 10, 26, 15, 4, 5, tzinfo=timezone.utc)
    assert parse_date("garbage") is None


def test_gap_markers():
    assert format_gap(Gap(31, 170)) == "[... omitted 140 post numbers (#31–#170) …]"
    assert format_gap(Gap(4, 4, "deleted")) == "[... omitted 1 post number (#4), deleted …]"


def test_with_gaps_inserts_leading_middle_trailing():
    posts = [Post(3, "a", "x"), Post(2, "a", "x"), Post(6, "a", "x")]
    items = with_gaps(posts, total=8)
    assert [(type(i).__name__, getattr(i, "number", None) or (i.start, i.end)) for i in items] == [
        ("Gap", (1, 1)), ("Post", 2), ("Post", 3), ("Gap", (4, 5)), ("Post", 6), ("Gap", (7, 8))]
    assert with_gaps([Post(1, "a", "x")], total=1) == [Post(1, "a", "x")]


def test_render_thread():
    items = with_gaps([Post(1, "a", "first", is_op=True), Post(3, "b", "third")], total=3)
    text = render_thread("Title", items, url="https://f.invalid/t/1", total=3)
    assert text.splitlines() == [
        "# Title", "https://f.invalid/t/1 · 2 of 3 posts shown", "",
        "[a #1] (OP): first", "[... omitted 1 post number (#2) …]", "[b #3]: third"]
    assert render_posts([]) == ""


def test_select_numbers_refuses_negative_post_numbers():
    with pytest.raises(ArgumentError, match="--from -3 is negative"):
        select_numbers(100, from_=-3)
    with pytest.raises(ArgumentError, match="--to -1 is negative"):
        select_numbers(100, to=-1)
    assert select_numbers(5, from_=0, to=2) == [1, 2]   # 0 still means "from the start"


def test_select_numbers_modes():
    assert select_numbers(100, "medium", head=3, tail=2) == [1, 2, 3, 99, 100]
    assert select_numbers(4, "medium", head=3, tail=2) == [1, 2, 3, 4]
    assert select_numbers(5, "full") == [1, 2, 3, 4, 5]
    assert select_numbers(100, "medium", from_=10, to=12) == [10, 11, 12]
    assert select_numbers(10, "full", from_=8) == [8, 9, 10]
    assert select_numbers(10, "full", to=2) == [1, 2]
    with pytest.raises(ValueError, match="ends at #10"):
        select_numbers(10, "medium", from_=20)
    assert select_numbers(0) == []
    with pytest.raises(ValueError):
        select_numbers(10, "weird")


def test_spans_and_chunks():
    assert to_spans([7, 1, 2, 3, 8]) == [(1, 3), (7, 8)]
    assert list(chunked(list(range(5)), 2)) == [[0, 1], [2, 3], [4]]


def test_a_paragraph_in_a_list_item_stays_on_the_bullets_line():
    assert html_to_text("<ul>\n<li>\n<p>one</p>\n</li><li><p>two</p></li></ul>") == "- one\n- two"
    assert html_to_text("<ul><li>a<p>b</p></li></ul>") == "- a\nb"


def test_html_to_text():
    html = ("<p>Hello <b>there</b></p><blockquote><p>quoted</p></blockquote>"
            "<ul><li>one</li><li>two</li></ul><script>evil()</script>"
            "<p><a href='https://x.invalid/long'>link</a> and <a href='https://y.invalid'>https://y.invalid</a>"
            "<br>next &amp; last <img alt='pic' src='a.png'></p>")
    assert html_to_text(html).splitlines() == [
        "Hello there", "> quoted", "- one", "- two",
        "link (https://x.invalid/long) and https://y.invalid", "next & last [image: pic]"]
    assert html_to_text("<a href='https://z.invalid'>z</a>", keep_links=False) == "z"


def test_long_link_targets_stay_followable():
    slug = "https://blog.invalid/" + "a-very-long-article-slug-" * 6 + "/"
    assert short_url(slug, 120) == slug                                  # no query: kept whole
    assert short_url(slug + "#comment-1", 120) == slug                   # the fragment goes first
    assert short_url(slug + "?utm_source=" + "x" * 50, 120) == slug + "?…"   # a long query is cut
    tracked = "https://t.invalid/c?u=" + "y" * 200
    assert short_url(tracked, 120) == tracked[:119] + "…"                # short page: query prefix kept
    assert short_url("https://x.invalid/" + "z" * 300, 120).endswith("…")


def test_an_image_without_alt_text_keeps_its_url():
    assert html_to_text('<h2>趋势图</h2><p><img src="//x.invalid/chart/" alt=""></p>').splitlines() == [
        "趋势图", "[image: https://x.invalid/chart/]"]
    assert html_to_text('<img src="data:image/gif;base64,AAA" alt="">') == ""


def test_since_accepts_weeks():
    now = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
    assert parse_since("2w", now) == datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    assert parse_since("1W", now) == datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="is not a time") as error:
        parse_since("2 fortnights", now)
    assert "2w / 7d / 12h / 30m" in error.value.help


@pytest.mark.parametrize("value", ["1000000w", "9" * 5000 + "d"], ids=["weeks", "5000-digit-days"])
def test_since_rejects_a_relative_time_before_year_one(value):
    with pytest.raises(ArgumentError, match="is too far back") as error:
        parse_since(value, datetime(2026, 11, 3, 12, tzinfo=timezone.utc))
    assert error.value.help


@pytest.mark.parametrize("value", ["0d", "0h", "0W", "00m"])
def test_since_rejects_a_relative_zero_because_nothing_is_newer_than_now(value):
    with pytest.raises(ArgumentError, match="is now") as error:
        parse_since(value, datetime(2026, 11, 3, 12, tzinfo=timezone.utc))
    assert "1d" in error.value.help


def test_gap_markers_count_post_numbers_not_posts():
    # numbers include deleted posts, so the marker must not claim that many posts exist
    assert format_gap(Gap(32, 5200, "not read")) == "[... omitted 5169 post numbers (#32–#5200), not read …]"


def test_subtree_follows_replies_at_any_depth():
    from esum import subtree
    posts = [Post(1, "op", "x"), Post(2, "a", "x", reply_to=1), Post(3, "b", "x"), Post(4, "c", "x", reply_to=2),
             Post(5, "d", "x", reply_to=3), Post(6, "e", "x", reply_to=4)]
    assert subtree(posts, 2) == [2, 4, 6]
    assert subtree(posts, 1) == [1, 2, 4, 6]
    assert subtree(posts, 5) == [5]
    with pytest.raises(ArgumentError, match="--root 9 is not a post"):
        subtree(posts, 9)


def test_largest_subthreads_ranks_top_level_comments_by_replies():
    from esum import largest_subthreads
    posts = [Post(1, "op", "x"), Post(2, "a", "x"), Post(3, "b", "x", reply_to=2), Post(4, "c", "x", reply_to=3),
             Post(5, "d", "x", reply_to=1), Post(6, "e", "x", reply_to=5), Post(7, "f", "x", reply_to=2),
             Post(8, "g", "x")]
    assert largest_subthreads(posts, least=1) == [(2, "a", 3), (5, "d", 1)]   # #5 replies to the post itself
    assert largest_subthreads(posts, least=2) == [(2, "a", 3)]
    assert largest_subthreads(posts, n=1, least=0)[0][0] == 2


def test_range_steps_offer_reading_forward_before_reading_back():
    from esum.core.thread_view import thread_view
    view = thread_view([Post(n, "u", "x") for n in range(31, 61)], [(1, 100, "not read")])
    steps = view.range_steps()
    assert [s.purpose for s in steps] == ["read the posts after these", "read the posts before these"]
    assert (steps[0].args["from_"], steps[0].args["to"]) == (61, 100)
    assert (steps[1].args["from_"], steps[1].args["to"]) == (1, 30)


def test_reply_counts_count_replies_at_any_depth_and_top_level_skips_the_post():
    from esum import is_top_level, reply_counts
    posts = [Post(1, "op", "x"), Post(2, "a", "x", reply_to=1), Post(3, "b", "x", reply_to=2),
             Post(4, "c", "x", reply_to=3), Post(5, "d", "x")]
    assert reply_counts(posts) == {1: 3, 2: 2, 3: 1, 4: 0, 5: 0}
    assert [p.number for p in posts if is_top_level(p)] == [2, 5]


# --- V2EX and NodeSeek against their raw pages: nothing unknown shown as zero, nothing silently missing ---

def test_a_post_shows_when_it_was_edited():
    post = Post(5, "a", "价格 $7.75", edited="2026-10-30 08:02")
    assert format_post(post).startswith("[a #5] (edited 2026-10-30 08:02): ")
