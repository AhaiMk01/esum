"""The Discourse engine's test helpers: its fixtures (`tests/fixtures/discourse/`, captured from
meta.discourse.org), `ForumFake`, a backend that answers Discourse URLs from them, and the pytest
fixtures that run the CLI against it. A test module imports the fixtures it uses by name:

    from support.discourse import BASE, TOPIC, ForumFake, backend, cli, engine_sites, forum_registry  # noqa: F401

`forum` is a synthetic site that is nothing but a base URL on the engine. A Discourse site config's
own tests point `ForumFake` at that site's base URL instead (see `site_fake`). `walled` is a synthetic
site with what a forum behind a bot wall declares; `log_in` is a user approving a User API Key.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from esum import Transport
from esum.core.errors import EXIT_OK
from esum.core.registry import Registry
from esum.engines.discourse import parse
from esum.transport import Request, Response

from support import engine_fixtures, engine_sites  # noqa: F401

FIX = engine_fixtures("discourse")
BASE = "https://forum.invalid"
TOPIC = 395331


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def all_posts() -> list[dict]:
    posts = parse.stream_posts(load("topic.json")) + parse.stream_posts(load("topic_posts.json"))
    return sorted(posts, key=lambda p: p["post_number"])


def json_resp(url: str, data, status: int = 200, headers: dict | None = None) -> Response:
    return Response(status, url, {"content-type": "application/json; charset=utf-8", **(headers or {})},
                    json.dumps(data).encode())


class ForumFake:
    """Answers the Discourse endpoints the engine uses, from the fixtures. Records every request."""

    def __init__(self, base: str = BASE, *, mega: bool = False, logged_in: bool = True):
        self.base = base
        self.topic = load("topic.json")
        if mega:
            del self.topic["post_stream"]["stream"]
        self.posts = all_posts()
        self.logged_in = logged_in
        self.user = {"id": 0, "username": "SCRUBBED"}
        self.requests: list[Request] = []
        self.overrides: dict[str, Response] = {}

    def page(self, number: int, asc: bool) -> list[dict]:
        if asc:
            return [p for p in self.posts if p["post_number"] > number][:20]
        return [p for p in reversed(self.posts) if p["post_number"] < number][:20]

    def send(self, request: Request, *, before_send=lambda: None) -> Response:
        before_send()
        self.requests.append(request)
        url = request.url
        if url in self.overrides:
            return self.overrides[url]
        parts = urlsplit(url)
        path, q = parts.path, parse_qs(parts.query)
        if path == f"/t/{TOPIC}.json":
            return json_resp(url, self.topic)
        if path == f"/t/{TOPIC}/posts.json" and "post_ids[]" in q:
            want = {int(i) for i in q["post_ids[]"]}
            return json_resp(url, {"post_stream": {"posts": [p for p in self.posts if p["id"] in want]}})
        if path == f"/t/{TOPIC}/posts.json" and "asc" not in q:  # near: the posts around N, with N
            n = int(q["post_number"][0])
            return json_resp(url, {"post_stream": {"posts": self.page(n, False)[:10][::-1]
                                                   + [p for p in self.posts if p["post_number"] >= n][:10]}})
        if path == f"/t/{TOPIC}/posts.json":
            page = self.page(int(q["post_number"][0]), q["asc"][0] == "true")
            return json_resp(url, {"post_stream": {"posts": page}, "id": TOPIC})
        if path == f"/t/{TOPIC}/last.json":
            return json_resp(url, {"post_stream": {"posts": self.posts[-20:]}})
        if path.startswith(f"/posts/by_number/{TOPIC}/"):
            n = int(path.rsplit("/", 1)[1].removesuffix(".json"))
            return json_resp(url, next(p for p in self.posts if p["post_number"] == n))
        if path.startswith(f"/posts/by-date/{TOPIC}/"):
            when = parse.utc_time(path.rsplit("/", 1)[1].removesuffix(".json"))
            later = [p for p in self.posts if parse.utc_time(p["created_at"]) >= when]
            return json_resp(url, later[0]) if later else Response(404, url, {}, b'{"errors": ["not found"]}')
        if path == "/session/current.json":
            if not self.logged_in:
                return Response(404, url, {"content-type": "text/plain"}, b"")
            return json_resp(url, {"current_user": self.user})
        if path == "/session/csrf.json":
            return json_resp(url, load("csrf.json"))
        if path == "/bookmarks.json" and request.method == "POST":
            return json_resp(url, {"id": 1})
        if path == "/user-api-key/revoke" and request.method == "POST":
            return json_resp(url, {"success": "OK"})
        simple = {"/search.json": "search.json", "/latest.json": "latest.json", "/hot.json": "hot.json",
                  "/top.json": "top.json", "/site.json": "site.json",
                  }
        if path in simple:
            return json_resp(url, load(simple[path]))
        if path == "/posts.json":   # newest first; `before` pages back by post id
            before = int(q["before"][0]) if "before" in q else None
            newest = load("latest_posts.json")["latest_posts"]
            page = (newest if before is None else
                    load("latest_posts_before.json")["latest_posts"] if before == newest[-1]["id"] else [])
            return json_resp(url, {"latest_posts": page})
        if path == "/u/user1.json":
            return json_resp(url, load("user.json"))
        if path == "/u/user1/summary.json":
            return json_resp(url, load("user_summary.json"))
        if path == "/user_actions.json" and q.get("username") == ["user1"]:
            acts = load("user_actions.json")["user_actions"]
            return json_resp(url, {"user_actions": acts[int(q["offset"][0]):][:3]})
        if re.fullmatch(r"/c/\d+/l/latest\.json", path):
            return json_resp(url, load("category_latest.json"))
        return Response(404, url, {"content-type": "text/plain"}, b"not found")

    def close(self) -> None:
        pass

    def paths(self) -> list[str]:
        return [urlsplit(r.url).path for r in self.requests]


def _cat(id: int, name: str, slug: str = "", parent_id: int | None = None, restricted: bool = False) -> dict:
    return {"id": id, "name": name, "slug": slug, "parent_id": parent_id, "restricted": restricted}


WALLED = "https://walled.invalid"
WALLED_CATEGORIES = [_cat(12, "玩卡", "rewards"), _cat(5, "信用卡", "credit-cards", 12),
                     _cat(1, "闲聊"), _cat(67, "钛金", restricted=True)]
SITES = {
    # nothing but a base URL: T1, categories from /site.json
    "forum": f'base_url="{BASE}", label="A test Discourse forum"',
    # behind a bot wall, with a static category map: reads over T2 without the browser's cookies, a challenged
    # call and the bookmark over T3, and a category only members see
    "walled": (f'base_url="{WALLED}", label="A walled test forum", transport=Transport.BROWSER_HTTP, '
               f'fallback=Transport.IN_PAGE, extras={{"categories": {WALLED_CATEGORIES!r}, '
               '"browser_cookies": False, "write_transport": "in_page"}'),
}


@pytest.fixture
def forum_registry(engine_sites) -> Registry:
    """The engines and the synthetic Discourse sites `forum` and `walled`: what `cli` runs. A Discourse
    site config's own test module defines `forum_registry` as the built-in registry instead."""
    return engine_sites("discourse", **SITES)


@pytest.fixture
def backend(action_runner):
    fake = ForumFake()
    action_runner.backend = fake
    return fake


@pytest.fixture
def cli(capsys, forum_registry, monkeypatch, action_runner):
    monkeypatch.setenv("ESUM_SPILL_CHARS", "10000000")  # keep whole threads on stdout

    def run(*argv: str):
        code = action_runner.cli(argv, registry=forum_registry)
        out = capsys.readouterr()
        return code, out.out, out.err
    return run


@pytest.fixture
def site_fake(action_runner):
    """`site_fake(base_url)` -> a `ForumFake` for a real Discourse site config. `fake.used` records, per
    client the action opens, the transport it declared and whether it carries the browser's cookies."""
    def make(base: str, **kwargs) -> ForumFake:
        fake = ForumFake(base, **kwargs)
        fake.used = []

        def factory(site, options):
            assert site.transport is not Transport.DIRECT, f"{site.name} must not use T1"
            fake.used.append(("T2" if site.transport is Transport.BROWSER_HTTP else "T3",
                              site.extras.get("browser_cookies", True)))
            return fake

        action_runner.backend_factory = factory
        return fake
    return make


@pytest.fixture
def walled(site_fake):
    return site_fake(WALLED)


# --- User API Key login (D-46): a user at a terminal ---------------------------------------------------

KEY = "secret-user-api-key-0123456789"


def approve(url: str, key: str = KEY, nonce: str | None = None) -> str:
    """What Discourse does on approval: encrypt the key to the public key in the URL."""
    q = parse_qs(urlsplit(url).query)
    public = serialization.load_pem_public_key(q["public_key"][0].encode())
    body = json.dumps({"key": key, "nonce": nonce or q["nonce"][0], "push": False, "api": 4})
    return base64.b64encode(public.encrypt(body.encode(), padding.PKCS1v15())).decode()


class Terminal:
    """A user at a terminal: answers the prompts, and approves the page the login opens."""

    def __init__(self, scopes="read", approve_with=approve, wrap=0, payloads=1):
        self.lines = [scopes]
        self.url = None
        self.approve = approve_with
        self.wrap = wrap
        self.payloads = payloads

    def isatty(self):
        return True

    def opened(self, url):
        self.url = url

    def readline(self):
        if self.url and self.payloads:
            self.payloads -= 1
            payload = self.approve(self.url)
            parts = [payload[i:i + self.wrap] for i in range(0, len(payload), self.wrap)] if self.wrap else [payload]
            self.lines += parts
        return f"{self.lines.pop(0)}\n" if self.lines else ""


@pytest.fixture(autouse=True)
def terminal_is_stdin(monkeypatch):
    """The fake Terminal stands in for a console: a real check would look for a Windows console."""
    monkeypatch.setattr("esum.core.terminal.interactive", lambda: sys.stdin.isatty())


def log_in(cli, monkeypatch, name: str = "forum", scopes: str = "read", **kw) -> str:
    term = Terminal(scopes, **kw)
    monkeypatch.setattr("sys.stdin", term)
    monkeypatch.setattr("webbrowser.open", term.opened)
    code, out, _ = cli(name, "login")
    assert code == EXIT_OK and KEY not in out
    return out
