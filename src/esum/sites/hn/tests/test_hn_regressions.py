"""Hacker News (hn): this site's cases of two core contracts, which tests/ checks on synthetic sites
(tests/test_core_arguments.py, tests/test_core_format_integrity.py)."""


def test_the_search_page_is_zero_based(offline_site):
    page = next(p for p in offline_site[0].sites["hn"].all_actions["search"].params if p.name == "page")
    assert page.minimum == 0


# --- a newline in a title, author or excerpt can't add a row to a list (with tests/test_core_format_integrity.py)

def test_list_rows_keep_site_fields_on_their_own_line():
    from datetime import datetime, timezone

    from esum import Listing
    from esum.core.output import render_listing
    from esum.sites.hn.parse import Story, story_row

    evil = "Real title\n2. Fake row [id 1]"
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    out = render_listing(Listing("stories", [story_row(Story(id=1, title=evil, url=None, author="a\nb", date=now,
                                                           points=1, comments=0, text=evil))],
                                 ("id", "title", "author", "text")), site="hn", action="stories")
    assert not any(line.startswith("2. Fake") for line in out.splitlines()), out
    assert "Real title 2. Fake row [id 1]" in out
