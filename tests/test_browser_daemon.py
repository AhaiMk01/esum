"""The patchright-cli daemon client: wire format, failures, and starting the daemon on demand."""

from __future__ import annotations

import json
import socket
import struct
import threading
from pathlib import Path

import pytest

from esum.browser import daemon as daemon_mod
from esum.browser.daemon import BrowserError, DaemonClient
from esum import Tier
from esum import Transport
from esum import action
from esum import site
from esum.browser.session import BrowserSession
from esum.core.context import Options
from esum.core.runner import run_action
from test_browser_transports import FakeDaemon
import io


class FakeDaemonServer:
    """A local TCP server speaking patchright-cli's protocol: 4-byte length + JSON, one per connection."""

    def __init__(self, reply=None):
        self.reply = reply or (lambda msg: {"success": True, "output": f"ran {msg['command']}"})
        self.messages: list[dict] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                header = conn.recv(4)
                if len(header) < 4:
                    continue  # a liveness probe
                (length,) = struct.unpack("!I", header)
                data = b""
                while len(data) < length:
                    data += conn.recv(length - len(data))
                msg = json.loads(data)
                self.messages.append(msg)
                out = json.dumps(self.reply(msg)).encode()
                conn.sendall(struct.pack("!I", len(out)) + out)

    def close(self):
        self.sock.close()


@pytest.fixture
def server(monkeypatch):
    # no installed patchright-cli is consulted (a test that wants one fakes it with _fake_cli)
    monkeypatch.setattr(daemon_mod.shutil, "which", lambda name: None)
    s = FakeDaemonServer()
    yield s
    s.close()


def test_command_sends_session_options_and_cwd(server, tmp_path):
    client = DaemonClient(server.port, cwd=str(tmp_path))
    out = client.command("open", [], session="esum", persistent=True, profile="P")
    assert out == "ran open"
    msg = server.messages[-1]
    assert msg == {"command": "open", "args": [], "cwd": str(tmp_path),
                   "options": {"session": "esum", "persistent": True, "profile": "P"}}


def test_failure_is_raised_raw(server):
    server.reply = lambda msg: {"success": False, "output": "Session 'esum' is not open. Run 'open' first."}
    with pytest.raises(BrowserError, match="tab-list .*'esum'.*is not open"):
        DaemonClient(server.port).command("tab-list", session="esum")


def test_reply_without_success_flag_is_an_error(server):
    """A reply shape ESUM doesn't know is a protocol change, never a silent success."""
    server.reply = lambda msg: {"output": "done"}
    with pytest.raises(BrowserError, match="unexpected daemon reply"):
        DaemonClient(server.port).command("tab-list", session="esum")


def test_large_reply_is_read_completely(server):
    big = "x" * 3_000_000
    server.reply = lambda msg: {"success": True, "output": big}
    assert DaemonClient(server.port).command("eval", ["1"], session="esum") == big


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_missing_patchright_cli_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(daemon_mod.shutil, "which", lambda name: None)
    with pytest.raises(BrowserError, match="patchright-cli is not on PATH"):
        DaemonClient(_free_port()).command("tab-list", session="esum")


def test_daemon_is_started_through_patchright_cli(monkeypatch):
    port = _free_port()
    started: list[list[str]] = []
    servers: list[FakeDaemonServer] = []

    def fake_run(cmd, **kw):
        started.append(cmd)
        s = FakeDaemonServer()
        s.sock.close()  # rebind on the port the client expects
        s.sock = socket.socket()
        s.sock.bind(("127.0.0.1", port))
        s.sock.listen()
        s.thread = threading.Thread(target=s._serve, daemon=True)
        s.thread.start()
        servers.append(s)

    monkeypatch.setattr(daemon_mod.shutil, "which", lambda name: "C:/bin/patchright-cli.exe")
    monkeypatch.setattr(daemon_mod.subprocess, "run", fake_run)
    client = DaemonClient(port)
    try:
        assert client.command("tab-list", session="esum") == "ran tab-list"
        assert client.command("tab-list", session="esum") == "ran tab-list"
    finally:
        for s in servers:
            s.close()
    assert started == [["C:/bin/patchright-cli.exe", f"--port={port}", "list"]]


# --- version contract (review limitation #5) ---------------------------------------------------------

def _fake_cli(tmp_path, monkeypatch, version_output, daemon_info=None):
    """A patchright-cli executable whose `--version` prints `version_output` and whose
    `daemon-info --json` prints `daemon_info` (a dict), or fails like an install that predates it."""
    exe = tmp_path / "patchright-cli.exe"
    exe.write_bytes(b"")
    runs: list[list[str]] = []

    class Done:
        def __init__(self, out, code=0):
            self.stdout, self.returncode = out, code

    def run(cmd, **kw):
        runs.append(cmd)
        if "daemon-info" in cmd:
            return Done(json.dumps(daemon_info)) if daemon_info else Done("Unknown command", 2)
        return Done(version_output)

    monkeypatch.setattr(daemon_mod.shutil, "which", lambda name: str(exe))
    monkeypatch.setattr(daemon_mod.subprocess, "run", run)
    return exe, runs


def test_too_old_patchright_cli_fails_clearly(tmp_path, monkeypatch):
    _fake_cli(tmp_path, monkeypatch, "patchright-cli 0.5.0\n")
    with pytest.raises(BrowserError, match=r"needs patchright-cli 0\.11 or newer.*found patchright-cli 0\.5\.0.*uv tool install -U patchright-cli") as e:
        DaemonClient(_free_port(), cwd=str(tmp_path)).check_version()
    # the agent upgrades on its own (skills/esum: keep patchright-cli current), so help gives both steps
    assert "`uv tool install -U patchright-cli`, then `patchright-cli kill-all`" in e.value.help


def test_patchright_cli_before_0_11_is_refused(tmp_path, monkeypatch):
    """0.10 speaks the protocol but enables CDP Runtime on every page, which bot checks detect."""
    _fake_cli(tmp_path, monkeypatch, "unused", {"version": "0.10.0", "protocol": 1, "port": 9321,
                                                 "token_path": None})
    with pytest.raises(BrowserError, match=r"needs patchright-cli 0\.11 or newer.*found 0\.10\.0"):
        DaemonClient(_free_port(), cwd=str(tmp_path)).check_version()


def test_version_check_is_cached_per_executable(tmp_path, monkeypatch):
    port = _free_port()
    exe, runs = _fake_cli(tmp_path, monkeypatch, "unused", {"version": "0.11.0", "protocol": 1,
                                                             "port": port, "token_path": None})
    DaemonClient(port, cwd=str(tmp_path)).check_version()
    DaemonClient(port, cwd=str(tmp_path)).check_version()  # a later esum call
    assert runs == [[str(exe), f"--port={port}", "daemon-info", "--json"]]
    exe.write_bytes(b"upgraded")  # a new install changes the modification time
    import os, time
    os.utime(exe, (time.time() + 10, time.time() + 10))
    DaemonClient(port, cwd=str(tmp_path)).check_version()
    assert len(runs) == 2


def test_daemon_info_supplies_the_token_path(tmp_path, monkeypatch, server):
    token = tmp_path / "elsewhere" / "daemon.token"
    token.parent.mkdir()
    token.write_text("s3cret\n", encoding="utf-8")
    _fake_cli(tmp_path, monkeypatch, "unused", {"version": "0.11.0", "protocol": 1, "port": server.port,
                                                 "token_path": str(token)})
    client = DaemonClient(server.port, cwd=str(tmp_path))
    client._started = True
    client.command("tab-list", session="esum")
    assert server.messages[-1]["token"] == "s3cret"


def test_unknown_protocol_is_refused_before_sending(tmp_path, monkeypatch):
    _fake_cli(tmp_path, monkeypatch, "unused", {"version": "1.4.0", "protocol": 2, "port": 9321,
                                                 "token_path": None})
    with pytest.raises(BrowserError, match=r"speaks daemon protocol 2; this ESUM speaks 1"):
        DaemonClient(_free_port(), cwd=str(tmp_path)).check_version()


def test_sends_the_daemon_token_when_it_wrote_one(server, tmp_path, monkeypatch):
    """Without daemon-info (patchright-cli not on PATH) the token is read from the default path, if any."""
    monkeypatch.setattr(daemon_mod, "TOKEN_DIR", tmp_path)
    client = DaemonClient(server.port)
    client._version_ok = True  # no discovery: the default path
    client.command("tab-list", session="esum")
    assert "token" not in server.messages[-1]
    (tmp_path / f"daemon-{server.port}.token").write_text("s3cret\n", encoding="utf-8")
    client.command("tab-list", session="esum")
    assert server.messages[-1]["token"] == "s3cret"


def test_a_stale_token_is_read_again_once(server, tmp_path, monkeypatch):
    """The daemon restarted between reading the token and sending: read it fresh and resend once."""
    monkeypatch.setattr(daemon_mod, "TOKEN_DIR", tmp_path)
    path = tmp_path / f"daemon-{server.port}.token"
    path.write_text("old", encoding="utf-8")

    def reply(msg):
        if msg.get("token") == "old":
            path.write_text("new", encoding="utf-8")
            return {"success": False, "output": "Error: the patchright-cli daemon on port 1 rejected the auth token."}
        return {"success": True, "output": "ok"}

    server.reply = reply
    client = DaemonClient(server.port)
    client._version_ok = True
    assert client.command("tab-list", session="esum") == "ok"
    assert [m["token"] for m in server.messages] == ["old", "new"]


@pytest.mark.parametrize("auth_retry", [False, True])
@pytest.mark.parametrize("existing_session", [False, True])
def test_old_daemon_rejects_managed_open_without_a_foreground_fallback(server, tmp_path, monkeypatch,
                                                                    auth_retry, existing_session):
    from esum.browser.session import BrowserSession

    monkeypatch.setattr(daemon_mod, "TOKEN_DIR", tmp_path)
    path = tmp_path / f"daemon-{server.port}.token"
    path.write_text("old", encoding="utf-8")
    foreground_opens = []

    def reply(msg):
        if auth_retry and msg.get("token") == "old":
            path.write_text("new", encoding="utf-8")
            return {"success": False, "output": "Error: the patchright-cli daemon on port 1 rejected the auth token."}
        if msg["command"] == "background-open":
            if not existing_session:
                return {"success": False, "output": "Session 'esum' is not open. Run 'open' first.",
                        "code": "session_not_open"}
            return {"success": False, "output": "Unknown command: background-open"}
        if msg["command"] == "open":
            foreground_opens.append(msg)
        return {"success": True, "output": "[]"}

    server.reply = reply
    session = BrowserSession(client=DaemonClient(server.port))
    with pytest.raises(BrowserError, match="Unknown command: background-open|Session 'esum' is not open") as raised:
        session.ensure_open()

    assert "0.12.0" in raised.value.help and "uv tool install -U patchright-cli" in raised.value.help
    assert str(server.port) in raised.value.help
    assert "restart" in raised.value.help and "sessions" in raised.value.help
    assert foreground_opens == []
    assert [msg["command"] for msg in server.messages] == ["background-open"] * (2 if auth_retry else 1)
    assert [msg["token"] for msg in server.messages] == (["old", "new"] if auth_retry else ["old"])
    for msg in server.messages:
        assert msg["options"]["background"] is True
        assert msg["options"]["persistent"] is True
        assert msg["options"]["profile"] == str(session.dir / "profile")


def test_evaluate_encodes_the_origin_guard_at_the_daemon_boundary():
    class RecordingDaemon(DaemonClient):
        def command(self, command, args=(), *, session, **options):
            self.call = command, args, session, options
            return '{"esum_tab": true, "value": "ok"}'

    client = RecordingDaemon()
    fn = "async (arg) => arg.text"
    result = client.evaluate(fn, {"text": 'a"b'}, origin="https://forum.example",
                             session="esum", tab="esum-owned")
    assert result == {"esum_tab": True, "value": "ok"}
    command, args, session, options = client.call
    assert (command, session, options) == ("eval", "esum", {"tab": "esum-owned"})
    expression, = args
    assert "if (location.origin !== origin) return {esum_tab: false}" in expression
    assert expression.endswith('("https://forum.example", (async (arg) => arg.text), ' +
                               json.dumps({"text": 'a"b'}) + ')')


# --- Socket failures talking to the patchright-cli daemon are BrowserErrors (review S1) --------------

def _refuse(*args, **kwargs):
    raise ConnectionRefusedError(10061, "No connection could be made because the target machine "
                                        "actively refused it")


def test_connection_refused_is_a_browser_error(monkeypatch):
    client = DaemonClient(9)
    client._version_ok = client._started = True
    monkeypatch.setattr(daemon_mod.socket, "create_connection", _refuse)
    with pytest.raises(BrowserError, match="tab-list.*ConnectionRefusedError") as info:
        client.command("tab-list", session="esum")
    assert isinstance(info.value.__cause__, ConnectionRefusedError)


def test_reset_and_timeout_mid_reply_are_browser_errors(monkeypatch):
    class Sock:
        def __init__(self, error):
            self.error = error

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def sendall(self, data):
            pass

        def recv(self, n):
            raise self.error

    client = DaemonClient(9)
    client._version_ok = client._started = True
    for error in (ConnectionResetError(10054, "reset by peer"), socket.timeout("timed out")):
        monkeypatch.setattr(daemon_mod.socket, "create_connection", lambda *a, e=error, **k: Sock(e))
        with pytest.raises(BrowserError, match="eval"):
            client.command("eval", ["1"], session="esum")


def test_action_error_survives_a_daemon_that_is_gone_at_cleanup(monkeypatch):
    daemon = FakeDaemon()

    def reply(msg):
        options = dict(msg["options"])
        session = options.pop("session")
        if msg["command"] == "eval" and msg["args"][0] not in ("location.href", "navigator.userAgent"):
            value = {"title": "Page", "href": "https://forum.example"}
            if "fetch(a.url" in msg["args"][0]:
                value = {"ok": True, "status": 200, "url": "https://forum.example/latest",
                         "headers": {}, "body": "b2s="}
            return {"success": True, "output": json.dumps({"esum_tab": True, "value": value})}
        try:
            return {"success": True, "output": daemon.command(msg["command"], msg["args"],
                                                              session=session, **options)}
        except BrowserError as e:
            return {"success": False, "output": str(e), **({"code": e.code} if e.code else {})}

    server = FakeDaemonServer(reply)
    try:
        client = DaemonClient(server.port)
        client._version_ok = client._started = True
        session = BrowserSession("managed", None, client=client, sleep=lambda s: None)

        ex = site("ex", base_url="https://forum.example", label="Example",
                  transport=Transport.IN_PAGE)

        @action(ex, tier=Tier.READ)
        def boom(ctx):
            """Fetch a page, then fail while the daemon is gone."""
            ctx.http.get_text("/latest")
            monkeypatch.setattr(daemon_mod.socket, "create_connection", _refuse)
            raise RuntimeError("the action's own error")

        with pytest.raises(RuntimeError, match="the action's own error"):
            run_action(ex, boom.esum_action, {}, Options(), stream=io.StringIO(), session=session)
        assert daemon.named(), "the tab is still open: closing it failed, quietly"
    finally:
        server.close()
