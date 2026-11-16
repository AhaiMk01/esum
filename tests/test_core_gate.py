import json

import pytest

from esum import Tier, Write, action, site
from esum.core import gate
from esum.core.errors import CheckNeeded, WriteInterrupted
from esum.core.outcomes import failure_outcome
from esum.core.paths import writes_log
from esum.transport import Response


def log_lines():
    path = writes_log()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] \
        if path.exists() else []


def bookmark_routes(fake_http, *ids):
    for i in ids:
        url = f"https://demo.invalid/t/{i}/bookmark"
        fake_http.routes[url] = Response(200, url, {}, b"{}")


def test_write_runs_every_item_and_logs_plan_and_outcomes(run, fake_http):
    """No dialog (D-36): ESUM runs the write it is given; the approval is the user's request."""
    bookmark_routes(fake_http, 1, 2, 3)
    code, out, err = run("demo", "bookmark", "--ids", "1", "2", "3")
    assert code == 0, err
    assert [r.method for r in fake_http.requests] == ["PUT"] * 3
    assert "done 3. Bookmark topic #3 on demo" in out
    entries = log_lines()
    assert [e["kind"] for e in entries] == ["write", "write-result", "write-result", "write-result"]
    assert entries[0]["items"] == [f"Bookmark topic #{i} on demo" for i in (1, 2, 3)]
    assert entries[0]["args"] == {"ids": [1, 2, 3]} and "decision" not in entries[0]
    assert all(e["outcome"] == "ok" for e in entries[1:])


def test_failed_write_logs_error_and_skips_rest():
    s = site(name="w", base_url="https://w.invalid", label="W")

    def fail():
        raise RuntimeError("server said no")

    @action(s, tier=Tier.REVERSIBLE)
    def go(ctx):
        """Two writes."""

    act = s.actions["go"]
    with pytest.raises(WriteInterrupted):
        gate.execute_writes(s, act, {}, [Write("first", fail), Write("second", lambda: "ok")])
    entries = log_lines()
    assert [(e.get("item"), e.get("outcome")) for e in entries[1:]] == [
        ("first", "error"), ("second", "skipped")]


def test_a_check_mid_write_says_what_is_already_written():
    """A rerun after CHECK NEEDED replays the command (D-54): the help must name the done items."""
    s = site(name="w", base_url="https://w.invalid", label="W")

    def checked():
        raise CheckNeeded("w", "https://w.invalid/", "esum", "esum-1a2b3c4d")

    @action(s, tier=Tier.REVERSIBLE)
    def go(ctx):
        """Two writes."""

    with pytest.raises(WriteInterrupted) as caught:
        gate.execute_writes(s, s.actions["go"], {}, [Write("first", lambda: "ok"), Write("second", checked)])
    assert caught.value.written == ["first"]
    assert "Already written: first" in failure_outcome(caught.value).help
    assert "rerun the write only for items not yet applied" in failure_outcome(caught.value).help
    assert "This was a write" not in CheckNeeded("w", "https://w.invalid/", "esum", "t").help
    assert "size couldn't be read" in CheckNeeded("w", "https://w.invalid/", "esum", "t").help
    sized = CheckNeeded("w", "https://w.invalid/", "esum", "t", viewport=(1037, 739), zoom=1.0)
    sized_help = failure_outcome(WriteInterrupted(sized, ["first"])).help
    assert "the page is 1037x739 CSS pixels" in sized_help and "Already written: first" in sized_help


def test_write_action_must_return_writes_and_reads_must_not():
    s = site(name="w", base_url="https://w.invalid", label="W")

    @action(s, tier=Tier.REVERSIBLE)
    def go(ctx):
        """Doc."""

    with pytest.raises(TypeError, match="must return a Write"):
        gate.execute_writes(s, s.actions["go"], {}, "did it myself")
    result = gate.execute_writes(s, s.actions["go"], {}, [])
    assert "Nothing to write" in result.text


def test_read_action_returning_writes_is_refused(tmp_path, capsys, isolated):
    from esum.cli import main
    from esum.core.registry import Registry
    from conftest import write

    write(isolated / "sites" / "sneaky" / "site.py", '''
        from esum import Tier, Write, action, site
        FORMAT = 1
        sneaky = site(name="sneaky", base_url="https://s.invalid", label="S")

        @action(sneaky, tier=Tier.READ)
        def go(ctx):
            """Pretends to read."""
            return Write("write", lambda: None)
    ''')
    reg = Registry.discover(engine_packages=(), site_packages=())
    assert main(["sneaky", "go"], registry=reg) == 1
    assert "tier-0 (read) action returned writes" in capsys.readouterr().out


def test_there_is_no_approve_command_or_write_dialog(run):
    code, err, _ = run("approve", "Post a reply in thread 5 on example.invalid")
    assert code == 2 and "unknown site or command 'approve'" in err
    assert not hasattr(gate, "approve") and not hasattr(gate, "tk_dialog")


def test_fields_on_a_write_is_refused_before_anything_is_sent(run, fake_http):
    bookmark_routes(fake_http, 1)
    code, out, _ = run("demo", "bookmark", "--ids", "1", "--fields", "x")
    assert code == 2 and out.startswith("error: --fields applies to lists")
    assert fake_http.requests == [] and log_lines() == []
