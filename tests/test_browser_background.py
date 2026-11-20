import pytest
from test_browser_transports import FakeDaemon, FakeTab

from esum.browser.session import BrowserSession


def managed_options(isolated):
    return {"persistent": True, "profile": str(isolated / "browser" / "profile"),
            "background": True}


def test_managed_ensure_promotes_an_existing_session_without_replacing_tabs(isolated):
    daemon = FakeDaemon()
    existing = FakeTab("https://forum.example/keep")
    daemon.sessions["esum"] = {"tabs": {"default": existing}, "options": {}}

    BrowserSession(client=daemon).ensure_open()

    assert daemon.calls == [("background-open", (), "esum", managed_options(isolated))]
    assert daemon.tabs() == {"default": existing}


@pytest.mark.parametrize("read_identity", [False, True])
def test_every_managed_open_carries_background_policy_and_profile(isolated, read_identity):
    daemon = FakeDaemon()
    session = BrowserSession(client=daemon)

    if read_identity:
        assert session.identity().user_agent == "Mozilla/5.0 FakeChrome/140"
    else:
        tab = session.open_tab("https://forum.example/read")
        tab.close()

    opens = [call for call in daemon.calls if call[0] in ("open", "background-open")]
    assert len(opens) == 2
    for command, args, name, options in opens:
        assert command == "background-open" and name == "esum"
        assert {key: options[key] for key in managed_options(isolated)} == managed_options(isolated)


@pytest.mark.parametrize("read_identity", [False, True])
def test_named_open_recreates_managed_profile_after_daemon_replacement(isolated, read_identity):
    class ReplacedDaemon(FakeDaemon):
        replace_on_named_open = False

        def command(self, command, args=(), *, session, **options):
            if self.replace_on_named_open and command in ("open", "background-open") and "tab" in options:
                self.sessions.clear()
                self.replace_on_named_open = False
            return super().command(command, args, session=session, **options)

    daemon = ReplacedDaemon()
    session = BrowserSession(client=daemon)
    session.ensure_open()
    daemon.replace_on_named_open = True

    if read_identity:
        assert session.identity().user_agent == "Mozilla/5.0 FakeChrome/140"
    else:
        tab = session.open_tab("https://forum.example/read")
        tab.close()

    assert daemon.sessions["esum"]["options"] == {
        **managed_options(isolated), "tab": next(call[3]["tab"] for call in daemon.calls
                                                if "tab" in call[3])}
    assert not any(call[0] == "open" for call in daemon.calls)
