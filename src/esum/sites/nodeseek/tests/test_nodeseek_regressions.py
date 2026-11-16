"""NodeSeek (nodeseek): this site's cases from the failure-contract and dogfooding rounds. The checks themselves
are shared (`support.outcomes`, in the checkout's tests/); the same contract is tested on synthetic
sites in tests/test_failure_outcomes.py."""

from pathlib import Path

import pytest

from esum.sites import nodeseek as module
from esum.transport import Response

from support.outcomes import layout_failure, missing_item, refused_before_any_request

FIX = Path(__file__).parent / "fixtures"


def test_a_bad_thread_id_is_refused_before_any_request(offline_site, capsys, action_runner):
    refused_before_any_request(offline_site, capsys, action_runner, ["nodeseek", "thread", "--id", "invalid!"])


def test_a_missing_thread_is_no_such_item(offline_site, capsys, action_runner):
    missing_item(offline_site, capsys, action_runner, ["nodeseek", "thread", "--id", "999999"])


def test_an_empty_later_list_page_is_a_layout_failure(monkeypatch, offline_site, capsys, action_runner):
    monkeypatch.setattr(module, "get_page", lambda *a, **kw: ("html", {}))
    monkeypatch.setattr(module.parse, "parse_list_page", lambda *a: {"page": 1, "threads": []})
    layout_failure(offline_site, capsys, action_runner, ["nodeseek", "listing", "--page", "2"])


# --- nodeseek's 404 error page ------------------------------------------------------------------------

@pytest.mark.parametrize("name, code, words", [
    ("error_guest.html", 3, "404 Not Found😭, HTTP 404: the post is for members or was removed"),
    ("error_member.html", 1, "the post was removed or is closed to this account"),
    ("not_found.txt", 1, "not found (HTTP 404)"),
])
def test_nodeseek_error_page_is_login_needed_for_a_guest_only(offline_site, action_runner, capsys, name, code, words):
    registry, fake_http = offline_site
    url = "https://www.nodeseek.com/post-1018885-1"
    fake_http.routes[url] = Response(404, url, {}, (FIX / name).read_bytes())
    assert action_runner.cli(["nodeseek", "thread", "--id", "1018885"], registry=registry) == code
    out = capsys.readouterr().out
    assert words in out
    assert ("LOGIN NEEDED" in out) == (code == 3)
    assert ("NoSuchItem" in out or "no such item" in out) == (code == 1)
