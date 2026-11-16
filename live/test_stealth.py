"""ESUM's browser tabs must not reveal a CDP client (D-56).

patchright-cli before 0.11 sent CDP `Runtime.enable` on every page, and a page can see that: the
inspector serializes `console.log` arguments for the client, which formats an Error's stack and so
calls `Error.prepareStackTrace` (fpscanner's check). The probe page sets one that marks the DOM;
ESUM's `eval` runs in an isolated world but shares the DOM, so it reads the mark. (A `stack`
getter is not called on Chrome 156; the control below caught that.) No network: a `data:` URL.

The control runs the same probe in a throwaway session after `console` turns CDP capture on, so a
probe that stopped detecting anything fails here instead of passing silently.
"""

from __future__ import annotations

import json
import time
from urllib.parse import quote

import pytest

from esum.browser.daemon import BrowserError
from esum.browser.session import _tab_name, session_for

PROBE = quote("""<!doctype html><title>probe</title><script>
function probe() {
  const saved = Error.prepareStackTrace;
  Error.prepareStackTrace = () => { document.documentElement.dataset.cdp = "1"; return ""; };
  console.log(new Error(""));
  Error.prepareStackTrace = saved;
}
probe(); setTimeout(probe, 300); setTimeout(probe, 800);
</script>""")
PROBE_URL = f"data:text/html,{PROBE}"
SETTLE = 1.5  # seconds: the probe logs again at 0.3 s and 0.8 s
CONTROL_SESSION = "esum-stealth-control"


def _probe(run) -> bool:
    """Open the probe in a new named tab via `run(command, *args, **options)`; True if CDP showed."""
    tab = _tab_name()
    try:
        run("open", PROBE_URL, tab=tab)
        time.sleep(SETTLE)
        return json.loads(run("eval", "document.documentElement.dataset.cdp || ''", tab=tab)) == "1"
    finally:
        try:
            run("close", tab=tab)
        except BrowserError:
            pass


def test_esum_tabs_do_not_reveal_cdp():
    session = session_for("managed", None)
    session.ensure_open()
    assert not _probe(session.run), (
        "a page in ESUM's browser session detected a CDP client (Runtime.enable). If patchright-cli "
        "was just upgraded, an older daemon may still be running: `patchright-cli kill-all`")


def test_the_probe_detects_cdp_when_capture_is_on():
    client = session_for("managed", None).client

    def run(command: str, *args: str, **options) -> str:
        return client.command(command, args, session=CONTROL_SESSION, **options)

    try:
        run("open", "about:blank", headless=True)
        run("console")  # session-wide CDP capture: what the probe must notice
        assert _probe(run), "the probe no longer detects CDP; test_esum_tabs_do_not_reveal_cdp proves nothing"
    except BrowserError as e:
        pytest.fail(f"control session failed: {e}")
    finally:
        for command in ("close", "delete-data"):
            try:
                run(command)
            except BrowserError:
                pass
