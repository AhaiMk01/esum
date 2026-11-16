"""Writes challenged on the POST: offline backends, real client escalation and gate log."""
import io
import json

import pytest

from esum import LoginNeeded, Options, Transport, site
from esum.core.errors import WriteInterrupted
from esum.core.outcomes import failure_outcome
from esum.engines.discourse import auth, discourse
from esum.transport import HttpError, Response
from conftest import FakeBackend, json_response


@pytest.mark.parametrize("reply, prior_success", [
    *((reply, False) for reply in ("key", "recover", "forbidden", "twice", "missing", "server",
                                  "ambiguous", "bare", "case", "different")),
    ("forbidden", True), ("twice", True), ("different", True), ("server", True),
])
def test_bookmark_auth_switch(reply, prior_success, monkeypatch, isolated, action_runner):
    base = "https://forum.invalid"
    url = base + "/bookmarks.json"
    challenge = Response(403, url, {"cf-mitigated": "challenge"}, b"")
    bad_csrf = Response(403, url, {}, b'["BAD CSRF"]')
    t2 = FakeBackend({
        base + "/session/current.json": json_response(base, {"current_user": {"username": "TEST"}}),
        base + "/t/1.json": json_response(base, {"title": "Test topic"}),
        base + "/posts/by_number/1/1.json": json_response(base, {"id": 11}),
        base + "/posts/by_number/1/2.json": json_response(base, {"id": 12}),
        url: json_response(url, {"id": 1}) if reply == "key" else challenge,
    })
    if prior_success:
        t2.routes[url] = [json_response(url, {"id": 1}), challenge]
    first = Response(403, url, {}, b"Forbidden") if reply == "forbidden" else (
        Response(500, url, {}, b"server failure") if reply == "server" else bad_csrf)
    if reply == "bare":
        first = Response(403, url, {}, b"BAD CSRF")
    if reply == "case":
        first = Response(403, url, {}, b'["bad csrf"]')
    if reply == "ambiguous":
        first = Response(403, url, {}, b"Action failed: BAD CSRF")
    t3 = FakeBackend({url: [first, bad_csrf if reply == "twice" else json_response(url, {"id": 1})],
                      base + "/session/csrf.json": json_response(base, {} if reply == "missing" else {"csrf": "TEST"}),
                      base + "/session/current.json": json_response(base, {"current_user": {"username": "OTHER" if reply == "different" else "TEST"}})})
    action_runner.backend_factory = lambda site, options: t2 if site.transport is Transport.BROWSER_HTTP else t3
    monkeypatch.setattr(auth, "load_key", lambda name: {"key": "TEST_KEY", "client_id": "test", "scopes": ["read", "write"]})
    forum = site(name="auth-switch", engine="discourse", base_url=base, label="Test",
                 transport=Transport.BROWSER_HTTP, fallback=Transport.IN_PAGE,
                 extras={"browser_cookies": False})
    forum.engine_obj = discourse
    act = forum.all_actions["bookmark"]
    output = io.StringIO()
    if reply in ("key", "recover", "bare", "case"):
        action_runner(forum, act, {"topic": 1, "posts": [1, 2]}, Options(), stream=output)
    else:
        with pytest.raises(WriteInterrupted) as caught:
            action_runner(forum, act, {"topic": 1, "posts": [1, 2]}, Options(), stream=output)
        outcome = failure_outcome(caught.value, ["auth-switch", "bookmark"])
        assert isinstance(caught.value.cause, LoginNeeded if reply in ("missing", "different") else HttpError)
        if reply in ("forbidden", "ambiguous"):
            assert isinstance(caught.value.cause, HttpError)
            assert "not replayed" in outcome.help
        done = "Bookmark post #1 by ? in topic #1 “Test topic” for @TEST on https://forum.invalid" if prior_success else "none"
        assert f"Already written: {done}" in outcome.help
        assert outcome.help.count("Already written:") == 1
        assert caught.value.written == ([done] if prior_success else [])
        if reply in ("forbidden", "ambiguous", "twice"):
            assert "esum auth-switch thread --id 1" in outcome.help
        if reply == "different":
            assert "@TEST" in str(caught.value) and "@OTHER" in str(caught.value)
    assert all("User-Api-Key" not in r.headers and "User-Api-Client-Id" not in r.headers for r in t3.requests)
    page_posts = [r for r in t3.requests if r.method == "POST"]
    csrf_gets = [r for r in t3.requests if r.url.endswith("/session/csrf.json")]
    if reply == "key":
        assert not t3.requests
        assert len([r for r in t2.requests if r.method == "POST"]) == 2
        assert all("X-CSRF-Token" not in r.headers for r in t2.requests)
        assert not any(r.url.endswith("/session/csrf.json") for r in t2.requests)
    elif reply in ("recover", "bare", "case"):
        assert len(page_posts) == 3  # one rejected, exactly one successful POST per item
        assert "X-CSRF-Token" not in page_posts[0].headers
        assert all(r.headers["X-CSRF-Token"] == "TEST" for r in page_posts[1:])
        assert len(csrf_gets) == 2
    elif reply == "different":
        assert not page_posts and not csrf_gets
    else:
        assert len(page_posts) == (2 if reply == "twice" else 1)
        assert len(csrf_gets) == (1 if reply in ("twice", "missing") else 0)
    if reply != "key":
        assert len([r for r in t3.requests if r.url.endswith("/session/current.json")]) == 1
    log = (isolated / "writes.jsonl").read_text(encoding="utf-8")
    assert "TEST_KEY" not in log + output.getvalue()
    events = [json.loads(line) for line in log.splitlines()]
    assert len(events) == 3
    expected = ["ok", "ok"] if reply in ("key", "recover", "bare", "case") else (
        ["ok", "error"] if prior_success else ["error", "skipped"])
    assert [e["outcome"] for e in events[1:]] == expected


def test_key_write_second_item_plain_500_lists_first(monkeypatch, isolated, action_runner):
    base = "https://forum.invalid"
    backend = FakeBackend({
        base + "/session/current.json": json_response(base, {"current_user": {"username": "TEST"}}),
        base + "/t/1.json": json_response(base, {"title": "Test topic"}),
        base + "/posts/by_number/1/1.json": json_response(base, {"id": 11}),
        base + "/posts/by_number/1/2.json": json_response(base, {"id": 12}),
        base + "/bookmarks.json": [json_response(base, {"id": 1}), Response(500, base + "/bookmarks.json", {}, b"server failure")],
    })
    action_runner.backend = backend
    monkeypatch.setattr(auth, "load_key", lambda name: {"key": "TEST_KEY", "client_id": "test", "scopes": ["read", "write"]})
    forum = site(name="key-write", engine="discourse", base_url=base, label="Test",
                 transport=Transport.BROWSER_HTTP, extras={"browser_cookies": False})
    forum.engine_obj = discourse
    with pytest.raises(WriteInterrupted) as caught:
        action_runner(forum, forum.all_actions["bookmark"], {"topic": 1, "posts": [1, 2]}, Options(), stream=io.StringIO())
    outcome = failure_outcome(caught.value, ["key-write", "bookmark"])
    assert isinstance(caught.value.cause, HttpError) and caught.value.cause.status == 500
    assert caught.value.cause.help is None
    item = "Bookmark post #1 by ? in topic #1 “Test topic” for @TEST on https://forum.invalid"
    assert caught.value.written == [item]
    assert outcome.exit_code == 1 and outcome.help.count(item) == 1
    assert outcome.help.count("Already written:") == 1
    assert "never the whole command as is" in outcome.help
    assert len([r for r in backend.requests if r.method == "POST"]) == 2
