"""Reddit (reddit): this site's cases from the failure-contract and dogfooding rounds. The checks themselves
are shared (`support.outcomes`, in the checkout's tests/); the same contract is tested on synthetic
sites in tests/test_failure_outcomes.py."""

import pytest

from esum.core.outcomes import failure_outcome
from esum.sites.reddit import parse
from esum.transport import Response

from support.outcomes import refused_before_any_request


def test_a_bad_thread_id_is_refused_before_any_request(offline_site, capsys, action_runner):
    refused_before_any_request(offline_site, capsys, action_runner, ["reddit", "thread", "--id", "invalid!"])


def test_a_post_response_without_a_post_is_a_layout_failure():
    with pytest.raises(RuntimeError) as caught:
        parse.post_data({"data": {"children": []}})
    assert "esum-site-author" in failure_outcome(caught.value).help


# --- reddit search-comments: guests can read it, and a refusal is LOGIN NEEDED -------------------------

def test_reddit_comment_search_works_for_guests_and_a_refusal_is_login_needed(offline_site, action_runner, capsys):
    registry, fake_http = offline_site
    doc = registry.sites["reddit"].all_actions["search-comments"].func.__doc__
    assert "Needs a login" not in doc
    assert "Works without a login" in doc
    assert "still covers titles and post text" in doc
    url = "https://www.reddit.com/svc/shreddit/r/LocalLLaMA/search/?q=qwen&type=comments&sort=relevance&t=all"
    fake_http.routes[url] = Response(403, url, {}, b"Forbidden")
    argv = ["reddit", "search-comments", "--subreddit", "LocalLLaMA", "--query", "qwen"]
    assert action_runner.cli(argv, registry=registry) == 3
    assert "LOGIN NEEDED" in capsys.readouterr().out
