"""NGA (nga): this site's cases from the failure-contract and dogfooding rounds. The checks themselves
are shared (`support.outcomes`, in the checkout's tests/); the same contract is tested on synthetic
sites in tests/test_failure_outcomes.py."""

from pathlib import Path
from types import SimpleNamespace

from esum.sites import nga as module
from esum.sites.nga import parse as nga

from support.outcomes import layout_failure, missing_item, refused_before_any_request


def doc(offline_site, act: str) -> str:
    return offline_site[0].sites["nga"].all_actions[act].func.__doc__


def test_a_bad_thread_id_is_refused_before_any_request(offline_site, capsys, action_runner):
    refused_before_any_request(offline_site, capsys, action_runner, ["nga", "thread", "--id", "invalid!"])


def test_a_missing_thread_is_no_such_item(offline_site, capsys, action_runner):
    missing_item(offline_site, capsys, action_runner, ["nga", "thread", "--id", "999999"])


def test_a_post_page_without_its_thread_is_a_layout_failure(monkeypatch, offline_site, capsys, action_runner):
    monkeypatch.setattr(module, "get_json", lambda *a, **kw: {})
    monkeypatch.setattr(module.parse, "thread_page", lambda *a, **kw: SimpleNamespace(tid=None))
    layout_failure(offline_site, capsys, action_runner, ["nga", "thread", "--id", "pid:123"])


def test_nga_stickers_and_docs_agree(offline_site):
    assert nga._markup("姑息治疗呢[s:ac:哭笑]") == "姑息治疗呢:哭笑:"
    assert "stickers (`[s:ac:…]`) show as their name, `:name:`" in doc(offline_site, "thread")
    assert "dropped" not in doc(offline_site, "thread")
    quirks = (Path(__file__).parents[1] / "doc.md").read_text(encoding="utf-8")
    assert "stickers (`[s:ac:…]`) dropped" not in quirks


def test_nga_listing_doc_counts_the_mixed_in_rows(offline_site):
    listing = doc(offline_site, "listing")
    assert "(35 rows)" not in listing and "about 50 rows" in listing


def test_the_listing_page_is_one_based(offline_site):
    page = next(p for p in offline_site[0].sites["nga"].all_actions["listing"].params if p.name == "page")
    assert page.minimum == 1
