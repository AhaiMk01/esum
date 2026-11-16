from datetime import datetime, timezone
import json

import pytest

from esum import Gap, Post, before_span, read_spans, thread_view
from esum.core.output import render


def post(n):
    return Post(n, "u", f"text {n}")


def test_gaps_split_at_coverage_boundaries_and_posts_are_sorted_once():
    view = thread_view([post(8), post(1), post(1)], read_spans({1, 2, 3, 8, 9}, 10))
    assert view.items == [post(1), Gap(2, 3, "deleted or hidden"), Gap(4, 7, "not read"),
                          post(8), Gap(9, 9, "deleted or hidden"), Gap(10, 10, "not read")]


def test_zero_post_count_header_summary_and_json_share_one_view():
    view = thread_view([post(0), post(1), post(4)], read_spans({1, 4}, 4))
    result = view.result("Title", url="https://example.test/t", data={"id": 7}, notes=["Times: UTC"])
    assert "3 of 5 posts shown" in result.text
    assert result.summary == "Title\nhttps://example.test/t\nTimes: UTC\n3 of 5 posts shown"
    data = json.loads(render(result, site="test", action="thread", as_json=True))["data"]
    assert data["id"] == 7
    assert data["posts"] == [post(0).to_dict(), post(1).to_dict(),
                             Gap(2, 3, "not read").to_dict(), post(4).to_dict()]
    omitted_op = thread_view([post(3)], read_spans({3}, 4), count=5)
    assert omitted_op.items == [Gap(1, 2, "not read"), post(3), Gap(4, 4, "not read")]
    assert "1 of 5 posts shown" in omitted_op.result("Title").text


def test_numbering_limit_is_distinct_from_actual_post_count():
    result = thread_view([post(1), post(6)], [(1, 8, "deleted or hidden")], count=5).result("Deleted posts")
    assert "2 of 5 posts shown" in result.text
    assert result.summary.endswith("2 of 5 posts shown")
    assert result.data["posts"][-1] == Gap(7, 8, "deleted or hidden")


def test_locked_unread_and_lifted_spans_have_distinct_labels():
    view = thread_view([post(1)], [(11, 20, "members only"), (31, 40, "members only"),
                                       (25, 27, "not served by the API"),
                                       (2, 3, "deleted, or among the recommended posts below"),
                                       (1, 10, "deleted or hidden"), (21, 30, "deleted or hidden")])
    assert view.items == [post(1), Gap(2, 3, "deleted, or among the recommended posts below"),
                          Gap(4, 10, "deleted or hidden"), Gap(11, 20, "members only"),
                          Gap(21, 24, "deleted or hidden"), Gap(25, 27, "not served by the API"),
                          Gap(28, 30, "deleted or hidden"), Gap(31, 40, "members only")]


@pytest.mark.parametrize("start,posts,expected", [
    (5, [post(0), post(5)], [post(0), Gap(1, 4, "before 2026-10-01 00:00 UTC"), post(5)]),
    (6, [post(0)], [post(0), Gap(1, 5, "before 2026-10-01 00:00 UTC")]),
    (6, [], [Gap(1, 5, "before 2026-10-01 00:00 UTC")]),
])
def test_since_cutoff_with_context_or_no_new_posts(start, posts, expected):
    after = datetime(2026, 10, 1, tzinfo=timezone.utc)
    view = thread_view(posts, [before_span(after, min(5, start - 1)), (1, 5, "not read")])
    assert view.items == expected


def test_range_window_clips_gaps_and_empty_range_has_no_gaps():
    view = thread_view([post(5)], [(4, 7, "deleted or hidden")], count=12)
    assert view.items == [Gap(4, 4, "deleted or hidden"), post(5), Gap(6, 7, "deleted or hidden")]
    assert thread_view([], [(13, 12, "not read")], count=12).items == []


def test_large_unread_span_and_zero_only_thread():
    assert thread_view([post(1)], read_spans({1}, 1_000_000_000)).items == [
        post(1), Gap(2, 1_000_000_000, "not read")]
    result = thread_view([post(0)], []).result("Article")
    assert "1 of 1 posts shown" in result.text
    assert result.data["posts"] == [post(0)]


def test_opening_context_and_cutoff_with_a_bounded_since_read():
    after = datetime.fromisoformat("2026-10-01T08:00:00+08:00")
    view = thread_view([post(1), post(5)], [before_span(after, 4), (5, 7, "deleted or hidden")], count=10)
    assert view.items == [post(1), Gap(2, 4, "before 2026-10-01 00:00 UTC"),
                          post(5), Gap(6, 7, "deleted or hidden")]
    empty = thread_view([], [before_span(after, 10)], count=10)
    assert empty.items == [Gap(1, 10, "before 2026-10-01 00:00 UTC")]


def test_separately_fetched_article_and_overlapping_coverage():
    view = thread_view([post(0), post(5)], [(1, 3, "deleted or hidden"),
                                                   (3, 6, "deleted or hidden"), (6, 7, "deleted or hidden")])
    assert view.items == [post(0), Gap(1, 4, "deleted or hidden"), post(5), Gap(6, 7, "deleted or hidden")]


def test_uncovered_numbers_default_to_not_read_and_custom_reasons_are_opaque():
    view = thread_view([post(0), post(8)], [(2, 3, "private")])
    assert view.items == [post(0), Gap(1, 1, "not read"), Gap(2, 3, "private"),
                          Gap(4, 7, "not read"), post(8)]
    assert view.count == 9


def test_read_spans_clip_selection_to_the_known_last_number():
    view = thread_view([post(11), post(18)], read_spans(range(11, 21), 18), count=18)
    assert view.items == [Gap(1, 10, "not read"), post(11), Gap(12, 17, "deleted or hidden"), post(18)]
