import io
import json

import pytest

from esum import Listing, Next, Result
from esum.core.context import Options
from esum.core.output import render
from esum.core.runner import run_action


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("listing", [False, True])
def test_next_commands_use_checked_shell_quoting(example_registry, monkeypatch, as_json, listing):
    site = example_registry.get("demo")
    act = site.all_actions["thread"]
    steps = [Next("thread", {"from_": 3}, "read more"), Next("big", {"lines": "5; echo unsafe"}, "see output")]
    result = Listing("items", [{"id": 1}], ("id",), next=steps) if listing else Result(
        data={"id": 7}, text="thread text", next=steps)
    monkeypatch.setattr(act, "func", lambda ctx, **kw: result)
    stream = io.StringIO()
    run_action(site, act, {"id": 7, "mode": "full"}, Options(json=as_json, no_spill=True), stream)
    output = stream.getvalue()
    hints = ["Run `esum demo thread --id 7 --mode full --from 3` to read more",
             "Run `esum demo big --lines '5; echo unsafe'` to see output"]
    if as_json:
        assert json.loads(output)["help"] == hints
    else:
        assert "help[2]:\n" + "\n".join("  " + hint for hint in hints) in output


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("listing", [False, True])
def test_unknown_next_action_fails_for_results_and_listings(example_registry, monkeypatch, as_json, listing):
    site = example_registry.get("demo")
    act = site.all_actions["thread"]
    steps = [Next("missing-action", {}, "try it")]
    result = Listing("items", [{"id": 1}], ("id",), next=steps) if listing else Result(text="x", next=steps)
    monkeypatch.setattr(act, "func", lambda ctx, **kw: result)
    with pytest.raises(ValueError, match="a next step names the unknown action 'missing-action'"):
        run_action(site, act, {"id": 7}, Options(json=as_json), io.StringIO())


def test_result_without_next_keeps_its_json_envelope():
    assert json.loads(render(Result(data={"a": 1}), site="s", action="a", as_json=True)) == {
        "site": "s", "action": "a", "untrusted_content": True, "data": {"a": 1}}


def test_next_defaults_are_independent():
    Result(next=[Next("read", {}, "read more")])
    assert Result().next == []


def test_empty_listing_only_exposes_steps_marked_when_empty():
    result = Listing("items", [], ("id",), next=[Next("read", {}, "read an item"),
                                                Next("search", {}, "search again", when_empty=True)])
    for as_json in (False, True):
        text = render(result, site="s", action="a", as_json=as_json,
                      command=lambda action, args: f"esum s {action}")
        assert "search again" in text and "read an item" not in text


def test_listing_json_help_names_the_next_page_like_its_text():
    result = Listing("items", [{"id": 1}], ("id",), more={"page": 2}, next=[Next("read", {}, "read an item")])
    command = lambda action, args: f"esum s {action}" + "".join(f" --{k} {v}" for k, v in args.items())
    hints = ["Run `esum s a --page 2` for the next page", "Run `esum s read` to read an item"]
    assert json.loads(render(result, site="s", action="a", as_json=True, command=command))["help"] == hints
    assert "help[2]:\n" + "\n".join("  " + h for h in hints) in render(
        result, site="s", action="a", as_json=False, command=command)


@pytest.mark.parametrize("as_json", [False, True])
def test_a_spilled_result_keeps_its_next_steps_on_stdout(tmp_path, monkeypatch, as_json):
    from esum.core import output
    monkeypatch.setattr(output, "spill_threshold", lambda: 10)
    result = Result(data={"posts": []}, text="x" * 100, summary="Title\n1 of 9 posts shown",
                    next=[Next("thread", {}, "read the posts left out")])
    stream = io.StringIO()
    assert output.emit(result, site="s", action="a", as_json=as_json, stream=stream, directory=tmp_path,
                       command=lambda action, args: f"esum s {action}") is not None
    hint = "Run `esum s thread` to read the posts left out"
    if as_json:
        assert json.loads(stream.getvalue())["help"] == [hint]
    else:
        assert f"help[1]:\n  {hint}" in stream.getvalue()


@pytest.mark.parametrize("as_json", [False, True])
def test_a_spilled_list_keeps_its_next_steps_on_stdout(tmp_path, monkeypatch, as_json):
    from esum.core import output
    monkeypatch.setattr(output, "spill_threshold", lambda: 10)
    listing = Listing("items", [{"id": i} for i in range(40)], ("id",), total=40,
                      next=[Next("read", {"id": 0}, "read the first item")])
    stream = io.StringIO()
    command = lambda action, args: f"esum s {action}" + "".join(f" --{k} {v}" for k, v in args.items())
    assert output.emit(listing, site="s", action="a", as_json=as_json, stream=stream, directory=tmp_path,
                       command=command) is not None
    hint = "Run `esum s read --id 0` to read the first item"
    if as_json:
        assert json.loads(stream.getvalue())["help"] == [hint]
    else:
        assert f"help[1]:\n  {hint}" in stream.getvalue()


def test_a_spilled_thread_prints_an_index_of_safe_parts(tmp_path, monkeypatch):
    from esum.core import output
    monkeypatch.setattr(output, "spill_threshold", lambda: 10)
    monkeypatch.setattr(output, "INDEX_PART_TOKENS", 100)
    posts = "\n".join(f"[user{n} #{n}] (2026-10-01 10:00 · ↩#1): " + "word " * 120 for n in range(1, 7))
    stream = io.StringIO()
    output.emit(Result(data={}, text=f"# Title\n\n{posts}", summary="Title"), site="s", action="thread",
                as_json=False, stream=stream, directory=tmp_path / "out")
    out = stream.getvalue()
    assert "Index (6 parts, each one safe read; post numbers → lines):" in out
    assert "  #1–#1: lines 4–4" in out and "  #6–#6: lines 9–9" in out
    spilled = next((tmp_path / "out").iterdir()).read_text(encoding="utf-8").split("\n")
    assert spilled[8].startswith("[user6 #6]")   # line 9, as the index says


def test_thread_index_counts_cjk_as_one_token_a_character():
    from esum.core.output import thread_index
    english = "\n".join(f"[u #{n}] (x): " + "a" * 400 for n in range(1, 5))     # ~133 tokens a post
    chinese = "\n".join(f"[u #{n}] (x): " + "中" * 400 for n in range(1, 5))    # ~400 tokens a post
    assert len(thread_index(english)) == 1 and len(thread_index(chinese)) == 1   # both under 12k
    from esum.core import output
    assert output._tokens("中" * 400) == 400 and output._tokens("a" * 300) == 100
    assert thread_index("[u #1] (x): only one post") == []


def test_thread_index_breaks_at_gaps_and_keeps_parts_under_the_cap(monkeypatch):
    from esum.core import output
    monkeypatch.setattr(output, "INDEX_PART_TOKENS", 250)
    post = lambda n: f"[u #{n}] (x): " + "a" * 300          # ~100 tokens a post
    text = "\n".join([post(1), post(2), post(3), "[... omitted 27 post numbers (#4–#30), not read …]",
                      post(31), post(32)])
    index = output.thread_index(text)
    assert index == ["#1–#2: lines 1–2 (~200 tokens)", "#3–#3: lines 3–3 (~100 tokens)",
                     "(27 post numbers (#4–#30), not read: not in this file)",
                     "#31–#32: lines 5–6 (~200 tokens)"]


@pytest.fixture
def installed(example_registry, monkeypatch):
    """The example world is what this process has installed: `one` is its built-in site, `two` and
    `demo` are custom ones (D-25: no custom code loads to print a hint)."""
    from esum.core import registry
    monkeypatch.setattr(registry, "get_registry", lambda: example_registry)
    return example_registry


def test_a_step_on_another_builtin_site_names_its_command(installed, monkeypatch):
    site = installed.get("demo")
    act = site.all_actions["thread"]
    steps = [Next("topic", {"id": 3}, "find a topic", site="one")]
    monkeypatch.setattr(act, "func", lambda ctx, **kw: Result(text="x", next=steps))
    stream = io.StringIO()
    run_action(site, act, {"id": 7}, Options(no_spill=True), stream)
    assert "Run `esum one topic --id 3` to find a topic" in stream.getvalue()


@pytest.mark.parametrize("other", ["two", "nosuchsite"])
def test_a_step_on_a_site_that_is_not_a_builtin_here_is_left_out(installed, monkeypatch, other):
    """A distribution can leave a site out, and a custom site's code is not loaded to print a hint:
    the step is dropped and the rest of the result prints."""
    site = installed.get("demo")
    act = site.all_actions["thread"]
    steps = [Next("topic", {}, "look", site=other), Next("thread", {"id": 8}, "read the next one")]
    monkeypatch.setattr(act, "func", lambda ctx, **kw: Result(text="x", next=steps))
    stream = io.StringIO()
    run_action(site, act, {"id": 7}, Options(no_spill=True), stream)
    out = stream.getvalue()
    assert "to look" not in out and other not in out
    assert "Run `esum demo thread --id 8` to read the next one" in out


def test_a_step_on_another_builtin_site_must_name_an_action_it_has(installed, monkeypatch):
    site = installed.get("demo")
    act = site.all_actions["thread"]
    steps = [Next("nosuchaction", {}, "look", site="one")]
    monkeypatch.setattr(act, "func", lambda ctx, **kw: Result(text="x", next=steps))
    with pytest.raises(ValueError, match="demo thread: a next step names the unknown action one 'nosuchaction'"):
        run_action(site, act, {"id": 7}, Options(), io.StringIO())


def test_a_help_line_names_another_sites_command_only_when_that_site_is_here(installed, monkeypatch):
    """A site's own use of this (gflights naming matrix's `airports`) is tested in that site's folder."""
    from esum import other_site_command
    assert other_site_command("one", "topic", {"id": "<id>"}) == "esum one topic --id <id>"
    assert other_site_command("nosuchsite", "airports", {}) is None
    monkeypatch.setattr(installed, "sites", {k: v for k, v in installed.sites.items() if k != "one"})
    assert other_site_command("one", "topic", {"id": "<id>"}) is None
