"""A saved key never leaves its site: `HttpClient` drops it on a redirect to another origin (T1 and
T2 alike), scrubbed from transport errors, refused when malformed, and the key store reports a
credential store that won't let go of a key instead of claiming it was deleted."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from esum import ArgumentError, Options, Result, Tier, action, site
from esum.core import keystore
from esum.core.errors import KeystoreError
from esum.core.model import Transport
from esum.core.errors import TransportFailure
from esum.transport import MAX_REDIRECTS, Credential, HttpClient, keeps_credential, without_credential
from esum.transport.browser_http import BrowserHttpBackend
from esum.transport.direct import DirectBackend

KEY = "usr_TESTKEY0123456789"
API = "https://data.offer.love/v1/my-offers?q=x"


@pytest.mark.parametrize("target, kept", [
    ("https://data.offer.love/v2/x", True),
    ("http://data.offer.love/v1/x", False),          # a downgrade to http
    ("https://data.offer.love:8443/v1/x", False),
    ("https://other.example/collect", False),
    ("https://evil.data.offer.love.example/x", False),
])
def test_which_redirects_keep_the_credential(target, kept):
    assert keeps_credential("https://data.offer.love/v1/my-offers", target) is kept
    assert keeps_credential("http://a.example/x", "https://a.example/y")   # an upgrade keeps it


class CurlHeaders(dict):
    def multi_items(self):
        return list(self.items())


class FakeCurl:
    """curl_cffi's session: follows redirects itself when asked to, like libcurl."""

    def __init__(self, routes, seen):
        self.routes, self.seen = routes, seen

    def request(self, method, url, headers=None, content=None, timeout=None, allow_redirects=True):
        for _ in range(50):
            self.seen.append((method, url, (headers or {}).get("x-api-key")))
            status, location = self.routes.get(url, (200, None))
            if not (allow_redirects and location):
                break
            url = location
        return SimpleNamespace(status_code=status, url=url, content=content or b"{}",
                               headers=CurlHeaders({"location": location} if location else {}))


def client(kind: Transport, routes: dict):
    """An HttpClient on a real T1 or T2 backend over a canned network; `seen` lists what went out."""
    seen: list[tuple[str, str, str | None]] = []
    target = site(name=f"redirect-t{kind.rank}", base_url="https://data.offer.love", label="T",
                  transport=kind, extras={"browser_cookies": False, "min_interval": 0})
    if kind is Transport.DIRECT:
        def handler(request):
            seen.append((request.method, str(request.url), request.headers.get("x-api-key")))
            status, location = routes.get(str(request.url), (200, None))
            return httpx.Response(status, headers={"location": location} if location else {},
                                  content=request.content or b"{}")
        backend = DirectBackend(httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True))
    else:
        backend = BrowserHttpBackend(target, SimpleNamespace(where="w"), session_factory=lambda: FakeCurl(routes, seen))
    return HttpClient(target, Options(), dedupe=False, backend_factory=lambda s, o: backend), seen


BOTH = pytest.mark.parametrize("kind", [Transport.DIRECT, Transport.BROWSER_HTTP], ids=["T1", "T2"])
CREDENTIAL = Credential({"x-api-key": KEY})
OTHER = "https://other.example/collect"


@BOTH
def test_the_key_is_dropped_on_a_redirect_to_another_host_and_stays_dropped(kind):
    http, seen = client(kind, {API: (302, OTHER), OTHER: (302, "https://data.offer.love/back")})
    resp = http.request("GET", API, credential=CREDENTIAL)
    assert seen == [("GET", API, KEY), ("GET", OTHER, None), ("GET", "https://data.offer.love/back", None)]
    assert resp.url == "https://data.offer.love/back"
    assert resp.credential is None            # the answer did not come from a keyed request


@BOTH
def test_the_key_stays_on_its_own_host_and_is_dropped_on_a_downgrade(kind):
    v2, plain = "https://data.offer.love/v2/my-offers", "http://data.offer.love/plain"
    http, seen = client(kind, {API: (301, "/v2/my-offers"), v2: (302, plain)})
    resp = http.request("GET", API, credential=CREDENTIAL)
    assert seen == [("GET", API, KEY), ("GET", v2, KEY), ("GET", plain, None)] and resp.credential is None


@BOTH
def test_a_redirected_post_keeps_its_method_and_body_only_where_a_browser_would(kind):
    again, done = "https://data.offer.love/again", "https://data.offer.love/done"
    http, seen = client(kind, {API: (307, again), again: (303, done)})
    resp = http.request("POST", API, credential=CREDENTIAL, content=b"payload")
    assert seen == [("POST", API, KEY), ("POST", again, KEY), ("GET", done, KEY)]
    assert resp.content == b"{}" and resp.credential is CREDENTIAL   # the body is not replayed as a GET


@BOTH
def test_a_keyed_request_that_never_stops_redirecting_is_an_error(kind):
    loop = "https://data.offer.love/loop"
    http, seen = client(kind, {API: (302, loop), loop: (302, loop)})
    with pytest.raises(httpx.TooManyRedirects):
        http.request("GET", API, credential=CREDENTIAL)
    assert len(seen) == MAX_REDIRECTS + 1 and all(key == KEY for _, _, key in seen)


@BOTH
def test_a_chain_that_ends_at_the_limit_is_accepted(kind):
    hops = [API] + [f"https://data.offer.love/h{n}" for n in range(MAX_REDIRECTS)]
    http, seen = client(kind, dict(zip(hops, [(302, nxt) for nxt in hops[1:]])))
    assert http.request("GET", API, credential=CREDENTIAL).url == hops[-1] and len(seen) == MAX_REDIRECTS + 1


@BOTH
def test_a_request_that_refuses_redirects_gets_the_redirect_itself(kind):
    http, seen = client(kind, {API: (302, OTHER)})
    for credential in (None, CREDENTIAL):
        resp = http.request("GET", API, credential=credential, check=False, follow_redirects=False)
        assert resp.status == 302 and resp.headers["location"] == OTHER
    assert [url for _, url, _ in seen] == [API, API]             # the other host was never asked


@BOTH
def test_without_a_key_the_backend_follows_redirects_as_before(kind):
    http, seen = client(kind, {API: (302, OTHER)})
    resp = http.request("GET", API)
    assert resp.url == OTHER and [url for _, url, _ in seen] == [API, OTHER]


def shown(error) -> str:
    """Everything a failure can print: its message and a debug traceback with its whole chain."""
    import traceback
    return str(error) + "".join(traceback.format_exception(error))


def chained():
    try:
        try:
            raise ValueError("Illegal header usr_DUMMYKEY1")
        except ValueError as inner:
            raise httpx.LocalProtocolError("request failed") from inner
    except httpx.LocalProtocolError as outer:
        return outer


@pytest.mark.parametrize("error", [
    httpx.LocalProtocolError("Illegal header value b'usr_DUMMYKEY1\\r\\nx'"),
    OSError(22, "Illegal header usr_DUMMYKEY1"),          # str() comes from strerror, not args
    chained(),                                            # a clean message over a cause that quotes it
], ids=["message", "oserror", "cause"])
def test_a_transport_error_quoting_the_key_is_replaced_by_a_clean_one(error):
    clean = without_credential(error, Credential({"x-api-key": "usr_DUMMYKEY1\r\nx"}))
    assert isinstance(clean, TransportFailure) and type(error).__name__ in str(clean)
    assert "usr_DUMMYKEY1" not in shown(clean) and clean.__cause__ is None and not vars(clean)


def test_an_error_that_quotes_no_key_is_left_alone():
    error = httpx.ReadTimeout("timed out")
    assert without_credential(error, Credential({"x-api-key": KEY})) is error
    assert without_credential(error, None) is error


@pytest.fixture
def keyed():
    """A site that keeps a pasted key, as a custom site may: `login` asks for it, `read` sends it."""
    target = site(name="keyed", base_url="https://keyed.example", label="Keyed", transport=Transport.DIRECT,
                  extras={"key_entry": "API key from keyed.example"})

    @action(target, tier=Tier.READ)
    def read(ctx):
        """Read with the saved key."""
        saved = keystore.load("keyed")
        return Result(data=ctx.http.get_json("/me", credential=Credential({"x-api-key": saved["key"]})), text="read")

    @action(target, tier=Tier.READ)
    def login(ctx):
        """Save a key."""
        key = ctx.ask_secret("key: ").strip()
        if not key.isalnum():
            raise ArgumentError("that is not a key", help="nothing was saved")
        keystore.save("keyed", {"key": key})
        return Result(data={"saved": True}, text="saved the key")

    target.registry = SimpleNamespace(get=lambda name: target if name == "keyed" else None, problems=[])
    return target


def test_a_scrubbed_error_raised_by_the_client_hides_its_original(action_runner, fake_http, keyed):
    keystore.save("keyed", {"key": "usr_DUMMYKEY1"})
    url = "https://keyed.example/me"
    fake_http.routes[url] = OSError(22, "Illegal header usr_DUMMYKEY1")
    with pytest.raises(TransportFailure) as caught:
        action_runner(keyed, keyed.all_actions["read"], {}, Options())
    assert "usr_DUMMYKEY1" not in shown(caught.value) and "[credential]" in str(caught.value)


def test_a_read_never_prints_a_key_a_transport_error_quotes(action_runner, fake_http, capsys, keyed):
    bad = "usr_DUMMYKEY1\r\nextra"
    keystore.save("keyed", {"key": bad})                # a key no login would accept
    url = "https://keyed.example/me"
    fake_http.routes[url] = httpx.LocalProtocolError(f"Illegal header value {bad.encode()!r}")
    assert action_runner.cli(["keyed", "read"], registry=keyed.registry) == 1
    out = capsys.readouterr().out
    assert "usr_DUMMYKEY1" not in out and "extra" not in out and "[credential]" in out


# --- a credential store that reads but won't change ----------------------------------------------

class StuckKeyring:
    def __init__(self, items):
        self.items = dict(items)

    def get_password(self, service, name):
        return self.items.get((service, name))

    def set_password(self, service, name, value):
        raise RuntimeError("read-only")

    def delete_password(self, service, name):
        if (service, name) not in self.items:
            raise KeyError("not found")
        raise RuntimeError("read-only")


@pytest.fixture
def stuck(monkeypatch, isolated):
    kr = StuckKeyring({(keystore._service(), "keeper"): '{"key": "usr_OLD", "created": "2026-11-01"}'})
    monkeypatch.setattr(keystore, "_keyring", lambda: kr)
    (isolated / "keys.json").write_text('{"keeper": {"created": "2026-11-01"}}', encoding="utf-8")
    return kr


def test_a_key_the_store_wont_delete_is_reported_and_kept(stuck, isolated):
    with pytest.raises(KeystoreError) as caught:
        keystore.delete("keeper")
    assert "usr_OLD" not in str(caught.value) and caught.value.help
    assert keystore.load("keeper")["key"] == "usr_OLD"
    assert [e["site"] for e in keystore.entries()] == ["keeper"]


def test_a_replacement_is_not_hidden_behind_an_old_key_the_store_kept(stuck, isolated):
    with pytest.raises(KeystoreError):
        keystore.save("keeper", {"key": "usr_NEW"})
    assert not (isolated / "auth" / "keeper.json").exists()
    assert keystore.load("keeper")["key"] == "usr_OLD"


class LockedKeyring(StuckKeyring):
    """Holds a key, then stops answering: after `reads_left` reads, reads fail like writes and deletes."""

    reads_left = 10 ** 6

    def get_password(self, service, name):
        if self.reads_left <= 0:
            raise RuntimeError("locked")
        self.reads_left -= 1
        return super().get_password(service, name)


@pytest.fixture
def locking(monkeypatch, isolated):
    kr = LockedKeyring({(keystore._service(), "keeper"): '{"key": "usr_OLD", "created": "2026-11-01"}'})
    monkeypatch.setattr(keystore, "_keyring", lambda: kr)
    (isolated / "keys.json").write_text('{"keeper": {"created": "2026-11-01"}}', encoding="utf-8")
    return kr


def test_a_store_that_locks_mid_delete_is_not_taken_for_empty(locking, isolated):
    locking.reads_left = 1                             # delete()'s own read works, then the store locks
    with pytest.raises(KeystoreError) as caught:
        keystore.delete("keeper")
    assert "may still be there" in str(caught.value) and "ESUM_KEYSTORE=file" in caught.value.help
    assert '"keeper"' in (isolated / "keys.json").read_text(encoding="utf-8")


def test_a_locked_store_refuses_a_replacement_it_would_later_shadow(locking, isolated):
    locking.reads_left = 0
    with pytest.raises(KeystoreError):
        keystore.save("keeper", {"key": "usr_NEW"})
    assert not (isolated / "auth" / "keeper.json").exists()
    locking.reads_left = 5                             # the store recovers: still the old key, no stray file
    assert keystore.load("keeper")["key"] == "usr_OLD"


def test_a_key_absent_from_a_stuck_store_still_saves_to_a_file(monkeypatch, isolated):
    monkeypatch.setattr(keystore, "_keyring", lambda: StuckKeyring({}))
    assert keystore.save("forum", {"key": "k"}) == "file" and keystore.load("forum") == {"key": "k"}
    assert keystore.delete("forum") is True
