"""Discourse User API Key login (D-46): the flow, where the key goes, and where it never goes."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from esum import Options, Tier, Transport, action, site
from esum.core.errors import EXIT_LOGIN_NEEDED, EXIT_OK, EXIT_USAGE
from esum.engines.discourse import auth
from esum.transport import Credential, Response

from support.discourse import (BASE, KEY, TOPIC, ForumFake, Terminal, approve, backend, cli, engine_sites,  # noqa: F401
                               forum_registry, json_resp, log_in, site_fake, terminal_is_stdin, walled)


def test_login_refuses_without_a_terminal(cli, backend, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("read\n"))
    code, out, _ = cli("forum", "login")
    assert code == EXIT_USAGE and "is interactive" in out and "run `esum forum login` themselves" in out
    assert auth.load_key("forum") is None


def test_login_asks_for_the_chosen_scopes(cli, backend, monkeypatch, capsys):
    term = Terminal("write")
    monkeypatch.setattr("sys.stdin", term)
    monkeypatch.setattr("webbrowser.open", term.opened)
    code, out, err = cli("forum", "login")
    assert code == EXIT_OK and "Scopes?" in err and term.url in err   # prompts and URL go to stderr
    assert term.url.startswith(f"{BASE}/user-api-key/new?")
    q = parse_qs(urlsplit(term.url).query)
    assert q["scopes"] == ["read,write"] and q["application_name"] == ["ESUM"]
    assert q["client_id"][0].startswith("esum-") and len(q["nonce"][0]) == 32
    assert "BEGIN PUBLIC KEY" in q["public_key"][0]


def test_an_empty_answer_means_read(cli, backend, monkeypatch):
    assert "scopes: read" in log_in(cli, monkeypatch, scopes="")


def test_a_wrong_scope_is_refused_before_the_browser_opens(cli, backend, monkeypatch):
    term = Terminal("admin")
    monkeypatch.setattr("sys.stdin", term)
    monkeypatch.setattr("webbrowser.open", term.opened)
    code, out, _ = cli("forum", "login")
    assert code == EXIT_USAGE and "use read, or read,write" in out and term.url is None


def test_login_then_reads_carry_the_key_and_no_output_shows_it(cli, backend, isolated, monkeypatch):
    out = log_in(cli, monkeypatch)
    assert "logged in: SCRUBBED" in out and "scopes: read" in out
    assert [f.name for f in (isolated / "auth").iterdir()] == ["forum.json"]   # no private key on disk
    backend.requests.clear()
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC), "--to", "2")
    assert code == EXIT_OK and KEY not in out
    assert all(r.headers["User-Api-Key"] == KEY for r in backend.requests)
    assert all(r.headers["User-Api-Client-Id"].startswith("esum-") for r in backend.requests)
    code, out, _ = cli("--json", "forum", "whoami")
    data = json.loads(out)["data"]
    assert data["logged_in"] and data["scopes"] == ["read"] and KEY not in out


def test_a_payload_wrapped_over_lines_is_read_whole(cli, backend, monkeypatch):
    log_in(cli, monkeypatch, wrap=60)
    assert auth.load_key("forum")["key"] == KEY


def test_a_bad_paste_can_be_tried_again(cli, backend, monkeypatch):
    calls = []

    def bad_then_good(url):
        calls.append(url)
        return base64.b64encode(b"x" * 256).decode() if len(calls) == 1 else approve(url)

    log_in(cli, monkeypatch, approve_with=bad_then_good, payloads=2)
    assert auth.load_key("forum")["key"] == KEY and len(calls) == 2


def test_a_payload_from_another_login_is_refused(cli, backend, monkeypatch):
    term = Terminal(approve_with=lambda url: approve(url, nonce="f" * 32), payloads=3)
    monkeypatch.setattr("sys.stdin", term)
    monkeypatch.setattr("webbrowser.open", term.opened)
    code, out, _ = cli("forum", "login")
    assert code == EXIT_USAGE and "doesn't belong to this login" in out
    assert auth.load_key("forum") is None


def test_stopping_at_the_prompt_saves_nothing(cli, backend, monkeypatch):
    term = Terminal(payloads=0)
    monkeypatch.setattr("sys.stdin", term)
    monkeypatch.setattr("webbrowser.open", term.opened)
    code, out, _ = cli("forum", "login")
    assert code == EXIT_USAGE and "login cancelled" in out and auth.load_key("forum") is None


def test_logging_in_again_reuses_the_client_id_so_the_old_key_is_replaced(cli, backend, monkeypatch):
    log_in(cli, monkeypatch)
    first = auth.load_key("forum")["client_id"]
    log_in(cli, monkeypatch)
    assert auth.load_key("forum")["client_id"] == first


def test_a_dead_key_asks_for_a_new_login(cli, backend, monkeypatch):
    log_in(cli, monkeypatch)
    backend.logged_in = False
    url = f"{BASE}/t/{TOPIC}.json"
    backend.overrides[url] = Response(403, url, {"content-type": "application/json"},
                                      b'{"errors": ["x"], "error_type": "invalid_access"}')
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC))
    assert code == EXIT_LOGIN_NEEDED and "their own terminal, by running `esum forum login" in out
    assert "no longer works" in out


def test_a_403_with_a_working_key_stays_a_plain_error(cli, backend, monkeypatch):
    log_in(cli, monkeypatch)
    url = f"{BASE}/t/{TOPIC}.json"
    backend.overrides[url] = Response(403, url, {"content-type": "application/json"},
                                      b'{"errors": ["x"], "error_type": "invalid_access"}')
    code, out, _ = cli("forum", "thread", "--id", str(TOPIC))
    assert code == 1 and "HTTP 403" in out and "LOGIN NEEDED" not in out


def test_a_read_key_leaves_the_bookmark_on_t3_without_the_key(cli, walled, monkeypatch):
    log_in(cli, monkeypatch, "walled", "read")
    walled.used.clear()
    walled.requests.clear()
    assert cli("walled", "bookmark", "--topic", str(TOPIC))[0] == EXIT_OK
    assert set(walled.used) == {("T3", True)}
    assert not any("User-Api-Key" in r.headers for r in walled.requests)   # never into a page


def test_a_write_key_bookmarks_over_t2_without_a_browser_or_csrf(cli, walled, monkeypatch):
    log_in(cli, monkeypatch, "walled", "read,write")
    walled.used.clear()
    walled.requests.clear()
    assert cli("walled", "bookmark", "--topic", str(TOPIC))[0] == EXIT_OK
    assert ("T3", True) not in walled.used
    post = next(r for r in walled.requests if r.method == "POST")
    assert post.headers["User-Api-Key"] == KEY and "X-CSRF-Token" not in post.headers
    assert "/session/csrf.json" not in walled.paths()


def test_escalating_to_t3_drops_the_key_from_the_resent_request(action_runner):
    url = f"{BASE}/x"
    challenge = Response(403, url, {"cf-mitigated": "challenge"}, b"")
    t2, t3 = ForumFake(), ForumFake()
    t2.overrides[url] = challenge
    t3.overrides[url] = Response(200, url, {}, b"ok")
    s = site(name="k", base_url=BASE, label="K", transport=Transport.BROWSER_HTTP, fallback=Transport.IN_PAGE)
    @action(s, tier=Tier.READ)
    def probe(ctx):
        """Read using an out-of-page credential."""
        response = ctx.http.get("/x", credential=Credential(auth.headers({"key": KEY, "client_id": "c"})))
        assert response.transport is Transport.IN_PAGE
        assert response.credential is None
        return response.text()

    output = io.StringIO()
    action_runner(s, s.actions["probe"], {}, Options(), stream=output,
                  backend_factory=lambda st, o: t3 if st.transport is Transport.IN_PAGE else t2)
    assert "ok" in output.getvalue()
    assert "User-Api-Key" in t2.requests[0].headers and "User-Api-Key" not in t3.requests[0].headers


def test_a_missing_name_does_not_lose_the_rest_of_a_batch(cli, backend):
    url = f"{BASE}/u/ghost.json"
    backend.overrides[url] = Response(404, url, {"content-type": "application/json"}, b'{"errors": ["x"]}')
    code, out, _ = cli("--json", "forum", "user", "--name", "ghost,user1")
    users = json.loads(out)["data"]["users"]
    assert code == EXIT_OK and users[0] == {"username": "ghost", "error": "no such user", "profile_hidden": False}
    assert users[1]["trust_level"] == 2


def test_a_name_that_needs_a_login_does_not_stop_the_batch(cli, backend):
    url = f"{BASE}/u/locked.json"
    backend.overrides[url] = Response(403, url, {"content-type": "application/json"},
                                      b'{"errors": ["x"], "error_type": "not_logged_in"}')
    code, out, _ = cli("forum", "user", "--name", "locked,user1")
    assert code == EXIT_OK and "trust_level: 2" in out and "needs login: locked" in out
    assert cli("forum", "user", "--name", "locked")[0] == EXIT_LOGIN_NEEDED


@pytest.mark.parametrize("scopes, parsed", [("read", ["read"]), ("write", ["read", "write"]),
                                            (" read , write ", ["read", "write"])])
def test_every_key_can_read(scopes, parsed):
    assert auth.parse_scopes(scopes) == parsed
def test_bookmark_reads_key_file_once(cli, walled, monkeypatch, isolated):
    log_in(cli, monkeypatch, "walled", "read,write")
    key_file = isolated / "auth" / "walled.json"
    reads = []
    read_text = Path.read_text

    def tracked(path, *args, **kwargs):
        if path == key_file:
            reads.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracked)
    assert cli("walled", "bookmark", "--topic", str(TOPIC))[0] == EXIT_OK
    assert reads == [key_file]
