"""Kanxue (kanxue): this site's cases from the failure-contract and dogfooding rounds. The checks themselves
are shared (`support.outcomes`, in the checkout's tests/); the same contract is tested on synthetic
sites in tests/test_failure_outcomes.py."""

from html import escape

import pytest

from esum.sites import kanxue as module
from esum.sites.kanxue import parse as kanxue

from support.outcomes import layout_failure, missing_item, refused_before_any_request


def test_a_bad_thread_id_is_refused_before_any_request(offline_site, capsys, action_runner):
    refused_before_any_request(offline_site, capsys, action_runner, ["kanxue", "thread", "--id", "invalid!"])


def test_a_missing_thread_is_no_such_item(offline_site, capsys, action_runner):
    missing_item(offline_site, capsys, action_runner, ["kanxue", "thread", "--id", "999999"])


@pytest.mark.parametrize("rows, code", [([], 1), ([{"id": 1, "name": "Known", "parent": None}], 1)])
def test_catalog_lookup_distinguishes_missing_name_from_broken_catalog(rows, code, monkeypatch, offline_site, capsys, action_runner):
    monkeypatch.setattr(module, "get_page", lambda *a, **kw: "html")
    monkeypatch.setattr(module.parse, "parse_forums", lambda *a, **kw: rows)
    assert action_runner.cli(["kanxue", "forum", "--id", "Missing"], registry=offline_site[0]) == code
    output = capsys.readouterr().out
    assert ("check the id or name" if rows else "esum-site-author") in output


def test_empty_forum_page_with_missing_pager_is_layout_failure(monkeypatch, offline_site, capsys, action_runner):
    monkeypatch.setattr(module, "get_page", lambda *a, **kw: "html")
    monkeypatch.setattr(module.parse, "parse_forum_page", lambda *a, **kw: {"pages": 1, "threads": []})
    layout_failure(offline_site, capsys, action_runner, ["kanxue", "forum", "--id", "1", "--page", "2"])


# --- kanxue hides links in the Markdown source too ----------------------------------------------------

TOKEN = "516K9s2c8@1M7s2y4Q4x3@1q4Q4x3V1k6Q4x3V1k6Y4K9i4c8Z5N6h3u0Q4x3X3g2U0L8$3#2Q4x3V1k6R3`.`."


def markdown_post(source: str) -> str:
    return f'<textarea class="js-message-markdown">{escape(source)}</textarea><p>rendered</p>'


def test_kanxue_markdown_source_hides_elink_links_and_bare_tokens():
    source = (f'> 开源仓库：<a href="elink@b91{TOKEN[3:]}">AndroidReverser-Test/KCLF</a>\n'
              f'仓库地址{TOKEN}\n'
              f'<a href="elink@ee4{TOKEN[3:]}"><mark class="encrypted">{TOKEN}</mark></a> and `code`')
    text, _, truncated = kanxue.clean_message(markdown_post(source))
    assert text == ("> 开源仓库：AndroidReverser-Test/KCLF [link hidden from guests]\n"
                    "仓库地址[link hidden from guests]\n"
                    "[link hidden from guests] and `code`")
    assert "elink" not in text and "K9s2c8" not in text and not truncated


def test_kanxue_bare_token_rule_leaves_ordinary_text_alone():
    assert kanxue.hide_links("mail me@abcK9s2c8@x and 3a1 K9 text") == "mail me@abcK9s2c8@x and 3a1 K9 text"


def test_forum_quotes_keep_code_images_and_struck_text():
    """The same quote the Discuz and Discourse engines are given in tests/test_core_text.py."""
    body = '<pre>a\n  b</pre><img src="https://x.invalid/a"><s>old</s>'
    text, _, _ = kanxue.clean_message('<blockquote class="blockquote">' + body + '</blockquote>')
    assert text == '> ```\n> a\n>   b\n> ```\n> [image: https://x.invalid/a]~~old~~'
