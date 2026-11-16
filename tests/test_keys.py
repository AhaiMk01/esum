"""Saved site keys (D-79): the OS credential store with a file fallback, `esum keys`, and the local
page that manages them only after the OS confirms the user. No test touches the real credential
store: conftest forces files, and the credential-store tests use an in-memory stand-in."""

from __future__ import annotations

import json
import threading

import httpx
import pytest

from types import SimpleNamespace

from esum import ArgumentError, Result, Tier, Transport, action, cli, site
from esum.core import keys_ui, keystore, osauth, terminal
from esum.core.errors import EXIT_OK, EXIT_USAGE

KEY = "usr_TESTKEY0123456789"


class MemoryKeyring:
    """Stands in for `keyring`: get/set/delete_password on a dict."""

    def __init__(self, fail_set: bool = False):
        self.items: dict[tuple[str, str], str] = {}
        self.fail_set = fail_set

    def get_password(self, service, name):
        return self.items.get((service, name))

    def set_password(self, service, name, value):
        if self.fail_set:
            raise RuntimeError("locked")
        self.items[(service, name)] = value

    def delete_password(self, service, name):
        self.items.pop((service, name))


@pytest.fixture
def ring(monkeypatch):
    kr = MemoryKeyring()
    monkeypatch.setattr(keystore, "_keyring", lambda: kr)
    return kr


def test_tests_never_use_the_real_credential_store():
    assert keystore.store_name() == "file"


def test_file_store_saves_loads_and_deletes(isolated):
    assert keystore.save("forum", {"key": KEY, "created": "2026-11-11"}) == "file"
    assert (isolated / "auth" / "forum.json").exists()
    assert keystore.load("forum")["key"] == KEY
    assert keystore.entries() == [{"site": "forum", "store": "file", "created": "2026-11-11"}]
    assert keystore.delete("forum", expected={"key": "other"}) is False   # a newer login is kept
    assert keystore.delete("forum") is True and keystore.load("forum") is None
    assert keystore.delete("forum") is False


def test_credential_store_keeps_the_key_and_the_index_keeps_only_metadata(isolated, ring):
    value = {"key": KEY, "client_id": "c", "scopes": ["read"], "created": "2026-11-11"}
    assert keystore.save("forum", value) == "keyring"
    assert not (isolated / "auth" / "forum.json").exists()
    assert keystore.load("forum") == value
    index = (isolated / "keys.json").read_text(encoding="utf-8")
    assert KEY not in index and json.loads(index) == {"forum": {"created": "2026-11-11", "scopes": ["read"]}}
    assert keystore.entries() == [{"site": "forum", "store": "keyring", "created": "2026-11-11", "scopes": ["read"]}]
    assert keystore.delete("forum") is True
    assert ring.items == {} and json.loads((isolated / "keys.json").read_text(encoding="utf-8")) == {}


def test_an_old_key_file_moves_into_the_credential_store(isolated, ring):
    path = isolated / "auth" / "keeper.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"key": KEY, "created": "2026-11-01"}), encoding="utf-8")
    assert keystore.load("keeper")["key"] == KEY
    assert not path.exists() and keystore.entries()[0]["store"] == "keyring"


def test_a_failing_credential_store_falls_back_to_a_file(isolated, monkeypatch):
    kr = MemoryKeyring(fail_set=True)
    monkeypatch.setattr(keystore, "_keyring", lambda: kr)
    assert keystore.save("forum", {"key": KEY}) == "file"
    assert keystore.load("forum")["key"] == KEY and keystore.entries()[0]["store"] == "file"


def test_each_home_has_its_own_service(monkeypatch, tmp_path):
    monkeypatch.setenv("ESUM_HOME", str(tmp_path / "a"))
    a = keystore._service()
    monkeypatch.setenv("ESUM_HOME", str(tmp_path / "b"))
    assert a != keystore._service() and a.startswith("esum (")


def test_esum_keys_lists_sites_without_keys(capsys):
    keystore.save("keeper", {"key": KEY, "created": "2026-11-11"})
    assert cli.main(["keys"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "keeper,file,2026-11-11" in out and KEY not in out and "esum keys ui" in out
    assert cli.main(["keys", "--json"]) == EXIT_OK
    assert KEY not in capsys.readouterr().out


def test_keys_ui_is_for_the_users_terminal(capsys, monkeypatch):
    monkeypatch.setattr(terminal, "interactive", lambda: False)
    assert cli.main(["keys", "ui", "--no-open"]) == EXIT_USAGE
    assert "Tell the user to run `esum keys ui` themselves" in capsys.readouterr().out


@pytest.fixture
def keyed():
    """A site whose `login` keeps a pasted key, as a custom site may (no built-in site does now)."""
    target = site(name="keyed", base_url="https://keyed.example", label="Keyed", transport=Transport.DIRECT,
                  extras={"key_entry": "API key from keyed.example"})

    @action(target, tier=Tier.READ)
    def login(ctx):
        """Save a key."""
        key = ctx.ask_secret("key: ").strip()
        if not key.replace("_", "").isalnum():
            raise ArgumentError("that is not a key", help="nothing was saved")
        keystore.save("keyed", {"key": key})
        return Result(data={"saved": True}, text="saved the key")

    target.registry = SimpleNamespace(get=lambda name: target if name == "keyed" else None, problems=[])
    return target


def test_the_page_logs_in_with_the_typed_key_and_no_terminal(action_runner, monkeypatch, keyed):
    monkeypatch.setattr(terminal, "interactive", lambda: False)
    monkeypatch.setattr(cli, "run_action", action_runner)
    reg = keyed.registry
    assert cli.run_captured(reg, ["keyed", "login"])[0] == EXIT_USAGE      # no terminal, no key
    code, out = cli.run_captured(reg, ["keyed", "login"], secret=f" {KEY} ")
    assert code == EXIT_OK and "saved the key" in out and KEY not in out
    assert keystore.load("keyed")["key"] == KEY
    code, out = cli.run_captured(reg, ["keyed", "login"], secret="usr_bad key")
    assert code == EXIT_USAGE and "error:" in out and "bad key" not in out


def test_ask_secret_reads_the_terminal_with_hidden_input(monkeypatch, example_registry):
    from esum import Options
    from esum.core.context import Context
    target = example_registry.get("demo")
    ctx = Context(site=target, action=target.all_actions["private"], options=Options(), http=None)
    monkeypatch.setattr(terminal, "interactive", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "typed")
    assert ctx.ask_secret("key: ") == "typed"
    monkeypatch.setattr(terminal, "interactive", lambda: False)
    with pytest.raises(ValueError, match="the user runs it in their own terminal"):
        ctx.ask_secret("key: ")


# --- the page ------------------------------------------------------------------------------------

class FakeAuth:
    def __init__(self, method="Windows Hello", why="", answer=True):
        self.method, self.why, self.answer = method, why, answer
        self.asked = 0

    def available(self):
        return self.method, self.why

    def verify(self, reason):
        self.asked += 1
        return self.answer


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


SITES = {"keeper": {"label": "Keeper", "entry": "API key from keeper.invalid/keys", "logout": True},
         "approver": {"label": "Approver forum", "entry": None, "logout": True}}


def page(auth=None, ran=None, clock=None):
    ran = [] if ran is None else ran

    def run(site, action, secret):
        ran.append((site, action, secret))
        if action == "logout":
            keystore.delete(site)
        return 0, f"ran {site} {action}"

    return keys_ui.KeysPage(SITES, run, auth=auth or FakeAuth(), clock=clock or Clock())


def test_page_shows_keys_but_never_a_key():
    keystore.save("keeper", {"key": KEY, "created": "2026-11-11"})
    html = page().render()
    assert "keeper" in html and "2026-11-11" in html and KEY not in html
    assert "Unlock with Windows Hello" in html and " disabled" in html


def test_nothing_changes_until_the_os_confirms_the_user():
    keystore.save("keeper", {"key": KEY})
    ran = []
    auth = FakeAuth(answer=False)
    p = page(auth, ran)
    p.post("/remove", {"site": "keeper"})
    assert ran == [] and "locked" in p.message
    p.post("/unlock", {})
    assert auth.asked == 1 and not p.unlocked() and ran == []
    auth.answer = True
    p.post("/unlock", {})
    p.post("/remove", {"site": "keeper"})
    # removing deletes ESUM's copy only; nothing runs against the site
    assert ran == [] and keystore.load("keeper") is None
    assert "deleted the saved keeper key here" in p.message and "revoke" in p.message


def test_unlock_lasts_five_minutes():
    clock = Clock()
    p = page(clock=clock)
    p.post("/unlock", {})
    clock.now += keys_ui.UNLOCK_SECONDS - 1
    assert p.unlocked()
    clock.now += 2
    assert not p.unlocked()


def test_adding_runs_the_sites_login_with_the_typed_key():
    ran = []
    p = page(ran=ran)
    p.post("/unlock", {})
    p.post("/add", {"site": "keeper", "key": KEY})
    assert ran == [("keeper", "login", KEY)]
    p.post("/add", {"site": "approver", "key": KEY})   # Discourse keys need the forum's approval page
    assert len(ran) == 1 and "esum approver login" in p.message


def test_without_a_system_check_the_page_is_read_only():
    p = page(FakeAuth(method=None, why="no fingerprint is enrolled"))
    assert "Read-only: no fingerprint is enrolled" in p.render()
    p.post("/unlock", {})
    assert not p.unlocked() and "can't confirm you" in p.message


@pytest.fixture
def served():
    clock = Clock()
    p = page(clock=clock)
    out = []

    class Out:
        def write(self, text):
            out.append(text)

        def flush(self):
            pass

    thread = threading.Thread(target=keys_ui.serve, args=(p, Out()), kwargs={"open_browser": False, "idle": 30},
                              daemon=True)
    thread.start()
    for _ in range(200):
        if p.origin:
            break
        threading.Event().wait(0.01)
    yield p
    clock.now += 60          # idle: serve() stops
    thread.join(5)


def test_page_needs_the_token_then_its_cookie(served):
    link = f"/?t={served.token}"
    with httpx.Client(base_url=served.origin, follow_redirects=False) as client:
        assert client.get("/").status_code == 403
        assert client.get("/?t=wrong").status_code == 403
        first = client.get(link)
        assert first.status_code == 303 and "HttpOnly; SameSite=Strict" in first.headers["set-cookie"]
        shown = client.get("/")
        assert shown.status_code == 200 and "Saved site keys" in shown.text
        assert "default-src 'none'" in shown.headers["content-security-policy"]
        # with no-referrer a browser's form post carries `Origin: null`, and every change was refused
        assert shown.headers["referrer-policy"] == "same-origin"
    with httpx.Client(base_url=served.origin, follow_redirects=False) as other:   # a copied link
        assert other.get(link).status_code == 403 and other.get("/").status_code == 403


def test_page_refuses_other_hosts_and_origins(served):
    with httpx.Client(base_url=served.origin, follow_redirects=False) as client:
        client.get(f"/?t={served.token}")
        assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
        assert client.post("/unlock").status_code == 403                                   # no Origin
        assert client.post("/unlock", headers={"Origin": "http://evil.example"}).status_code == 403
        assert client.post("/unlock", headers={"Origin": served.origin}).status_code == 303
        assert served.unlocked()


def test_windows_hello_answers_are_read_exactly(monkeypatch):
    monkeypatch.setattr(osauth.sys, "platform", "win32")
    monkeypatch.setattr(osauth, "_powershell", lambda script, **kw: "DeviceNotPresent")
    assert osauth.available()[0] is None and not osauth.verify("x")
    monkeypatch.setattr(osauth, "_powershell", lambda script, **kw: "Available" if "CheckAvailability" in script
                        else "Verified")
    assert osauth.available() == ("Windows Hello", "") and osauth.verify("x")


def test_fprintd_needs_an_enrolled_finger_and_a_match(monkeypatch):
    monkeypatch.setattr(osauth.sys, "platform", "linux")
    monkeypatch.setattr(osauth.shutil, "which", lambda name: f"/usr/bin/{name}")
    outputs = {"fprintd-list": "User me has no fingers enrolled for x.", "fprintd-verify": ""}

    class Done:
        def __init__(self, cmd):
            self.stdout = outputs[cmd[0]]

    monkeypatch.setattr(osauth.subprocess, "run", lambda cmd, **kw: Done(cmd))
    assert osauth.available()[0] is None
    outputs["fprintd-list"] = "Fingerprints for user me on x:\n - #0: right-index-finger"
    outputs["fprintd-verify"] = "Verify result: verify-no-match (done)"
    assert osauth.available()[0] and not osauth.verify("x")
    outputs["fprintd-verify"] = "Verify result: verify-match (done)"
    assert osauth.verify("x")
