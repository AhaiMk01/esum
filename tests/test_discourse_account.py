"""Account and credential regressions exercised through complete offline actions."""
import io
import json

import pytest

from esum import ArgumentError, Credential, Options, Tier, Transport, action, site
from esum.core.errors import WriteInterrupted
from esum.engines.discourse import discourse
from esum.transport import Response
from conftest import FakeBackend, json_response

BASE = "https://forum.invalid"
KEY = {"key": "OLD_TEST_KEY", "client_id": "test", "scopes": ["read", "write"]}


def forum():
    target = site(name="account-test", engine="discourse", base_url=BASE, label="Test",
                  transport=Transport.BROWSER_HTTP, extras={"browser_cookies": False})
    target.engine_obj = discourse
    return target


def save_key(isolated, value):
    path = isolated / "auth" / "account-test.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.mark.parametrize("start", [Transport.IN_PAGE, Transport.BROWSER_HTTP])
@pytest.mark.parametrize("source", ["site", "request", "both", "credential"])
def test_page_send_strips_secret_headers_from_every_source(action_runner, start, source):
    secrets = {"uSeR-aPi-kEy": "TEST_KEY", "USER-API-CLIENT-ID": "TEST_CLIENT"}
    target = site(name="headers-test", base_url=BASE, label="Test", transport=start,
                  fallback=Transport.IN_PAGE if start is Transport.BROWSER_HTTP else None,
                  headers=secrets if source in ("site", "both") else {})
    url = BASE + "/probe"
    first = FakeBackend({url: Response(403, url, {"cf-mitigated": "challenge"}, b"")})
    page = FakeBackend({url: [Response(429, url, {"retry-after": "0"}, b"wait"),
                             Response(200, url, {}, b"ok")]})

    @action(target, tier=Tier.READ)
    def probe(ctx):
        """Read with legacy header sources and a page retry."""
        headers = {**(secrets if source in ("request", "both") else {}), "X-Test": "ok"}
        credential = Credential(secrets, page_safe=True) if source == "credential" else None
        response = ctx.http.get("/probe", headers=headers, credential=credential)
        assert response.transport is Transport.IN_PAGE
        return response.text()

    action_runner(target, target.actions["probe"], {}, Options(), stream=io.StringIO(),
                  backend_factory=lambda st, opts: page if st.transport is Transport.IN_PAGE else first)
    assert len(page.requests) == 2
    for request in page.requests:
        assert request.headers == {"X-Test": "ok"}
    if start is Transport.BROWSER_HTTP:
        assert any(k.lower() == "user-api-key" for k in first.requests[0].headers)


@pytest.mark.parametrize("scopes", [["read"], ["read", "write"]])
def test_bookmark_uses_browser_for_read_only_key(action_runner, isolated, scopes):
    save_key(isolated, {**KEY, "scopes": scopes})

    class Accounts(FakeBackend):
        def send(self, request, *, before_send=lambda: None):
            before_send()
            self.requests.append(request)
            if request.url.endswith("/session/current.json"):
                user = "KEY_USER" if "User-Api-Key" in request.headers else "BROWSER_USER"
                return json_response(request.url, {"current_user": {"username": user}})
            return self.routes[request.url]

    backend = Accounts({BASE + "/t/1.json": json_response(BASE, {"title": "Test"}),
                        BASE + "/session/csrf.json": json_response(BASE, {"csrf": "TEST_CSRF"}),
                        BASE + "/bookmarks.json": json_response(BASE, {"id": 1})})
    action_runner.backend = backend
    output = io.StringIO()
    target = forum()
    action_runner(target, target.all_actions["bookmark"], {"topic": 1}, Options(), stream=output)
    post = next(r for r in backend.requests if r.method == "POST")
    if "write" in scopes:
        assert post.headers["User-Api-Key"] == KEY["key"]
        assert "X-CSRF-Token" not in post.headers
        assert "for @KEY_USER" in output.getvalue()
    else:
        assert all("User-Api-Key" not in r.headers for r in backend.requests)
        assert post.headers["X-CSRF-Token"] == "TEST_CSRF"
        assert "for @BROWSER_USER" in output.getvalue()


def test_read_only_key_browser_account_mismatch_sends_no_post(action_runner, isolated):
    save_key(isolated, {**KEY, "scopes": ["read"]})
    current = BASE + "/session/current.json"
    backend = FakeBackend({
        current: [json_response(current, {"current_user": {"username": "PLANNED"}}),
                  json_response(current, {"current_user": {"username": "OTHER"}})],
        BASE + "/t/1.json": json_response(BASE, {"title": "Test"}),
        BASE + "/session/csrf.json": json_response(BASE, {"csrf": "TEST"}),
    })
    action_runner.backend = backend
    target = forum()
    with pytest.raises(WriteInterrupted) as caught:
        action_runner(target, target.all_actions["bookmark"], {"topic": 1}, Options(), stream=io.StringIO())
    assert "@PLANNED" in str(caught.value) and "@OTHER" in str(caught.value)
    assert not any(r.method == "POST" for r in backend.requests)
    assert not any("User-Api-Key" in r.headers for r in backend.requests)


@pytest.mark.parametrize("meta, posts, post_marked, sends", [
    ({"title": "T", "bookmarks": [{"bookmarkable_type": "Topic", "bookmarkable_id": 1}]}, None, False, 0),
    ({"title": "T", "bookmarks": [{"bookmarkable_type": "Post", "bookmarkable_id": 9}]}, None, False, 1),
    ({"title": "T", "bookmarked": True}, None, False, 0),                 # an older forum: no `bookmarks`
    ({"title": "T", "bookmarks": []}, [2], True, 0),
    ({"title": "T", "bookmarks": []}, [2], False, 1),
])
def test_an_existing_bookmark_is_a_no_op(action_runner, isolated, meta, posts, post_marked, sends):
    save_key(isolated, KEY)

    class Accounts(FakeBackend):
        def send(self, request, *, before_send=lambda: None):
            before_send()
            self.requests.append(request)
            if request.url.endswith("/session/current.json"):
                return json_response(request.url, {"current_user": {"username": "KEY_USER"}})
            return self.routes[request.url]

    backend = Accounts({BASE + "/t/1.json": json_response(BASE, meta),
                        BASE + "/posts/by_number/1/2.json": json_response(
                            BASE, {"id": 9, "username": "u", "bookmarked": post_marked}),
                        BASE + "/bookmarks.json": json_response(BASE, {"id": 5})})
    action_runner.backend = backend
    output = io.StringIO()
    target = forum()
    action_runner(target, target.all_actions["bookmark"], {"topic": 1, "posts": posts}, Options(), stream=output)
    assert sum(r.method == "POST" for r in backend.requests) == sends
    assert ("Already bookmarked (no-op):" in output.getvalue()) == (sends == 0)
