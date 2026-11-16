import email.utils
import time

import httpx
import pytest

from esum import HttpError, Options, Transport, site
from esum.core.throttle import Throttle
from esum.transport import ChallengeError, HttpClient, Response, charset_of, decode, retry_wait
from esum.transport.direct import DirectBackend

from conftest import FakeBackend, json_response
from esum import Tier
from esum import action
from esum.core.errors import LoginNeeded
from test_browser_transports import make_site

BASE = "https://f.invalid"


def client(backend, *, dedupe=True, charset=None, sleeps=None, base=BASE):
    s = site(name="f", base_url=base, label="F", charset=charset, headers={"X-Site": "1"})
    return HttpClient(s, Options(), dedupe=dedupe, backend_factory=lambda st, o: backend,
                      sleep=(sleeps.append if sleeps is not None else lambda s: None))


def test_url_resolution():
    c = client(FakeBackend({}), base="https://f.invalid/forum")
    assert c.url("/t/1.json") == "https://f.invalid/forum/t/1.json"
    assert c.url("t/1.json", {"page": 2, "skip": None}) == "https://f.invalid/forum/t/1.json?page=2"
    assert c.url("/s?q=a", {"x": [1, 2]}) == "https://f.invalid/forum/s?q=a&x=1&x=2"
    assert c.url("https://other.invalid/a") == "https://other.invalid/a"
    assert c.url("HTTPS://other.invalid/a") == "HTTPS://other.invalid/a"  # a scheme is case-insensitive
    assert c.url("search?q=a:b") == "https://f.invalid/forum/search?q=a:b"  # a colon alone isn't a scheme


@pytest.mark.parametrize("url", ["file:///C:/Users/x", "FILE:///etc/passwd", "ftp://f.invalid/a"])
def test_an_absolute_non_web_url_is_refused_not_appended_to_the_base(url):
    from esum.core.errors import UnsupportedURL
    backend = FakeBackend({})
    with pytest.raises(UnsupportedURL, match=r"not an http\(s\) URL"):
        client(backend).get(url)
    assert backend.requests == []


@pytest.mark.parametrize("base", ["file:///C:/Users", "ftp://f.invalid", "chrome://settings"])
def test_non_web_urls_are_refused_before_any_backend(base):
    """site() refuses such a base_url; this is the second line, for a Site built some other way.
    curl_cffi (T2) would read a file:// URL from disk."""
    from dataclasses import replace
    from esum.core.errors import UnsupportedURL
    backend = FakeBackend({})
    c = client(backend)
    c.site = replace(c.site, base_url=base)
    with pytest.raises(UnsupportedURL, match=r"not an http\(s\) URL") as e:
        c.get("/x")
    assert "esum-site-author" in e.value.help
    assert backend.requests == []


def test_get_json_headers_and_errors():
    b = FakeBackend({f"{BASE}/ok.json": json_response(f"{BASE}/ok.json", {"a": 1}),
                     f"{BASE}/bad": Response(500, f"{BASE}/bad", {}, b"server   exploded")})
    c = client(b)
    assert c.get_json("/ok.json") == {"a": 1}
    assert b.requests[0].headers == {"X-Site": "1", "Accept": "application/json"}
    with pytest.raises(HttpError) as e:
        c.get_text("/bad")
    assert e.value.status == 500 and "server exploded" in str(e.value)
    assert c.get("/bad", check=False).status == 500
    c.close()
    assert b.closed == 1


def test_post_bodies():
    b = FakeBackend({})
    c = client(b)
    c.request("POST", "/j", json={"x": 1}, check=False)
    c.request("PUT", "/f", data={"a": "b c"}, check=False)
    assert b.requests[0].content == b'{"x": 1}'
    assert b.requests[0].headers["Content-Type"] == "application/json"
    assert b.requests[1].content == b"a=b+c"


def test_429_retry_after_seconds_then_success():
    url = f"{BASE}/x"
    b = FakeBackend({url: [Response(429, url, {"retry-after": "3"}, b""),
                           Response(200, url, {}, b"ok")]})
    sleeps: list[float] = []
    assert client(b, sleeps=sleeps).get_text("/x") == "ok"
    assert sleeps == [3.0]


def test_429_discourse_wait_seconds_and_backoff():
    url = f"{BASE}/x"
    discourse = json_response(url, {"extras": {"wait_seconds": 4}}, status=429)
    plain = Response(429, url, {}, b"slow down")
    b = FakeBackend({url: [discourse, plain, plain, Response(200, url, {}, b"ok")]})
    sleeps: list[float] = []
    assert client(b, sleeps=sleeps).get_text("/x") == "ok"
    assert sleeps == [5.0, 4.0, 8.0]


def test_429_gives_up_after_retries_or_long_wait():
    url = f"{BASE}/x"
    b = FakeBackend({url: Response(429, url, {}, b"")})
    sleeps: list[float] = []
    with pytest.raises(HttpError) as e:
        client(b, sleeps=sleeps).get_text("/x")
    assert e.value.status == 429 and len(sleeps) == 3 and len(b.requests) == 4
    long = FakeBackend({url: Response(429, url, {"retry-after": "600"}, b"")})
    with pytest.raises(HttpError, match="wait 600s"):
        client(long).get_text("/x")


def test_max_retries_retries_more_and_waits_longer():
    url = f"{BASE}/x"
    s = site(name="f", base_url=BASE, label="F")
    b = FakeBackend({url: [Response(429, url, {"retry-after": "120"}, b"")] * 5 + [Response(200, url, {}, b"ok")]})
    sleeps: list[float] = []
    opts = Options(max_retries=5)
    assert HttpClient(s, opts, dedupe=False, backend_factory=lambda st, o: b,
                      sleep=sleeps.append).get_text("/x") == "ok"
    assert sleeps == [120.0] * 5   # a 120 s wait fails the call without --max-retries
    too_long = FakeBackend({url: Response(429, url, {"retry-after": "600"}, b"")})
    with pytest.raises(HttpError, match="up to 300s"):
        HttpClient(s, opts, dedupe=False, backend_factory=lambda st, o: too_long,
                   sleep=lambda x: None).get_text("/x")


def test_min_interval_raises_the_site_spacing_but_never_lowers_it(monkeypatch):
    waits = []
    monkeypatch.setattr(Throttle, "wait", lambda self, key, interval=None: waits.append(interval))
    url = f"{BASE}/p"
    b = FakeBackend({url: Response(200, url, {}, b"x")})
    slow = site(name="g", base_url=BASE, label="G", extras={"min_interval": 2.0})
    plain = site(name="h", base_url=BASE, label="H")
    for s, wanted in ((slow, 3.0), (slow, 0.1), (plain, 1.0), (plain, None)):
        HttpClient(s, Options(min_interval=wanted), dedupe=False, backend_factory=lambda st, o: b).get("/p")
    assert waits == [3.0, 2.0, 1.0, None]


def test_a_429_cool_down_holds_other_processes_not_the_one_that_waited(tmp_path):
    now = [1000.0]
    slept: dict[str, list[float]] = {"first": [], "other": []}

    def throttle(who):
        def sleep(s):
            slept[who].append(round(s, 3))
        return Throttle(tmp_path / "t", clock=lambda: now[0], sleep=sleep)

    first, other = throttle("first"), throttle("other")
    first.cool_down("demo", 40)       # the call that got the 429 sleeps 40 s itself
    first.wait("demo", 0)
    other.wait("demo", 0)             # a parallel call holds its next request until the cool-down ends
    assert slept == {"first": [], "other": [40.0]}
    now[0] += 41
    other.wait("demo", 0)
    other.cool_down("demo", 10_000)   # never longer than MAX_COOLDOWN
    first.wait("demo", 0)
    assert slept["other"] == [40.0] and slept["first"] == [300.0]


def test_reads_retry_short_lived_failures_then_succeed(capsys):
    url = f"{BASE}/x"
    b = FakeBackend({url: [Response(502, url, {}, b"bad gateway"),
                           httpx.ReadTimeout("timed out"),
                           Response(200, url, {}, b"ok")]})
    sleeps: list[float] = []
    assert client(b, sleeps=sleeps).get_text("/x") == "ok"
    assert sleeps == [1.0, 2.0] and len(b.requests) == 3
    err = capsys.readouterr().err
    assert "answered HTTP 502; retrying in 1s" in err and "ReadTimeout; retrying in 2s" in err


def test_reads_give_up_after_two_retries():
    url = f"{BASE}/x"
    b = FakeBackend({url: Response(503, url, {}, b"down")})
    sleeps: list[float] = []
    with pytest.raises(HttpError) as e:
        client(b, sleeps=sleeps).get_text("/x")
    assert e.value.status == 503 and sleeps == [1.0, 2.0] and len(b.requests) == 3
    gone = FakeBackend({url: httpx.ConnectError("refused")})
    with pytest.raises(httpx.ConnectError):
        client(gone).get_text("/x")
    assert len(gone.requests) == 3


def test_writes_and_permanent_errors_are_never_retried():
    url = f"{BASE}/x"
    b = FakeBackend({url: Response(503, url, {}, b"down")})
    with pytest.raises(HttpError):
        client(b).request("POST", "/x", data={"a": 1})   # may have reached the site: not resent
    dropped = FakeBackend({url: httpx.ReadTimeout("timed out")})
    with pytest.raises(httpx.ReadTimeout):
        client(dropped).request("POST", "/x")
    for status in (404, 403, 500):
        b.routes[url] = Response(status, url, {}, b"no")
        with pytest.raises(HttpError):
            client(b).get_text("/x")
    challenge = Response(503, url, {"cf-mitigated": "challenge"}, b"Just a moment...")
    b.routes[url] = challenge
    with pytest.raises(HttpError):
        client(b).get_text("/x")                         # a challenge won't clear by waiting
    assert len(b.requests) == 5 and len(dropped.requests) == 1
    b.routes[url] = Response(202, url, {"x-amzn-waf-action": "challenge"}, b"")   # AWS WAF's
    with pytest.raises(HttpError):
        client(b).get_text("/x")


def test_retry_after_http_date():
    when = email.utils.formatdate(time.time() + 10, usegmt=True)
    wait = retry_wait(Response(429, "u", {"retry-after": when}, b""), 0)
    assert 8 <= wait <= 11


def test_charset_detection_and_override():
    curly = "“quoted” café".encode("cp1252")
    assert charset_of({"content-type": "text/html; charset=ISO-8859-1"}, b"") == "ISO-8859-1"
    assert decode(curly, "ISO-8859-1") == "“quoted” café"  # WHATWG: latin1 label = windows-1252
    meta = b'<html><head><meta charset="iso-8859-1"></head>' + curly
    assert Response(200, "u", {"content-type": "text/html"}, meta).text().endswith("“quoted” café")
    assert Response(200, "u", {}, "naïve".encode()).text() == "naïve"
    assert Response(200, "u", {}, b"x", charset="no-such-charset").text() == "x"
    url = f"{BASE}/p"
    b = FakeBackend({url: Response(200, url, {"content-type": "text/html; charset=utf-8"}, curly)})
    assert client(b, charset="iso-8859-1").get_text("/p") == "“quoted” café"


def test_identical_gets_are_sent_once_per_read_action():
    url = f"{BASE}/c"
    b = FakeBackend({url: Response(200, url, {"x": "y"}, b"v1")})
    c = client(b)
    assert c.get_text("/c") == "v1"
    b.routes[url] = Response(200, url, {}, b"v2")
    again = c.get("/c")
    assert again.text() == "v1" and again.reused and len(b.requests) == 1
    assert c.get_text("/c", headers={"X-Other": "1"}) == "v2"   # different headers: sent
    assert c.get_text("/c", dedupe=False) == "v2"               # opted out: sent
    assert client(b).get_text("/c") == "v2"                     # a new call never reuses
    d = client(b, dedupe=False)                                 # writes' reads are always live
    d.get("/c"), d.get("/c")
    assert len(b.requests) == 6


def test_errors_are_not_reused():
    url = f"{BASE}/e"
    b = FakeBackend({url: Response(503, url, {}, b"down")})
    c = client(b)
    with pytest.raises(HttpError):
        c.get_text("/e")
    b.routes[url] = Response(200, url, {}, b"up")
    assert c.get_text("/e") == "up"


def test_throttle_spaces_requests_per_host(tmp_path):
    now = [100.0]
    slept: list[float] = []

    def sleep(s):
        slept.append(round(s, 3))
        now[0] += s

    t = Throttle(tmp_path / "t", clock=lambda: now[0], sleep=sleep)
    t.wait("a.example", 0.5)          # first request to the host: no wait
    now[0] += 0.2
    t.wait("a.example", 0.5)          # 0.2 s later: waits the remaining 0.3 s
    t.wait("b.example", 0.5)          # another site has its own stamp
    now[0] += 5
    t.wait("a.example", 0.5)          # long after: no wait
    assert slept == [0.3]
    (tmp_path / "t" / "a.example.stamp").write_text(repr(now[0] + 999), encoding="ascii")
    t.wait("a.example", 0.5)          # a stamp from the future never blocks for more than 0.5 s
    assert slept == [0.3, 0.5]
    t.wait("a.example", 0)            # spacing off: returns at once
    assert slept == [0.3, 0.5]


def test_every_send_goes_through_the_site_throttle_with_the_site_interval(monkeypatch):
    waits = []
    monkeypatch.setattr(Throttle, "wait", lambda self, key, interval=None: waits.append((key, interval)))
    url = f"{BASE}/p"
    b = FakeBackend({url: [Response(429, url, {"retry-after": "1"}, b""), Response(200, url, {}, b"x")],
                     "https://cdn.invalid/p": Response(200, "https://cdn.invalid/p", {}, b"y")})
    s = site(name="g", base_url=BASE, label="G", extras={"min_interval": 2.0})
    HttpClient(s, Options(), dedupe=True, backend_factory=lambda st, o: b,
               sleep=lambda s: None).get("/p")
    HttpClient(s, Options(), dedupe=True, backend_factory=lambda st, o: b).get("https://cdn.invalid/p")
    # keyed by site, not host; the 429 retry is spaced too
    assert waits == [("g", 2.0), ("g", 2.0), ("g", 2.0)]


def test_direct_backend_with_mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-site"] == "1"
        return httpx.Response(200, headers=[("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")],
                              content=b'{"ok": true}')

    backend = DirectBackend(httpx.Client(transport=httpx.MockTransport(handler)))
    c = client(backend, dedupe=False)
    resp = c.get("/api")
    assert resp.json() == {"ok": True}
    assert resp.headers["set-cookie"] == "a=1, b=2"
    c.close()


def test_unavailable_transport_is_reported(monkeypatch):
    from esum import transport

    monkeypatch.setitem(transport.BACKEND_MODULES, Transport.IN_PAGE, "esum.transport._missing_t3")
    s = site(name="t", base_url=BASE, label="T", transport=Transport.IN_PAGE)
    with pytest.raises(RuntimeError, match="T3 in-page fetch is not available"):
        HttpClient(s, Options(), dedupe=True).get_text("/x")


# --- escalation on a Cloudflare challenge (D-40) --------------------------------------------------------

CHALLENGE = Response(403, "x", {"content-type": "text/html", "cf-mitigated": "challenge"},
                     b"<title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x'></script>")


def _ladder(responses_by_transport, fallback=Transport.IN_PAGE):
    """A T2 site with a fallback, and one FakeBackend per transport the client asks for."""
    s = site(name="cf", base_url=BASE, label="CF", transport=Transport.BROWSER_HTTP, fallback=fallback)
    made = []

    def factory(st, options):
        made.append(st.transport)
        return FakeBackend(responses_by_transport[st.transport])

    slept = []
    client = HttpClient(s, Options(), dedupe=False, backend_factory=factory, sleep=slept.append)
    return client, made, slept


@pytest.mark.parametrize("check", [True, False])
def test_challenge_escalates_to_the_fallback_and_stays_there(capsys, check):
    url, other = f"{BASE}/latest.json", f"{BASE}/t/1.json"
    client, made, slept = _ladder({
        Transport.BROWSER_HTTP: {url: CHALLENGE},
        Transport.IN_PAGE: {url: json_response(url, {"ok": 1}), other: json_response(other, {"ok": 2})},
    })
    assert client.get_json("latest.json", check=check) == {"ok": 1}
    assert client.get_json("t/1.json") == {"ok": 2}          # the rest of the call stays on T3
    assert made == [Transport.BROWSER_HTTP, Transport.IN_PAGE] and slept == []
    assert "bot check; switching to T3 in-page fetch for this call" in capsys.readouterr().err


def test_a_challenged_write_is_resent_on_the_fallback():
    url = f"{BASE}/bookmarks.json"
    client, made, _ = _ladder({Transport.BROWSER_HTTP: {url: CHALLENGE},
                               Transport.IN_PAGE: {url: json_response(url, {"ok": True})}})
    assert client.request("POST", "bookmarks.json", data={"id": 1}).json() == {"ok": True}
    assert made == [Transport.BROWSER_HTTP, Transport.IN_PAGE]


@pytest.mark.parametrize("check", [True, False])
@pytest.mark.parametrize("transport,cookies,hint", [
    (Transport.DIRECT, True, "transport=Transport.BROWSER_HTTP (T2) or IN_PAGE (T3)"),
    (Transport.BROWSER_HTTP, False, "transport=Transport.IN_PAGE (T3)"),
    (Transport.BROWSER_HTTP, True, "wait about 30 s and rerun"),
    (Transport.IN_PAGE, True, "wait about 30 s and rerun"),
])
def test_last_transport_challenge_raises_without_retry(check, transport, cookies, hint):
    url = f"{BASE}/latest.json"
    s = site(name="nf", base_url=BASE, label="NF", transport=transport,
             extras={"browser_cookies": cookies})
    slept = []
    backend = FakeBackend({url: CHALLENGE})
    client = HttpClient(s, Options(), dedupe=False, backend_factory=lambda st, o: backend,
                        sleep=slept.append)
    with pytest.raises(ChallengeError) as caught:
        client.get("latest.json", check=check)
    assert caught.value.status == 403 and caught.value.url == CHALLENGE.url
    assert caught.value.body == ""
    assert str(caught.value) == (f"HTTP 403 from {CHALLENGE.url}: a bot check page (Cloudflare challenge) "
                                 "instead of the site's response")
    assert hint in caught.value.help
    assert slept == [] and len(backend.requests) == 1


@pytest.mark.parametrize("check", [True, False])
def test_challenge_on_fallback_raises_with_final_transport_help(check):
    url = f"{BASE}/latest.json"
    client, made, slept = _ladder({Transport.BROWSER_HTTP: {url: CHALLENGE},
                                  Transport.IN_PAGE: {url: CHALLENGE}})
    with pytest.raises(ChallengeError) as caught:
        client.get("latest.json", check=check)
    assert "wait about 30 s and rerun" in caught.value.help
    assert made == [Transport.BROWSER_HTTP, Transport.IN_PAGE] and slept == []


@pytest.mark.parametrize("check", [True, False])
@pytest.mark.parametrize("transport,cookies", [
    (Transport.DIRECT, True), (Transport.BROWSER_HTTP, False), (Transport.IN_PAGE, True)])
def test_challenge_with_custom_marker_always_raises(check, transport, cookies):
    url = f"{BASE}/latest.json"
    s = site(name="nf", base_url=BASE, label="NF", transport=transport,
             extras={"challenge_text": "site bot check", "browser_cookies": cookies})
    backend = FakeBackend({url: Response(200, url, {}, b"site bot check")})
    http = HttpClient(s, Options(), dedupe=True, backend_factory=lambda st, o: backend)
    with pytest.raises(ChallengeError) as caught:
        http.get("latest.json", check=check)
    assert str(caught.value) == f"HTTP 200 from {url}: a bot check page instead of the site's response"
    assert caught.value.body == ""
    assert "Cloudflare" not in caught.value.help
    assert "bot check" in caught.value.help


def test_a_plain_403_does_not_escalate():
    url = f"{BASE}/private.json"
    client, made, _ = _ladder({Transport.BROWSER_HTTP: {url: Response(403, url, {}, b'{"errors":["no"]}')},
                               Transport.IN_PAGE: {}})
    assert client.get("private.json", check=False).status == 403 and made == [Transport.BROWSER_HTTP]


def test_fallback_must_be_stronger():
    with pytest.raises(ValueError, match="stronger than T2"):
        site(name="bad", base_url=BASE, label="B", transport=Transport.BROWSER_HTTP, fallback=Transport.DIRECT)
    assert site(name="ok", base_url=BASE, label="O", fallback=Transport.IN_PAGE).transport_short == "T1→T3"


@pytest.mark.parametrize("failure", [None, "planning", "write"])
def test_context_owns_read_and_write_clients_through_execution(action_runner, failure):
    import io
    from esum import Tier, Write, action

    url = f"{BASE}/x"
    read = FakeBackend({url: json_response(url, {"ok": 1})})
    write = FakeBackend({url: json_response(url, {"ok": 2})})
    made = []
    def factory(st, options):
        made.append(st.transport)
        return read if st.transport is Transport.DIRECT else write
    action_runner.backend_factory = factory
    target = site(name="owned", base_url=BASE, label="Owned")

    @action(target, tier=Tier.REVERSIBLE)
    def save(ctx):
        """Exercise managed clients."""
        assert ctx.http_for(Transport.DIRECT) is ctx.http
        ctx.http.get("/x")
        http = ctx.http_for(Transport.BROWSER_HTTP, browser_cookies=True)
        assert ctx.http_for(Transport.BROWSER_HTTP) is http
        http.get("/x")
        if failure == "planning":
            raise RuntimeError("planning failed")
        def run():
            assert read.closed == write.closed == 0
            http.request("POST", "/x")
            if failure == "write":
                raise RuntimeError("write failed")
            return {"ok": True}
        return [Write("save", run) for _ in range(3)]

    def run():
        action_runner(target, target.actions["save"], {}, Options(), stream=io.StringIO())
    if failure:
        from esum.core.errors import WriteInterrupted
        with pytest.raises(WriteInterrupted if failure == "write" else RuntimeError, match=f"{failure} failed"):
            run()
    else:
        run()
        assert len([r for r in write.requests if r.method == "POST"]) == 3
    assert made == [Transport.DIRECT, Transport.BROWSER_HTTP]
    assert read.closed == write.closed == 1


@pytest.mark.parametrize("browser,where", [
    ("managed", "the ESUM browser window (managed Chrome profile, patchright-cli session 'esum')"),
    ("attach", "the Chrome attached at http://127.0.0.1:9222 (patchright-cli session 'esum-attach-127-0-0-1-9222')"),
    ("native", "your everyday Chrome (native mode)"),
])
def test_final_browser_challenge_names_the_selected_browser(browser, where):
    url = f"{BASE}/x"
    backend = FakeBackend({url: CHALLENGE})
    target = site(name="browser", base_url=BASE, label="Browser", transport=Transport.IN_PAGE)
    http = HttpClient(target, Options(browser=browser), dedupe=False, backend_factory=lambda st, o: backend)
    with pytest.raises(ChallengeError) as caught:
        http.get("/x")
    assert where in caught.value.help and "wait about 30 s and rerun" in caught.value.help


@pytest.mark.parametrize("transport", [Transport.DIRECT, Transport.IN_PAGE])
def test_context_ignores_cookie_policy_for_non_t2_clients(transport):
    from esum import Context
    target = site(name="same", base_url=BASE, label="Same", transport=transport,
                  extras={"browser_cookies": False})
    http = HttpClient(target, Options(), dedupe=False)
    ctx = Context(target, None, Options(), http)
    assert ctx.http_for(transport, browser_cookies=False) is http
    assert ctx.http_for(transport, browser_cookies=True) is http
    assert ctx.http_for(transport) is http
    ctx.close()


def test_challenge_error_rejects_the_old_positional_options_argument():
    target = site(name="browser", base_url=BASE, label="Browser", transport=Transport.IN_PAGE)
    with pytest.raises(TypeError, match="positional"):
        ChallengeError(CHALLENGE, target, Options())


# --- Regression tests for the request-reuse, transport and browser fixes of review round 1 -----------

# --- 1-2. nothing outlives a call, so no account sees another's pages (review-r2 #1, r3 #1) --------

def test_retry_after_login_fetches_live(fake_http, action_runner):
    demo = site(name="lo", base_url="https://lo.invalid", label="Logged-out test")

    @action(demo, tier=Tier.READ)
    def feed(ctx):
        """Read a feed that answers logged-out clients with a 200 HTML page."""
        resp = ctx.http.get("/feed.json")
        if not resp.headers.get("content-type", "").startswith("application/json"):
            raise ctx.login_needed("got an HTML page instead of JSON")
        return resp.text()

    url = "https://lo.invalid/feed.json"
    fake_http.routes[url] = Response(200, url, {"content-type": "text/html"}, b"<p>log in</p>")
    with pytest.raises(LoginNeeded):
        action_runner(demo, feed.esum_action, {}, Options())
    # the user logs in; the agent retries the same command
    fake_http.routes[url] = Response(200, url, {"content-type": "application/json"}, b'{"ok": 1}')
    action_runner(demo, feed.esum_action, {}, Options())
    assert len(fake_http.requests) == 2


def test_browser_responses_are_reused_only_within_one_call():
    """T2/T3 answer as whichever account is logged in to the session. A response may answer an
    identical GET of the same call, never a later call (account B must not see account A's page)."""
    url = "https://forum.example/latest.json"
    backend = FakeBackend({url: Response(200, url, {}, b"x")})

    for transport in (Transport.BROWSER_HTTP, Transport.IN_PAGE):
        call = HttpClient(make_site(transport), Options(), dedupe=True,
                          backend_factory=lambda st, o: backend)
        assert not call.get("/latest.json").reused
        assert call.get("/latest.json").reused
        later = HttpClient(make_site(transport), Options(), dedupe=True,
                           backend_factory=lambda st, o: backend)
        assert not later.get("/latest.json").reused
    assert len(backend.requests) == 4


# --- 6. form data and query parameters use the site's charset (review-r3 #4) ------------------------

def test_form_and_query_are_encoded_in_the_declared_charset():
    backend = FakeBackend({})
    latin = make_site(Transport.DIRECT, charset="ISO-8859-1")
    http = HttpClient(latin, Options(), dedupe=False, backend_factory=lambda st, o: backend)
    http.request("POST", "/search.php", data={"query": "Zürich “lounge” ☃", "f[]": ["é", 2]},
                 check=False)
    http.get("/s", params={"q": "Zürich"}, check=False)
    assert backend.requests[0].content == b"query=Z%FCrich+%93lounge%94+%26%239731%3B&f%5B%5D=%E9&f%5B%5D=2"
    assert backend.requests[1].url == "https://forum.example/s?q=Z%FCrich"

    utf8 = HttpClient(make_site(Transport.DIRECT), Options(), dedupe=False,
                      backend_factory=lambda st, o: backend)
    utf8.request("POST", "/x", data={"q": "Zürich"}, check=False)
    assert backend.requests[-1].content == b"q=Z%C3%BCrich"
