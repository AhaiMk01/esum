"""How ESUM tells failures apart. Every failed daemon reply becomes a `BrowserError` whose `code` is
the daemon's own (patchright-cli 0.11.1+) or, for a reply without one, the code its message implies
(`reply_code`). The rest of ESUM reads only codes. The codes and the messages are pinned here: if
patchright-cli renames a code or rewords a message, update browser/daemon.py and this file."""

import pytest

from esum.browser.daemon import AUTH_REJECTED, BrowserError, DaemonClient, reply_code
from esum.browser.session import cookie_jar, is_closed, is_not_open, is_tab_gone


def failed(output: str, **extra) -> dict:
    return {"success": False, "output": output, **extra}


# Messages of daemons without codes (0.8 to 0.11.0), and the token rejection (no code in any version).
SESSION_NOT_OPEN = "Session 'esum' is not open. Run 'open' first."
TAB_NOT_OPEN = "Tab 'esum-1a2b3c4d' is not open in session 'esum'. Run `patchright-cli --tab esum-1a2b3c4d open <url>` first."
CLOSED = "Error: Target page, context or browser has been closed"
TOKEN = "Error: the patchright-cli daemon on port 9321 rejected the auth token. Run the client as the same user"


@pytest.mark.parametrize("output, code", [
    (SESSION_NOT_OPEN, "session_not_open"),
    (TAB_NOT_OPEN, "tab_not_open"),
    (CLOSED, "target_closed"),
    (TOKEN, AUTH_REJECTED),
    ("Error: ReferenceError: x is not defined", None),
])
def test_a_reply_without_a_code_gets_the_one_its_message_implies(output, code):
    assert reply_code(failed(output)) == code


@pytest.mark.parametrize("output", [
    "Error: Error: The stream has been closed",                      # a page's own exception
    "Error: page.evaluate: Error: Session 'chat' is not open",       # page text, not the daemon's
    "Error: page.evaluate: Error: Tab 'news' is not open yet",
    "Error: page.evaluate: Error: server rejected the auth token",
])
def test_page_errors_that_sound_alike_get_no_code(output):
    """eval passes a page's exception through; only the daemon's own messages imply a code."""
    assert reply_code(failed(output)) is None


def test_the_daemons_own_code_wins():
    # 0.11.1: a default-tab page that closed outside the daemon; the text never says "has been closed"
    gone = failed("This tab's page closed or crashed outside the daemon.", code="target_closed")
    assert reply_code(gone) == "target_closed"
    # a code ESUM doesn't act on beats text that would imply another
    assert reply_code(failed("No page open; Session 'x' is not open", code="no_page")) == "no_page"
    assert reply_code(failed(CLOSED, code=7)) == "target_closed"  # a non-string code is ignored
    assert reply_code({"success": True, "output": CLOSED}) is None  # only failures have codes


def test_codes_classify_the_failure():
    def err(code):
        return BrowserError("x", code=code)
    assert is_not_open(err("session_not_open")) and not is_tab_gone(err("session_not_open"))
    assert is_tab_gone(err("tab_not_open")) and is_not_open(err("tab_not_open"))
    assert is_closed(err("target_closed")) and not is_not_open(err("target_closed"))
    for other in ("no_page", None):
        assert not (is_closed(err(other)) or is_not_open(err(other)) or is_tab_gone(err(other)))
    # text alone decides nothing outside the daemon client
    assert not is_closed(BrowserError("patchright-cli eval (session 'esum'): " + CLOSED))


@pytest.mark.parametrize("reply, code", [
    (failed("Error: x", code="tab_not_open"), "tab_not_open"),
    (failed(TAB_NOT_OPEN), "tab_not_open"),
    (failed("Error: x"), None),
])
def test_the_client_raises_with_the_code(reply, code):
    client = DaemonClient(port=1)
    client.ensure_running = lambda: None
    client._send_with_token = lambda msg: reply
    with pytest.raises(BrowserError) as e:
        client.command("eval", ["1"], session="esum")
    assert e.value.code == code and str(e.value).endswith(reply["output"])


def test_cookie_list_reads_an_empty_jar():
    assert cookie_jar("0 cookies in this session") == []   # 0.10+
    assert cookie_jar("[]") == []                          # 0.8, 0.9
    assert cookie_jar('[{"name": "a", "value": "b"}]') == [{"name": "a", "value": "b"}]
