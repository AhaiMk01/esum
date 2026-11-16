"""One offline action entry point preserves declared transports and owned-client lifetimes."""
import io

import pytest

from esum import Options, Tier, Transport, action, site
from esum.browser.session import BrowserSession
from conftest import FakeBackend, json_response
from esum.transport import Response


@pytest.fixture(autouse=True)
def no_browser(monkeypatch):
    monkeypatch.setattr(BrowserSession, "ensure_open", lambda self: pytest.fail("fake action started a browser"))


@pytest.mark.parametrize("transport", list(Transport))
def test_declared_transport_uses_shared_fake(action_runner, fake_http, transport):
    target = site(name="seam", base_url="https://seam.invalid", label="Seam", transport=transport)
    url = target.base_url + "/data"
    fake_http.routes[url] = json_response(url, {"value": "offline"})

    @action(target, tier=Tier.READ)
    def read(ctx):
        """Read offline data."""
        return ctx.http.get_json("/data")

    output = io.StringIO()
    action_runner(target, target.actions["read"], {}, Options(json=True), stream=output)
    assert '"value": "offline"' in output.getvalue()
    assert len(fake_http.requests) == 1 and fake_http.closed == 1


@pytest.mark.parametrize("initial,fallback", [
    (Transport.DIRECT, Transport.BROWSER_HTTP),
    (Transport.DIRECT, Transport.IN_PAGE),
    (Transport.BROWSER_HTTP, Transport.IN_PAGE),
])
def test_fallback_reuses_injected_factory(action_runner, initial, fallback):
    target = site(name="seam", base_url="https://seam.invalid", label="Seam",
                  transport=initial, fallback=fallback)
    url = target.base_url + "/data"
    first = FakeBackend({url: Response(403, url, {"cf-mitigated": "challenge"}, b"check")})
    second = FakeBackend({url: json_response(url, {"value": "cleared"})})
    made = []

    def factory(site, options):
        made.append(site.transport)
        return first if site.transport is initial else second

    @action(target, tier=Tier.READ)
    def read(ctx):
        """Read after escalation."""
        return ctx.http.get_json("/data")

    output = io.StringIO()
    action_runner(target, target.actions["read"], {}, Options(json=True), stream=output,
                  backend_factory=factory)
    assert '"value": "cleared"' in output.getvalue()
    assert made == [initial, fallback]
    assert first.closed == second.closed == 1


@pytest.mark.parametrize("initial", list(Transport))
@pytest.mark.parametrize("other", list(Transport))
def test_http_for_inherits_factory(action_runner, initial, other):
    target = site(name="seam", base_url="https://seam.invalid", label="Seam", transport=initial)
    url = target.base_url + "/data"
    backends = {transport: FakeBackend({url: json_response(url, transport.value)}) for transport in Transport}
    made = []

    def factory(site, options):
        made.append(site.transport)
        return backends[site.transport]

    action_runner.backend_factory = factory

    @action(target, tier=Tier.READ)
    def read(ctx):
        """Read through two client transports."""
        first = ctx.http.get_json("/data")
        http = ctx.http_for(other)
        assert ctx.http_for(other) is http
        return [first, http.get_json("/data")]

    output = io.StringIO()
    action_runner(target, target.actions["read"], {}, Options(json=True), stream=output)
    assert initial.value in output.getvalue() and other.value in output.getvalue()
    assert made == ([initial] if initial is other else [initial, other])
    assert all(backends[transport].closed == 1 for transport in made)
