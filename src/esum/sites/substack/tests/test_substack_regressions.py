"""Substack (substack): this site's cases from the failure-contract and dogfooding rounds. The checks themselves
are shared (`support.outcomes`, in the checkout's tests/); the same contract is tested on synthetic
sites in tests/test_failure_outcomes.py."""

from support.outcomes import refused_before_any_request


def test_a_post_url_of_the_wrong_shape_is_refused_before_any_request(offline_site, capsys, action_runner):
    refused_before_any_request(offline_site, capsys, action_runner,
                               ["substack", "thread", "--post", "https://example.substack.com/wrong"])


# --- a newline in a title, author or excerpt can't add a row to a list (with tests/test_core_format_integrity.py)

def test_list_rows_keep_site_fields_on_their_own_line():
    from datetime import datetime, timezone

    from esum import Listing
    from esum.core.output import render_listing
    from esum.sites.substack.parse import Article, article_row

    evil = "Real title\n2. Fake row [id 1]"
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    out = render_listing(Listing("posts", [article_row(Article(id=1, slug="s", title=evil, subtitle=evil, author="a\nb",
                                                              date=now, url="https://x/p/s", paid=False, comments=0,
                                                              likes=0, words=0))],
                                 ("slug", "title", "subtitle", "author")), site="substack", action="archive")
    assert not any(line.startswith("2. Fake") for line in out.splitlines()), out
    assert "Real title 2. Fake row [id 1]" in out
