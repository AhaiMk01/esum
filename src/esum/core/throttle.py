"""Per-site request spacing across processes (D-18).

Agents often start several `esum` calls at once, and each call is its own process. Before every
request, `HttpClient` waits here until at least `interval` seconds have passed since the last
request any ESUM process sent to the same site (all of a site's hosts count as one: hn's Algolia
and Firebase APIs share a budget). The last-request time lives in `~/.esum/throttle/<site>.stamp`,
guarded by a file lock, so parallel calls queue up instead of bursting. A site can set its own
spacing with `extras["min_interval"]` (seconds), and a call can raise it with `--min-interval`.

When a site answers HTTP 429, the call records a cool-down until the wait the site asked for
(`<site>.cooldown`), and every ESUM process holds its next request to that site until then, so a
batch of parallel calls backs off together instead of each one hitting the limit again.

Spacing is a courtesy, not a guarantee: if the stamp folder can't be used, the request goes out
without waiting.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable

from filelock import FileLock, Timeout

from .paths import throttle_dir

DEFAULT_INTERVAL = 0.5  # seconds between requests to one site, across all ESUM processes
LOCK_TIMEOUT = 120.0    # a call stuck behind this many seconds of queued requests just goes ahead
MAX_COOLDOWN = 300.0    # never hold a request longer than this for a recorded 429 cool-down


class Throttle:
    def __init__(self, root: Path | None = None, *, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        self.root = root or throttle_dir()
        self.clock = clock
        self.sleep = sleep
        self._own_cooldown: dict[str, float] = {}   # cool-downs this client set; it waits those out itself

    def _path(self, key: str, suffix: str) -> Path:
        return self.root / f"{re.sub(r'[^A-Za-z0-9.-]', '_', key)}.{suffix}"

    def cool_down(self, key: str, seconds: float) -> None:
        """Hold every process's next request to site `key` for `seconds` (after an HTTP 429)."""
        path = self._path(key, "cooldown")
        until = self.clock() + min(seconds, MAX_COOLDOWN)
        self._own_cooldown[key] = max(until, self._own_cooldown.get(key, 0.0))
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            try:
                until = max(until, float(path.read_text(encoding="ascii")))
            except (OSError, ValueError):
                pass
            path.write_text(repr(until), encoding="ascii")
        except OSError:
            return

    def _cooling(self, key: str) -> float:
        try:
            until = float(self._path(key, "cooldown").read_text(encoding="ascii"))
        except (OSError, ValueError):
            return 0.0
        if until <= self._own_cooldown.get(key, 0.0):
            return 0.0
        return max(0.0, min(until - self.clock(), MAX_COOLDOWN))

    def wait(self, key: str, interval: float | None = None) -> None:
        """Block until site `key` may be sent the next request, then record that it is being sent."""
        if pause := self._cooling(key):   # outside the lock: queued calls all wait out the same 429
            self.sleep(pause)
        interval = DEFAULT_INTERVAL if interval is None else interval
        if interval <= 0:
            return
        stamp = self._path(key, "stamp")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with FileLock(str(stamp) + ".lock", timeout=LOCK_TIMEOUT):
                try:
                    last = float(stamp.read_text(encoding="ascii"))
                except (OSError, ValueError):
                    last = 0.0
                # min() guards against a stamp from the future (clock change): never wait longer
                # than one interval.
                delay = min(last + interval - self.clock(), interval)
                if delay > 0:
                    self.sleep(delay)
                stamp.write_text(repr(self.clock()), encoding="ascii")
        except (OSError, Timeout):
            return
