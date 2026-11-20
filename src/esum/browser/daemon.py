"""Client for the patchright-cli daemon.

patchright-cli runs one long-lived daemon (default port 9321) that owns every named session. Its
own `patchright-cli <command>` client sends one length-prefixed JSON message per command over a
local TCP socket. ESUM sends the same message directly instead of spawning a process per command
(several commands per request would cost a Python start-up each). Starting the daemon is left to
patchright-cli: when nothing listens on the port, ESUM runs `patchright-cli` once, which starts it.

The wire format is patchright-cli's published client protocol (its docs/daemon-protocol.md).
`patchright-cli daemon-info --json` reports the installed version, the protocol number and the
auth-token path; ESUM reads it once per install, refuses a protocol it doesn't know, and sends the
token (read fresh per request, never printed). An install without `daemon-info` is older than the
minimum: ESUM reads `--version` only to name it in the upgrade message. The token falls back
to the daemon's default path when ESUM can't ask (patchright-cli not on PATH, daemon running).

Every failed reply reaches callers as a `BrowserError` with a `code` (see `reply_code`), so nothing
outside this module reads patchright-cli's message text.

Because the daemon is shared, the agent's own `patchright-cli -s=<session> …` commands act on the
same sessions and tabs ESUM uses.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import struct
import subprocess
import time
from pathlib import Path
from typing import Any

from ..core.errors import EsumError

DEFAULT_PORT = 9321
MIN_VERSION = (0, 15, 1)  # named tabs (0.8), daemon-info (0.9.1), no CDP Runtime.enable on every page
                          # (0.11): before it, bot checks could tell ESUM's tabs are automated;
                          # in attach and native mode, leaves the user's own tabs alone (0.15.1):
                          # before it, their downloads went to a temp dir that was then deleted
                          # and their dialogs were dismissed (D-105)
COMMAND_TIMEOUT = 120.0  # what patchright-cli's own client allows a command
START_TIMEOUT = 60.0
TOKEN_DIR = Path.home() / ".patchright-cli"  # the daemon's default token directory
PROTOCOLS = frozenset({1})  # daemon client protocol versions this ESUM speaks

# Stable codes on failed replies (patchright-cli 0.11.1+, its docs/daemon-protocol.md).
SESSION_NOT_OPEN = "session_not_open"
TAB_NOT_OPEN = "tab_not_open"
TARGET_CLOSED = "target_closed"  # also a page that closed or crashed outside the daemon
NO_PAGE = "no_page"
AUTH_REJECTED = "auth_rejected"  # ESUM's own: patchright-cli rejects a stale token without a code

# The code a failure's message implies, for replies without one: every daemon's token rejection,
# and every failure from a daemon before 0.11.1 (including one started before an upgrade). Each
# pattern is the daemon's or Playwright's whole message, anchored where the daemon puts it, because
# `eval` passes a page's own exception text through. The texts are pinned in
# tests/test_browser_messages.py.
_CODE_FROM_TEXT = (
    (re.compile(r"^Error: the patchright-cli daemon on port \d+ rejected the auth token"), AUTH_REJECTED),
    (re.compile(r"^(?:Error: )?Tab '[^']*' is not open\b"), TAB_NOT_OPEN),
    (re.compile(r"^(?:Error: )?Session '[^']*' is not open\b"), SESSION_NOT_OPEN),
    (re.compile(r"Target page, context or browser has been closed"), TARGET_CLOSED),
)


# The guard every in-tab evaluation goes through: run `fn(arg)` only on the origin we opened.
_ON_ORIGIN = """(async (origin, fn, arg) => {
  if (location.origin !== origin) return {esum_tab: false};
  return {esum_tab: true, value: await fn(arg)};
})"""


def reply_code(reply: dict[str, Any]) -> str | None:
    """A failed reply's code: the daemon's own, else the one its message implies, else None."""
    if reply.get("success") is not False:
        return None
    if isinstance(code := reply.get("code"), str):
        return code
    output = str(reply.get("output", ""))
    return next((code for pattern, code in _CODE_FROM_TEXT if pattern.search(output)), None)


class BrowserError(EsumError):
    """A patchright-cli command failed. The message is the daemon's output, passed through raw;
    `code` says what failed (`reply_code`), None for a failure with no code."""

    def __init__(self, message: str, help: str | None = None, code: str | None = None):
        super().__init__(message)
        self.help = help
        self.code = code


class DaemonClient:
    """Sends commands to the patchright-cli daemon on `port`, starting it on first use."""

    def __init__(self, port: int = DEFAULT_PORT, *, cwd: str | None = None,
                 executable: str = "patchright-cli"):
        self.port = port
        self.cwd = cwd  # patchright-cli writes snapshot files to <cwd>/.patchright-cli/
        self.executable = executable
        self._started = False
        self._version_ok = False
        self._token_path: Path | None = None  # from daemon-info; None means the default path

    def command(self, command: str, args: list[str] | tuple[str, ...] = (), *, session: str,
                **options: Any) -> str:
        """Run one command in `session` and return its output. Raises BrowserError on failure."""
        self.ensure_running()
        msg = {"command": command, "args": list(args), "options": {"session": session, **options},
               "cwd": self.cwd or os.getcwd()}
        reply = self._send_with_token(msg)
        # a reply without an explicit success flag is a protocol change, not a success
        if not isinstance(reply, dict) or not isinstance(reply.get("success"), bool):
            raise BrowserError(f"patchright-cli {command} (session {session!r}): unexpected daemon "
                               f"reply {str(reply)[:200]!r}; is patchright-cli a supported version?")
        output = str(reply.get("output", ""))
        if not reply["success"]:
            help = None
            if command == "background-open" and (
                output.removeprefix("Error: ").strip() == "Unknown command: background-open"
                or reply_code(reply) == SESSION_NOT_OPEN
            ):
                help = ("Managed mode needs patchright-cli 0.12.0 or newer for background opens: run "
                        "`uv tool install -U patchright-cli`. Then restart the patchright-cli daemon on port "
                        f"{self.port} (`patchright-cli kill-all`) when its sessions may be closed, and rerun. "
                        "ESUM has left those sessions running")
            raise BrowserError(f"patchright-cli {command} (session {session!r}): {output}",
                               help=help, code=reply_code(reply))
        return output

    def evaluate(self, fn: str, arg: Any = None, *, origin: str, session: str, tab: str) -> Any:
        """Evaluate a function behind the origin guard and return its structured reply."""
        expression = (f"({_ON_ORIGIN})({json.dumps(origin)}, ({fn}), "
                      f"{json.dumps(arg, ensure_ascii=True)})")
        return json.loads(self.command("eval", (expression,), session=session, tab=tab))

    def _send_with_token(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Send with the current token; once more with a fresh read if the daemon restarted in
        between and rejected the one just read."""
        for attempt in (1, 2):
            if token := self.token():
                msg["token"] = token
            reply = self._send(msg)
            stale = isinstance(reply, dict) and reply_code(reply) == AUTH_REJECTED
            if not (stale and attempt == 1):
                return reply
        return reply

    def token(self) -> str | None:
        """The daemon's auth token, read fresh each time (a restarted daemon writes a new one).
        None when there is no token file: an older daemon that needs none, or none running."""
        path = self._token_path or TOKEN_DIR / f"daemon-{self.port}.token"
        try:
            return path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def check_version(self) -> None:
        """Learn the installed patchright-cli's version, protocol and token path, and fail clearly
        when ESUM can't talk to it (too old for named tabs, or a protocol ESUM doesn't know). The
        answer is cached per executable (path + modification time), so it costs one run per install
        or upgrade, not one per call."""
        if self._version_ok:
            return
        exe = shutil.which(self.executable)
        if exe is None:
            return  # ensure_running reports a missing patchright-cli when it has to start it
        try:
            stamp = f"{exe}|{os.path.getmtime(exe)}|{self.port}"
        except OSError:
            return  # can't stat it, so can't run it either; starting the daemon reports that
        cache = Path(self.cwd) / "patchright-cli.info.json" if self.cwd else None
        info = None
        try:
            if cache is not None:
                saved = json.loads(cache.read_text(encoding="utf-8"))
                info = saved["info"] if saved.get("stamp") == stamp else None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        if info is None:
            info = self._discover(exe)
            if cache is not None:
                try:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(json.dumps({"stamp": stamp, "info": info}), encoding="utf-8")
                except OSError:
                    pass
        self._apply(info)
        self._version_ok = True

    def _discover(self, exe: str) -> dict[str, Any]:
        """`daemon-info --json` when the install has it, else the version from `--version`."""
        def run(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=START_TIMEOUT, stdin=subprocess.DEVNULL)

        proc = run(f"--port={self.port}", "daemon-info", "--json")
        if proc.returncode == 0:
            try:
                info = json.loads(proc.stdout)
            except ValueError:
                info = None
            if isinstance(info, dict) and isinstance(info.get("protocol"), int):
                return {"version": str(info.get("version", "")), "protocol": info["protocol"],
                        "token_path": info.get("token_path")}
        return {"version": run("--version").stdout.strip(), "protocol": None, "token_path": None}

    def _apply(self, info: dict[str, Any]) -> None:
        version = info.get("version") or ""
        m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", version)
        if not m or (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) < MIN_VERSION:
            raise BrowserError(f"ESUM needs patchright-cli {'.'.join(map(str, MIN_VERSION))} or newer "
                               f"(older ones let bot checks detect its tabs, and in attach and native "
                               f"mode lose downloads and dismiss dialogs in your own tabs); found "
                               f"{version or 'an unknown version'}. Upgrade with "
                               f"`uv tool install -U patchright-cli`",
                               help="Run `uv tool install -U patchright-cli`, then `patchright-cli kill-all` "
                                    "so the daemon restarts on the new version, then rerun this command")
        protocol = info.get("protocol")
        if protocol is not None and protocol not in PROTOCOLS:
            raise BrowserError(f"patchright-cli {version} speaks daemon protocol {protocol}; this ESUM "
                               f"speaks {', '.join(map(str, sorted(PROTOCOLS)))}. Upgrade ESUM, or "
                               f"install a patchright-cli that speaks a supported protocol")
        self._token_path = Path(info["token_path"]) if info.get("token_path") else None

    def ensure_running(self) -> None:
        self.check_version()
        if self._started or self._listening():
            self._started = True
            return
        exe = shutil.which(self.executable)
        if exe is None:
            raise BrowserError(f"{self.executable} is not on PATH; the browser transports need it "
                               f"(install with `uv tool install patchright-cli`, then "
                               f"`uv tool run --from patchright-cli patchright install chrome` if "
                               f"Chrome is missing)")
        # Any command starts the daemon; `list` without an open session just reports that.
        subprocess.run([exe, f"--port={self.port}", "list"], capture_output=True,
                       timeout=START_TIMEOUT, stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + START_TIMEOUT
        while not self._listening():
            if time.monotonic() > deadline:
                raise BrowserError(f"the patchright-cli daemon did not start on port {self.port}")
            time.sleep(0.2)
        self._started = True

    def _listening(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                return True
        except OSError:
            return False

    def _send(self, msg: dict[str, Any]) -> dict[str, Any]:
        # A socket failure (refused, reset, timed out) is a BrowserError like any daemon failure, so
        # tab cleanup that catches BrowserError can't let an OSError replace the action's own error.
        data = json.dumps(msg).encode("utf-8")
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=COMMAND_TIMEOUT) as sock:
                sock.sendall(struct.pack("!I", len(data)) + data)
                (length,) = struct.unpack("!I", _recv_exact(sock, 4))
                return json.loads(_recv_exact(sock, length).decode("utf-8"))
        except OSError as e:
            raise BrowserError(f"patchright-cli daemon on port {self.port}: {msg.get('command')}: "
                               f"{type(e).__name__}: {e}") from e


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise BrowserError("the patchright-cli daemon closed the connection")
        buf.extend(chunk)
    return bytes(buf)
