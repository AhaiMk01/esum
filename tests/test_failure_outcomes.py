"""Offline checks of the failure contract at the core and action boundaries."""
import pytest

from esum import ArgumentError, LoginNeeded, NoSuchItem, SiteRefused, Tier, Write, action, site
from esum.cli import UsageError
from esum.core.errors import CheckNeeded, HttpError, SiteNotLoaded, UnsupportedURL, WriteInterrupted, WriteLogError
from esum.core.gate import execute_writes
from esum.core.outcomes import FailureOutcome, failure_outcome
from esum.core.output import FieldsError

from support import engine_sites, engine_world  # noqa: F401
from support.outcomes import missing_item, refused_before_any_request


REPAIR = ("Rerun it once. If it fails the same way, the site may have changed: the esum-site-author "
          "skill repairs a site, and ESUM_DEBUG=1 prints a traceback")
ARGUMENTS = "Change the argument the error names: `esum demo thread --help` lists each flag and its format"
MISSING = "The site has no such item: check the id or name. It may have been removed"
REFUSED = "The site refused this call: `esum docs demo` says what it needs"


@pytest.mark.parametrize("error, expected", [
    (ArgumentError("bad id 'x'"), ("bad id 'x'", ARGUMENTS, 2)),
    (ArgumentError("bad", help="specific"), ("bad", "specific", 2)),
    (UsageError("flag", help="flags"), ("flag", "flags", 2)),
    (FieldsError("column", "columns"), ("column", "columns", 2)),
    (SiteNotLoaded("broken", "repair module"), ("broken", "repair module", 1)),
    (NoSuchItem("item 3"), ("item 3", MISSING, 1)),
    (SiteRefused("item 4"), ("item 4", REFUSED, 1)),
    (HttpError(404, "https://demo.invalid/3"), ("HttpError: HTTP 404 from https://demo.invalid/3", MISSING, 1)),
    (HttpError(410, "https://demo.invalid/3"), ("HttpError: HTTP 410 from https://demo.invalid/3", MISSING, 1)),
    (HttpError(403, "https://demo.invalid/4"), ("HttpError: HTTP 403 from https://demo.invalid/4", REFUSED, 1)),
    (HttpError(500, "https://demo.invalid/5"), ("HttpError: HTTP 500 from https://demo.invalid/5", "The site failed: rerun in a minute", 1)),
    (HttpError(429, "https://demo.invalid/6"), ("HttpError: HTTP 429 from https://demo.invalid/6", "The site rate-limited ESUM: wait about 30 s, then rerun `esum demo thread --id x`", 1)),
    (HttpError(400, "https://demo.invalid/7"), ("HttpError: HTTP 400 from https://demo.invalid/7", "The site rejected the arguments: `esum demo thread --help` lists them", 1)),
    (TimeoutError("waited"), ("TimeoutError: waited", REPAIR, 1)),
    (OSError("disk"), ("OSError: disk", REPAIR, 1)),
    (UnsupportedURL("ftp://demo"), ("UnsupportedURL: ftp://demo", UnsupportedURL.help, 1)),
    (CheckNeeded("demo", "https://demo.invalid", "esum", "t"), ("CHECK NEEDED: demo (https://demo.invalid) shows a browser check in ESUM's tab 't' (patchright-cli session 'esum').", CheckNeeded("demo", "https://demo.invalid", "esum", "t").help, 5)),
    (RuntimeError("bug"), ("RuntimeError: bug", REPAIR, 1)),
    (LoginNeeded("demo"), ("LOGIN NEEDED: demo. The user logs in themselves in the browser ESUM uses. Never type or handle credentials.", LoginNeeded.help, 3)),
])
def test_failure_outcome_table(error, expected):
    assert failure_outcome(error, ["demo", "thread", "--id", "x"]) == FailureOutcome(*expected)


@pytest.mark.parametrize("cause", [
    HttpError(500, "https://demo.invalid/write"),
    TimeoutError("write timed out"),
    LoginNeeded("demo"),
    CheckNeeded("demo", "https://demo.invalid/", "esum", "tab"),
])
def test_every_mid_write_failure_lists_completed_items_once(cause):
    target = site(name="demo", base_url="https://demo.invalid", label="Demo")

    @action(target, tier=Tier.REVERSIBLE)
    def save(ctx):
        """Save three items."""

    sent = []
    def fail():
        sent.append("second")
        raise cause

    with pytest.raises(WriteInterrupted) as caught:
        execute_writes(target, target.actions["save"], {}, [
            Write("first item", lambda: sent.append("first")),
            Write("second item", fail),
            Write("third item", lambda: sent.append("third")),
        ])
    error = caught.value
    assert error.cause is cause and error.__cause__ is cause
    assert error.written == ["first item"] and sent == ["first", "second"]
    assert not hasattr(cause, "written")
    original = failure_outcome(cause, ["demo", "save"])
    outcome = failure_outcome(error, ["demo", "save"])
    assert outcome.message == original.message and outcome.exit_code == original.exit_code
    assert outcome.help == (original.help + ". This was a write. Already written: first item. Read each item's state and rerun "
                            "the write only for items not yet applied, never the whole command as is")
    assert outcome.help.count("Already written:") == outcome.help.count("first item") == 1


# One synthetic site per engine (support.engine_world); a standalone site's row is in that site's folder.
@pytest.mark.parametrize("argv", [
    ["board", "thread", "--id", "invalid!"],
    ["lounge", "thread", "--id", "invalid!"],
    ["blog", "thread", "--id", "https://example.com/"],
    ["forum", "thread", "--id", "invalid!"],
], ids=lambda argv: argv[0])
def test_bad_id_via_action_interface(argv, engine_world, capsys, action_runner):
    refused_before_any_request(engine_world, capsys, action_runner, argv)


@pytest.mark.parametrize("name", ["board", "lounge", "blog", "forum"])
def test_missing_item_via_action_interface(name, engine_world, capsys, action_runner):
    missing_item(engine_world, capsys, action_runner, [name, "thread", "--id", "999999"])


# SHA-256 of origin/main's literal CLI output; these fixed signals must remain byte-identical.
@pytest.mark.parametrize("kind, args, written, digest", [
    ('login', ('demo',), None, '351bea101b82ef91abbb43ed3a533a3375f8b6675a14e4cabb9e8e423df0de6a'),
    ('login', ('demo',), ['first item'], '77cbcd071db0522b448a24c602a05d31a671910efb1372fa47fb3c95fa610e9e'),
    ('check', ('demo', 'https://demo.invalid/', 'esum', 'tab'), None, '148a70c11574189f83b035055e7668f469b7b1738031dfa056be489b56a494db'),
    ('check', ('demo', 'https://demo.invalid/', 'esum', 'tab'), ['first item'], '66fe59e8f295f1abd5555d6bd5a26e11f9740b12c1bd01d259943eb25a6ad22e'),
    ('check', ('demo', 'https://demo.invalid/', 'esum', 'tab', (1037, 739), 1.0), None, '6a314e2b33b420009eeaa9c6bb85b93a46acf98a77525c9a8f743d8c32f7995c'),
    ('check', ('demo', 'https://demo.invalid/', 'esum', 'tab', (1037, 739), 1.0), ['first item'], '6245f3e9dd877c7d60b11c8995dbf28248cf2d6c3beda86f5a37bf914ecc6afa'),
    ('check', ('demo', 'https://demo.invalid/', 'esum', 'tab', None, 1.25), None, 'fbd026518c666a23a062ef6cd2a4a9f2361a0d158a0f1b1a03a0ed6e316dd3c1'),
    ('check', ('demo', 'https://demo.invalid/', 'esum', 'tab', None, 1.25), ['first item'], '58bea6e98fffb4d6738484dc11abdc143c07341750d95b0be82d9b57aeb245e7')])
def test_fixed_login_and_check_output_bytes(kind, args, written, digest):
    import hashlib
    import io
    from esum.cli import report_error

    error = (LoginNeeded if kind == "login" else CheckNeeded)(*args)
    outcome = failure_outcome(error if written is None else WriteInterrupted(error, written))
    stream = io.StringIO()
    report_error(stream, outcome.message, outcome.help, False)
    assert hashlib.sha256(stream.getvalue().encode()).hexdigest() == digest



@pytest.mark.parametrize("write_failed", [False, True])
def test_log_failure_after_write_includes_applied_item(monkeypatch, write_failed):
    from esum.core import gate
    target = site(name="demo", base_url="https://demo.invalid", label="Demo")

    @action(target, tier=Tier.REVERSIBLE)
    def save(ctx):
        """Save an item."""

    def log(**record):
        if record["kind"] == "write-result":
            raise OSError("log disk full")

    def write():
        if write_failed:
            raise TimeoutError("write timed out")
        return "ok"

    monkeypatch.setattr(gate, "log_event", log)
    with pytest.raises(WriteInterrupted) as caught:
        execute_writes(target, target.actions["save"], {}, [Write("applied item", write)])
    assert isinstance(caught.value.cause, TimeoutError if write_failed else WriteLogError)
    assert caught.value.written == ([] if write_failed else ["applied item"])
    assert ("Already written: none" if write_failed else "Already written: applied item") in failure_outcome(caught.value).help
