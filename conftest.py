"""Shared test setup for the offline tests: `tests/` and each built-in site's own `tests/` folder
(`src/esum/sites/**/tests/`). It sits at the repository root so both find it; `live/conftest.py`
turns its autouse guards off for the live checks.

Every test runs with ESUM_HOME and the working directory in a temp folder, so no test can touch
~/.esum. Per-site request spacing is off unless a test turns it on.

`example_registry` builds a tiny example world in temp folders (never shipped):
  - engine `demoeng` (a test package on sys.path) with a `topic` action,
  - built-in-style site `one` on it (test package), overriding nothing,
  - custom site `two` on `demoeng` in ESUM_HOME/sites/demoeng/two/site.py,
  - custom standalone site `demo` in ESUM_HOME/sites/demo/site.py (reads, a write, login, spill),
  - custom Markdown site `notes` in ESUM_HOME/sites/notes/SITE.md.
`guarded` adds a site behind a bot wall to it (T2, with T3 as the fallback), for the tests that need one.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import textwrap
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from esum.core import registry, throttle
from esum.core.registry import Registry
from esum.transport import Request, Response


@pytest.fixture(autouse=True)
def offline_network(monkeypatch):
    """Deny outbound network in offline tests; live/conftest.py turns this off for live/.

    Socket guards cover httpx and raw sockets. libcurl bypasses Python sockets, so guard
    its URL option too. Loopback stays available to the daemon and HTTP server tests.
    """
    from curl_cffi import Curl, CurlOpt

    def check_host(host):
        if isinstance(host, bytes):
            host = host.decode("ascii")
        if host == "localhost":
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        pytest.fail(f"offline test attempted outbound network to {host!r}", pytrace=False)

    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    getaddrinfo = socket.getaddrinfo
    setopt = Curl.setopt

    def guarded_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            check_host(address[0])
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            check_host(address[0])
        return connect_ex(sock, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host is not None:
            check_host(host)
        return getaddrinfo(host, *args, **kwargs)

    def guarded_setopt(curl, option, value):
        if option == CurlOpt.URL:
            url = value.decode("utf-8") if isinstance(value, bytes) else value
            check_host(urlsplit(url).hostname)
        return setopt(curl, option, value)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(Curl, "setopt", guarded_setopt)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "esum-home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("ESUM_HOME", str(home))
    monkeypatch.setenv("ESUM_KEYSTORE", "file")   # never the real OS credential store
    monkeypatch.delenv("ESUM_SPILL_CHARS", raising=False)
    monkeypatch.chdir(work)
    monkeypatch.setattr(registry, "_loaded_custom", [])  # per test: which custom code is loaded
    monkeypatch.setattr(throttle, "DEFAULT_INTERVAL", 0.0)  # no per-site spacing unless a test asks
    yield home


@pytest.fixture
def native_profile(tmp_path, monkeypatch):
    """A Chrome profile under the test's home on every supported platform."""
    from esum.browser.session import chrome_user_data_dir

    for name in ("LOCALAPPDATA", "XDG_CONFIG_HOME", "USERPROFILE", "HOME"):
        monkeypatch.setenv(name, str(tmp_path))
    profile = chrome_user_data_dir()
    profile.mkdir(parents=True, exist_ok=True)
    return profile


class FakeBackend:
    """Canned responses keyed by URL; records every request. An exception in a route is raised."""

    def __init__(self, routes: dict[str, list[Response] | Response]):
        self.routes = routes
        self.requests: list[Request] = []
        self.closed = 0

    def send(self, request: Request, *, before_send=lambda: None) -> Response:
        before_send()
        self.requests.append(request)
        route = self.routes.get(request.url)
        if route is None:
            return Response(404, request.url, {}, b"not found")
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if isinstance(route, BaseException):
            raise route
        return route

    def close(self) -> None:
        self.closed += 1


def json_response(url: str, data, status: int = 200, headers: dict | None = None) -> Response:
    return Response(status, url, {"content-type": "application/json", **(headers or {})},
                    json.dumps(data).encode())


@pytest.fixture
def fake_http():
    """Canned HTTP responses; pass this backend to `action_runner`."""
    return FakeBackend({})


@pytest.fixture
def action_runner(fake_http, monkeypatch):
    """Run actions offline on any declared transport, including http_for and escalation.

    `action_runner(site, act, kwargs, options, stream=None, *, backend_factory=None, session=None)`
    uses `fake_http` by default. Set `backend` or `backend_factory` for site-specific fakes.
    `.cli(argv, *, registry=None)` exercises CLI parsing and outcomes through the same seam.
    """
    from esum import cli
    from esum.core.runner import run_action

    class ActionRunner:
        backend = fake_http
        backend_factory = None

        def factory(self, site, options):
            if self.backend_factory is not None:
                return self.backend_factory(site, options)
            return self.backend

        def __call__(self, site, act, kwargs, options, stream=None, *, backend_factory=None, session=None,
                     typed=frozenset(), secret=None):
            return run_action(site, act, kwargs, options, stream=stream, session=session,
                              backend_factory=backend_factory or self.factory, typed=typed, secret=secret)

        def cli(self, argv, *, registry=None):
            # The CLI adapter still parses and renders errors; only action execution is injected.
            with monkeypatch.context() as patch:
                patch.setattr(cli, "run_action", self)
                return cli.main(list(argv), registry=registry)

    return ActionRunner()


@pytest.fixture
def offline_site(fake_http):
    """Built-in registry and the shared canned backend."""
    return Registry.discover(custom_root=None), fake_http


DEMO_SITE = '''
from typing import Literal

from esum import FORMAT, Post, Result, Tier, Transport, Write, action, render_thread, select_numbers, site, with_gaps

FORMAT = 1
PORTED_FROM = "tests only"

demo = site(name="demo", base_url="https://demo.invalid", transport=Transport.DIRECT,
            label={"en": "Demo forum for tests"}, extras={"op_user": "alice"})


@action(demo, tier=Tier.READ)
def thread(ctx, id: int, mode: Literal["medium", "full"] = "medium", from_: int | None = None,
           to: int | None = None, verbose: bool = False, tags: list[str] | None = None):
    """Read a demo thread in compact-post format.

    Posts come from the fake backend.

    Args:
        id: Topic id.
        mode: medium (head + tail) or full.
        from_: First post number.
        to: Last post number.
        verbose: Print more.
        tags: Filter tags.
    """
    data = ctx.http.get_json(f"/t/{id}.json")
    total = len(data["posts"])
    wanted = set(select_numbers(total, mode, from_, to, head=2, tail=1))
    posts = [Post(number=p["n"], user=p["u"], text=p["t"], likes=p.get("l", 0),
                  is_op=p["u"] == ctx.site.extras["op_user"])
             for p in data["posts"] if p["n"] in wanted]
    items = with_gaps(posts, total)
    return Result(data={"title": data["title"], "posts": items},
                  text=render_thread(data["title"], items, total=total))


@action(demo, tier=Tier.READ)
def big(ctx, lines: int = 2000):
    """Return a lot of text."""
    return "\\n".join(f"line {i} " + "x" * 40 for i in range(lines))


@action(demo, tier=Tier.READ)
def private(ctx):
    """Needs a login."""
    raise ctx.login_needed()


@action(demo, tier=Tier.READ)
def boom(ctx):
    """Fails raw."""
    raise RuntimeError("raw failure text")


@action(demo, tier=Tier.REVERSIBLE)
def bookmark(ctx, ids: list[int]):
    """Bookmark topics.

    Args:
        ids: Topic ids.
    """
    return [Write(f"Bookmark topic #{i} on demo", lambda i=i: ctx.http.request("PUT", f"/t/{i}/bookmark").status)
            for i in ids]
'''

DEMO_ENGINE = '''
from esum import Tier, action, engine

FORMAT = 1

demoeng = engine("demoeng", description="Demo engine for tests")


@action(demoeng, tier=Tier.READ)
def topic(ctx, id: int):
    """Show which site ran the engine action.

    Args:
        id: Topic id.
    """
    return f"{ctx.site.name} topic {id} at {ctx.http.url(f'/t/{id}')}"
'''

ONE_SITE = '''
from esum import Tier, action, site

FORMAT = 1

one = site(name="one", engine="demoeng", base_url="https://one.invalid", label="Site one on demoeng")


@action(one, tier=Tier.READ)
def extra(ctx):
    """A site-specific extra."""
    return "extra from one"
'''

TWO_SITE = '''
from esum import site

FORMAT = 1

two = site(name="two", engine="demoeng", base_url="https://two.invalid", label="Custom site two")
'''

NOTES_MD = '''---
name: notes
description: A Markdown-only demo site
format: 1
verified: 2026-10-26
writes: none
---
# notes

Open the page and read it.
'''


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


@pytest.fixture
def example_tree(tmp_path, isolated, monkeypatch):
    """Write the example engine/sites to disk; returns discover() kwargs for them."""
    pkgs = tmp_path / "pkgs"
    write(pkgs / "esumtest_engines" / "__init__.py", "")
    write(pkgs / "esumtest_engines" / "demoeng" / "__init__.py", DEMO_ENGINE)
    write(pkgs / "esumtest_sites" / "__init__.py", "")
    write(pkgs / "esumtest_sites" / "demoeng" / "one" / "__init__.py", ONE_SITE)  # none in demoeng on purpose
    monkeypatch.syspath_prepend(str(pkgs))
    sites = isolated / "sites"
    write(sites / "demo" / "site.py", DEMO_SITE)
    write(sites / "demoeng" / "two" / "site.py", TWO_SITE)
    write(sites / "notes" / "SITE.md", NOTES_MD)
    _forget_test_modules()  # a module another test left behind would look like this test's import
    yield {"engine_packages": ("esumtest_engines",), "site_packages": ("esumtest_sites",),
           "custom_root": sites}
    _forget_test_modules()


def _forget_test_modules() -> None:
    import sys
    for name in [m for m in sys.modules if m.startswith(("esumtest_", "esum_custom_sites"))]:
        del sys.modules[name]


@pytest.fixture
def example_registry(example_tree) -> Registry:
    return Registry.discover(**example_tree)


@pytest.fixture
def guarded(example_registry):
    """Add `guarded` to the example world and return it: a site behind a bot wall, read with Chrome's
    network fingerprint (T2), where a challenged call switches to the page (T3, D-40)."""
    from esum import Tier, Transport, action, site

    target = site(name="guarded", base_url="https://guarded.invalid", label="Guarded demo forum for tests",
                  transport=Transport.BROWSER_HTTP, fallback=Transport.IN_PAGE)

    @action(target, tier=Tier.READ)
    def thread(ctx, id: int):
        """Read a thread's title.

        Args:
            id: Topic id.
        """
        return ctx.http.get_json(f"/t/{id}.json")["title"]

    example_registry.sites[target.name] = target
    return target


@pytest.fixture
def run(capsys, example_registry, action_runner):
    """run("demo", "thread", "--id", "1") -> (exit code, stdout, stderr)."""
    def _run(*argv: str, registry: Registry | None = None):
        code = action_runner.cli(argv, registry=registry or example_registry)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run
