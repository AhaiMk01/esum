"""Live smoke checks: real requests to the real sites, through the real `esum` command.

Not part of `uv run pytest` (testpaths are `tests/` and the sites' own `tests/` folders). Run them on purpose:

    uv run pytest live                 # every site
    uv run pytest live -k v2ex         # one site

They use your real ~/.esum (browser profile, logins, request spacing), only read, and check shape,
not content: an action exits 0 and returns the rows or posts a working parser would. A site that
needs a login you don't have is skipped, not failed. Each run writes esum-out/live-report.md with
the date and every check's outcome.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

LOGIN_NEEDED = 3
RUN_TIMEOUT = 300  # a browser site may wait out an anti-bot check
_CLI = "import sys; from esum.cli import main; sys.exit(main(sys.argv[1:]))"


class Esum:
    def __call__(self, *args: str, refused: str | None = None) -> dict | None:
        """Run `esum <args> --json` and return its `data`; skip on LOGIN NEEDED, fail on any error.
        An error whose message has `refused` in it returns None instead: the site's own answer."""
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "ESUM_SPILL_CHARS": str(10**9)}
        proc = subprocess.run([sys.executable, "-c", _CLI, *args, "--json"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", env=env,
                              timeout=RUN_TIMEOUT)
        if proc.returncode == LOGIN_NEEDED:
            pytest.skip(f"login needed: esum {' '.join(args)}")
        if refused and proc.returncode != 0 and refused in proc.stdout:
            return None
        assert proc.returncode == 0, (f"esum {' '.join(args)} exited {proc.returncode}:\n"
                                      f"{proc.stderr.strip() or proc.stdout.strip()}")
        out = json.loads(proc.stdout)
        assert out.get("untrusted_content") is True
        return out["data"]


@pytest.fixture(scope="session")
def esum() -> Esum:
    return Esum()


# The root conftest.py serves the offline tests; its autouse guards must not reach live checks.
@pytest.fixture
def offline_network():
    """Off here: live checks make real requests."""


@pytest.fixture
def isolated():
    """Off here: live checks use the real ~/.esum (browser profile, logins, request spacing)."""


_results: list[tuple[str, str, str]] = []


def pytest_runtest_logreport(report):
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        why = ""
        if report.outcome == "skipped" and isinstance(report.longrepr, tuple):
            why = report.longrepr[2]
        elif report.outcome == "failed":
            crash = getattr(report.longrepr, "reprcrash", None)
            why = (crash.message if crash else str(report.longrepr)).splitlines()[0][:200]
        _results.append((report.nodeid.split("::", 1)[-1], report.outcome, why))


def pytest_sessionfinish(session, exitstatus):
    if not _results or hasattr(session.config, "workerinput"):  # run without -n for a full report
        return
    out = Path("esum-out")
    out.mkdir(exist_ok=True)
    lines = [f"# ESUM live check, {datetime.now().astimezone():%Y-%m-%d %H:%M %Z}", "",
             "| Check | Outcome | Note |", "|---|---|---|"]
    lines += [f"| {name} | {outcome} | {why.replace('|', '/')} |" for name, outcome, why in _results]
    (out / "live-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
