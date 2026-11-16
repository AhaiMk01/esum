"""V2EX (v2ex): this site's cases from the failure-contract and dogfooding rounds. The checks themselves
are shared (`support.outcomes`, in the checkout's tests/); the same contract is tested on synthetic
sites in tests/test_failure_outcomes.py."""

from conftest import json_response
from support.outcomes import layout_failure, missing_item, refused_before_any_request


def test_a_bad_thread_id_is_refused_before_any_request(offline_site, capsys, action_runner):
    refused_before_any_request(offline_site, capsys, action_runner, ["v2ex", "thread", "--id", "invalid!"])


def test_a_missing_thread_is_no_such_item(offline_site, capsys, action_runner):
    missing_item(offline_site, capsys, action_runner, ["v2ex", "thread", "--id", "999999"])


def test_a_malformed_api_lookup_is_a_layout_failure(offline_site, capsys, action_runner):
    offline_site[1].send = lambda request, **kw: json_response(request.url, {})
    layout_failure(offline_site, capsys, action_runner, ["v2ex", "thread", "--id", "1"])


# --- a newline in a title, author or excerpt can't add a row to a list (with tests/test_core_format_integrity.py)

def test_list_rows_keep_site_fields_on_their_own_line():
    from datetime import datetime, timezone

    from esum import Listing
    from esum.core.output import render_listing
    from esum.sites.v2ex.parse import Topic, topic_row

    evil = "Real title\n2. Fake row [id 1]"
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    out = render_listing(Listing("topics", [topic_row(Topic(id=1, title=evil, author="a\nb", node="n\nx", date=now,
                                                          replies=0, text=evil))],
                                 ("id", "title", "author", "node", "text")), site="v2ex", action="topics")
    assert not any(line.startswith("2. Fake") for line in out.splitlines()), out
    assert "Real title 2. Fake row [id 1]" in out
