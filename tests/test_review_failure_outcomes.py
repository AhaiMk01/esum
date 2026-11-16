"""Regressions for the failure-outcome boundary review; no real network or browser."""
from types import SimpleNamespace

import pytest

from esum import LoginNeeded, Tier, Write, action, site
from esum.cli import main
from esum.core import gate
from esum.core.errors import WriteInterrupted, WriteLogError
from esum.core.outcomes import failure_outcome
from esum.core.paths import writes_log
from conftest import json_response
from support import engine_sites, engine_world  # noqa: F401
from support.outcomes import layout_failure


@pytest.mark.parametrize("argv, help", [
    (["--min-interval", "abc", "demo", "thread"], "Run `esum --help` for the global flags"),
    (["--max-retries", "-1", "demo", "thread"], "Run `esum --help` for the global flags"),
    (["--browser"], "Run `esum --help` for the global flags"),
    (["--browser", "wrong"], "Run `esum --help` for the global flags"),
    (["--browser=native", "--cdp=x"], "Run `esum --help` for the global flags"),
    (["docs"], "Run `esum docs <site>` to read a site doc; `esum list` lists the sites"),
    (["docs", "--all"], "Run `esum docs --all --write` to regenerate every built-in site doc"),
])
def test_cli_syntax_help_names_the_actual_command(argv, help, capsys, example_registry):
    assert main(argv, registry=example_registry) == 2
    assert capsys.readouterr().out.splitlines()[-1] == f"help: {help}"


@pytest.mark.parametrize("cause, code", [(TimeoutError("timeout"), 1), (LoginNeeded("demo"), 3)])
def test_nested_write_interruptions_have_one_suffix(cause, code):
    error = WriteInterrupted(WriteInterrupted(cause, ["second", "first"]), ["first"])
    outcome = failure_outcome(error, ["demo", "save"])
    assert outcome.exit_code == code
    assert outcome.message == failure_outcome(cause).message
    assert outcome.help.count("This was a write") == outcome.help.count("Already written:") == 1
    assert "Already written: first; second." in outcome.help


def test_log_failure_names_local_path_and_attempts_every_skipped_record(monkeypatch):
    target = site(name="demo", base_url="https://demo.invalid", label="Demo")
    @action(target, tier=Tier.REVERSIBLE)
    def save(ctx):
        """Save items."""
    events, sent = [], []
    def log(**event):
        events.append((event.get("item"), event.get("outcome")))
        if event.get("outcome") in ("ok", "skipped"):
            raise OSError("disk full")
    monkeypatch.setattr(gate, "log_event", log)
    with pytest.raises(WriteInterrupted) as caught:
        gate.execute_writes(target, target.actions["save"], {}, [
            Write(name, lambda name=name: sent.append(name)) for name in ("first", "second", "third")])
    assert sent == ["first"]
    assert events == [(None, None), ("first", "ok"), ("second", "skipped"), ("third", "skipped")]
    assert isinstance(caught.value.cause, WriteLogError)
    help = failure_outcome(caught.value).help
    assert str(writes_log()) in help and "The listed items were written" in help
    assert "Already written: first." in help and "site may have changed" not in help


# On one synthetic site per engine (support.engine_world); a standalone site's case is in that site's folder.

@pytest.mark.parametrize("rows, code", [([], 1), ([{"id": 1, "name": "Known", "parent": None}], 1)])
def test_catalog_lookup_distinguishes_missing_name_from_broken_catalog(rows, code, monkeypatch, engine_world, capsys, action_runner):
    from esum.engines import discuz
    monkeypatch.setattr(discuz, "get_page", lambda *a, **kw: "html")
    monkeypatch.setattr(discuz.parse, "parse_forums", lambda *a, **kw: rows)
    assert action_runner.cli(["board", "forum", "--id", "Missing"], registry=engine_world[0]) == code
    output = capsys.readouterr().out
    assert ("check the id or name" if rows else "esum-site-author") in output


def test_vbulletin_broken_canonical_url_is_layout_failure(monkeypatch, engine_world, capsys, action_runner):
    from esum.engines import vbulletin
    monkeypatch.setattr(vbulletin, "thread_page", lambda *a, **kw: SimpleNamespace(page=2, url="https://lounge.invalid/new-url-shape"))
    assert action_runner.cli(["lounge", "thread", "--id", "https://lounge.invalid/forum/showthread.php?p=123"], registry=engine_world[0]) == 1
    output = capsys.readouterr().out
    assert "canonical thread URL" in output and "esum-site-author" in output


def test_empty_forum_page_with_missing_pager_is_layout_failure(monkeypatch, engine_world, capsys, action_runner):
    from esum.engines import discuz
    monkeypatch.setattr(discuz, "get_page", lambda *a, **kw: "html")
    monkeypatch.setattr(discuz.parse, "parse_forum_page", lambda *a, **kw: {"pages": 1, "threads": []})
    layout_failure(engine_world, capsys, action_runner, ["board", "forum", "--id", "1", "--page", "2"])


def test_malformed_api_lookup_is_layout_failure(engine_world, capsys, action_runner):
    engine_world[1].send = lambda request, **kw: json_response(request.url, {})
    layout_failure(engine_world, capsys, action_runner, ["blog", "thread", "--id", "some-slug"])


def test_custom_plain_errors_keep_repair_guidance():
    for error in (ValueError("bad response"), LookupError("missing field")):
        outcome = failure_outcome(error, ["custom", "thread"])
        assert outcome.exit_code == 1 and "esum-site-author" in outcome.help


@pytest.mark.parametrize("case", ["discourse-category", "wordpress-category", "wordpress-disqus"])
def test_other_page_derived_surprises_are_layout_failures(case, monkeypatch, engine_world, capsys, action_runner):
    registry, backend = engine_world
    if case == "discourse-category":
        from esum.engines import discourse
        monkeypatch.setattr(discourse, "categories", lambda ctx: [])
        args = ["forum", "latest", "--category", "missing"]
    elif case == "wordpress-category":
        backend.send = lambda request, **kw: json_response(request.url, {})
        args = ["blog", "recent", "--category", "missing"]
    else:
        from esum.engines import wordpress
        context = SimpleNamespace(http=SimpleNamespace(get_text=lambda *a: "html"))
        with pytest.raises(RuntimeError) as caught:
            wordpress.disqus_comments(context, "https://doc.invalid/post", (), "medium", None, None)
        assert "esum-site-author" in failure_outcome(caught.value).help
        return
    layout_failure(engine_world, capsys, action_runner, args)
