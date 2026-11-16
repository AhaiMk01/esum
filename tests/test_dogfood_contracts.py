"""Offline regressions for what dogfooding found in the shared mechanism: a 429 is rate limiting, and a
rerun hint is the command the user ran. They run on `guarded`, the example world's site behind a bot
wall (root conftest). A site's own dogfooding cases are in that site's folder, an engine's in its
parser tests."""
import pytest

from esum import Options, Transport
from esum.core.errors import HttpError
from esum.core.outcomes import failure_outcome
from esum.transport import ChallengeError, HttpClient, Response


# --- a 429 is rate limiting, not a check ------------------------------------------------------------------

LONG = "https://guarded.invalid/t/1/posts.json?" + "post_ids%5B%5D=12345&" * 30
CF_429 = Response(429, LONG, {"cf-mitigated": "challenge"}, b"<title>Just a moment...</title>")
CF_403 = Response(403, LONG, {"cf-mitigated": "challenge"}, b"<title>Just a moment...</title>")


def guarded_client(guarded, fake_http, routes):
    fake_http.routes[LONG] = routes
    made = []

    def factory(site, options):
        made.append(site.transport)
        return fake_http
    http = HttpClient(guarded, Options(max_retries=0), dedupe=False, backend_factory=factory,
                      sleep=lambda s: None)
    return http, made


@pytest.mark.parametrize("first", [CF_429, CF_403])
def test_cloudflare_429_on_the_last_transport_is_a_rate_limit_error(guarded, fake_http, first):
    # T2 meets a challenge (a 429 one escalates too, D-40), then T3's fetch is rate-limited
    http, made = guarded_client(guarded, fake_http, [first, CF_429])
    with pytest.raises(HttpError) as caught:
        http.get(LONG)
    assert not isinstance(caught.value, ChallengeError)
    assert made == [Transport.BROWSER_HTTP, Transport.IN_PAGE]
    assert caught.value.status == 429 and caught.value.url == LONG
    out = failure_outcome(caught.value, ["guarded", "thread", "--id", "1", "--mode", "full"])
    assert out.exit_code == 1 and out.message.startswith("HttpError: HTTP 429 from https://guarded.invalid/t/1/")
    assert len(out.message) < 400 and LONG not in out.message and "Just a moment" not in out.message
    assert out.help == "The site rate-limited ESUM: wait about 30 s, then rerun `esum guarded thread --id 1 --mode full`"


def test_a_site_asking_for_a_long_wait_gets_that_wait_in_the_help():
    error = HttpError(429, "https://demo.invalid/x", "rate limited")
    error.wait = 120.4
    assert "wait about 120 s, then rerun `esum demo x`" in failure_outcome(error, ["demo", "x"]).help


def test_challenge_message_clips_a_long_url_and_browser_help_is_runnable(guarded, fake_http):
    http, made = guarded_client(guarded, fake_http, [CF_403, CF_403])
    with pytest.raises(ChallengeError) as caught:
        http.get(LONG)
    assert caught.value.url == LONG and LONG not in str(caught.value) and len(str(caught.value)) < 400
    assert caught.value.help.startswith("wait about 30 s and rerun the same command; if the Cloudflare check "
                                        "persists, ask the user to open guarded in ")
    assert "open the site in" not in caught.value.help


# --- review follow-up: rerun hints keep global flags, terminal 429s keep their wait, no Cloudflare HTML ----

class AnswerAll(dict):
    """Routes for a fake backend that answer every URL with the same response list."""

    def __init__(self, *responses):
        super().__init__()
        self.responses = list(responses)

    def get(self, url, default=None):
        resp = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return Response(resp.status, url, resp.headers, resp.content)


def cf_429(retry_after: str | None = None) -> Response:
    headers = {"cf-mitigated": "challenge", "content-type": "text/html",
               **({"retry-after": retry_after} if retry_after else {})}
    return Response(429, "x", headers, b"<title>Just a moment...</title>")


ATTACH = ["guarded", "thread", "--id", "1", "--browser", "attach", "--cdp", "http://localhost:9333",
          "--max-retries", "0"]


def test_a_rate_limited_action_keeps_global_flags_and_no_cloudflare_html(action_runner, fake_http, capsys, guarded,
                                                                         example_registry):
    fake_http.routes = AnswerAll(cf_429())
    assert action_runner.cli(ATTACH, registry=example_registry) == 1
    out = capsys.readouterr().out
    assert "error: HttpError: HTTP 429 from https://guarded.invalid/" in out
    assert "Just a moment" not in out and "body (untrusted)" not in out
    assert ("help: The site rate-limited ESUM: wait about 30 s, then rerun `esum guarded thread --id 1 --browser "
            "attach --cdp http://localhost:9333 --max-retries 0`") in out
    assert "CHECK NEEDED" not in out and "ChallengeError" not in out


def test_rerun_hint_keeps_global_flags_given_before_the_site(action_runner, fake_http, capsys, guarded,
                                                             example_registry):
    fake_http.routes = AnswerAll(cf_429())
    argv = ["--max-retries", "0", "--min-interval", "2", "guarded", "thread", "--id", "1"]
    assert action_runner.cli(argv, registry=example_registry) == 1
    assert "rerun `esum --max-retries 0 --min-interval 2 guarded thread --id 1`" in capsys.readouterr().out


def test_retry_after_on_a_terminal_429_with_zero_retries_is_in_the_help(action_runner, fake_http, capsys, guarded,
                                                                       example_registry):
    fake_http.routes = AnswerAll(cf_429("120"))
    assert action_runner.cli(ATTACH, registry=example_registry) == 1
    assert "wait about 120 s, then rerun" in capsys.readouterr().out


def test_a_longer_wait_on_the_last_attempt_is_the_one_named(guarded, fake_http):
    url = "https://guarded.invalid/latest.json"
    fake_http.routes[url] = [Response(429, url, {"retry-after": "1"}, b"slow down"),
                             Response(429, url, {"retry-after": "90"}, b"slow down")]
    http = HttpClient(guarded, Options(max_retries=1), dedupe=False,
                      backend_factory=lambda s, o: fake_http, sleep=lambda s: None)
    with pytest.raises(HttpError) as caught:
        http.get(url)
    assert len(fake_http.requests) == 2 and caught.value.wait == 90
    assert "wait about 90 s, then rerun `esum guarded latest`" in failure_outcome(caught.value, ["guarded", "latest"]).help
