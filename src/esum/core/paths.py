"""Where ESUM keeps state: `~/.esum/` (override with the ESUM_HOME environment variable)."""

from __future__ import annotations

import os
from pathlib import Path


def esum_home() -> Path:
    return Path(os.environ.get("ESUM_HOME") or Path.home() / ".esum")


def throttle_dir() -> Path:
    return esum_home() / "throttle"


def custom_sites_dir() -> Path:
    return esum_home() / "sites"


def writes_log() -> Path:
    return esum_home() / "writes.jsonl"


def out_dir() -> Path:
    """Spilled results go to ./esum-out/ in the working directory, where the agent can read them."""
    return Path.cwd() / "esum-out"
