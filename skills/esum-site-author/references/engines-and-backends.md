# Engines and transport backends

For work inside the esum repo: writing a new engine, or a T2/T3 transport backend. A site
config, a standalone module and a Markdown site need none of this; their contract is
[module-format.md](module-format.md). Custom engines aren't supported (D-84).

## Engine

Files a **new engine** worker creates (replace `forum` with the engine name):

```
src/esum/engines/forum/__init__.py        # engine("forum", …) + its @action functions
src/esum/engines/forum/parse.py           # optional: pure parsing functions (what fixtures test)
src/esum/sites/forum/<site>/__init__.py   # one thin config per v1 site on the engine
tests/fixtures/forum/<case>.json|html     # the engine's own scrubbed responses, whichever site they came from
tests/test_parsers_forum.py               # offline tests of the engine, on a synthetic site (module-format.md §9)
src/esum/sites/forum/<site>/doc.md        # generated: uv run esum docs <site> --write
src/esum/sites/forum/<site>/tests/        # that site's own tests and fixtures (module-format §1)
```


```python
# src/esum/engines/forum/__init__.py
from esum import Mode, Next, Post, Tier, action, engine, read_spans, select_numbers, thread_view

FORMAT = 1
PORTED_FROM = "xgoose skills/forum.source.xgs.js (github.com/xGoose-dev/xgoose)"

EXTRAS = ("categories",)   # the site extras this engine's actions read (see "Site extras" below)
forum = engine("forum", description="Forum platform X: topics, posts, search")


@action(forum, tier=Tier.READ)
def thread(ctx, id: int, mode: Mode = "medium", from_: int | None = None, to: int | None = None):
    """A topic and its posts in compact-post format.

    `medium` reads the first and last 30 posts; `full` reads everything.

    Args:
        id: Topic id (the number in the topic URL).
        mode: medium (head + tail) or full.
        from_: First post number to read (overrides mode).
        to: Last post number to read (overrides mode).
    """
    data = ctx.http.get_json(f"/t/{id}.json")
    ...
    view = thread_view(posts, read_spans(wanted, highest), count=total)
    return view.result(title, url=url, data={"id": id}, next=view.range_steps({"id": id}))
```

Engine actions run for every site on the engine and read per-site settings from `ctx.site`
(module-format §2). Declare the extras keys they read in the module's `EXTRAS` tuple.

## Transport backends


T1 exports `make_backend(site, options) -> Backend`. T2 and T3 export
`make_backend(site, options, *, session: BrowserSession) -> Backend`. The client passes their
browser session explicitly. The module name is fixed per transport, so nothing needs to be registered:

| `Transport` | Module | Status |
|---|---|---|
| `DIRECT` (T1) | `esum.transport.direct` | httpx, implemented |
| `BROWSER_HTTP` (T2) | `esum.transport.browser_http` | implemented: curl_cffi with Chrome's fingerprint, plus the browser session's cookie jar and user agent unless `extras["browser_cookies"]` is False |
| `IN_PAGE` (T3) | `esum.transport.in_page` | implemented: a generic `fetch()` in a named tab of the origin, raw body bytes back |

```python
from esum.transport import Request, Response, is_challenged

is_challenged(response: Response, site: Site) -> bool  # shared detector for clearing probes

class Backend(Protocol):
    def send(self, request: Request, *, before_send: Callable[[], None]) -> Response: ...
        # one request: no retry, no throttle of its own; call before_send() after slow set-up,
        # immediately before the request goes out (it applies the site throttle)
    transient_errors: tuple[type[BaseException], ...]   # optional: network errors HttpClient retries reads after
    def close(self) -> None: ...
```

- `Request` has the fields `method`, `url` (absolute, query included), `headers`, `content`
  (bytes or None), `timeout` and the selected `credential`. The client has already merged that
  credential's headers into `headers`. Return `Response(status, url, headers, content)`, with header
  names lower-cased and the raw body bytes. Don't decode the body; `HttpClient` does that.
- A backend module is imported only on a site's first request, so a T1 run never imports browser
  code. `make_backend` should be cheap, and the browser should start (or attach) on the first
  `send`.
- `options` carries the global flags (Options fields: `json`, `browser`, `cdp`, `fields`,
  `no_spill`, `max_retries`, `min_interval`, `extra`):
  `options.json` (True for `--json`), `options.browser` (`"managed"` | `"attach"` | `"native"`),
  `options.cdp` (URL or None; giving `--cdp` implies `attach`), `options.fields` (the `--fields`
  value, used by the output layer), `options.max_retries` / `options.min_interval` (read by
  `HttpClient`, so a backend ignores them), and `options.extra` (a dict for
  backend settings).
- If the backend sees a logged-out session, it raises
  `LoginNeeded(site.name, site.base_url, where=session.where)`, including for cookieless T2. A T3 backend runs a
  generic `fetch()` in a page, or the site's page script (D-95): a static file in the site
  package that calls the page's own read functions or data (a request signature, a token, state
  the page holds) and returns data. A page script never writes; a write stays a tier ≥ 1
  `Write`. A built-in script is reviewed and has tests; a custom site's script runs only when the
  user allows that site in config. The file layout and the call that runs it are not built
  yet; until they are, a site needing one stays a Markdown site. (D-55 also allows a fixed,
  reviewed patch shipped in ESUM itself that runs only inside a captcha vendor's frames. A module
  or backend never ships one, and none exists yet.)
- `HttpClient` closes the backend after each action. Shared browser state such as patchright-cli
  sessions belongs in `src/esum/browser/`, not in the backend object.

`HttpClient(site, options, *, dedupe, backend_factory=None, session=None, throttle=None,
sleep=time.sleep)` accepts a browser session dependency. An optional `backend_factory(site, options)`
replaces backend construction for tests. `load_backend(site, options, *, session=None)` passes an
injected session to browser backend factories. Without an injected session, the client selects the process's
shared session lazily. `Context.http_for` passes that same session to additional clients. The runner's
`run_action(site, act, kwargs, options, stream=None, *, session=None, backend_factory=None)` also accepts this dependency.
The runner passes `backend_factory(site, options)` to the primary client; `Context.http_for`
propagates it to additional clients, and escalation uses it with the stronger transport.
The optional factory has the same two-argument signature on T1, T2 and T3.
T1 imports no browser code for ordinary requests. Cookieless T2 never opens a browser.

`BrowserSession(mode="managed", cdp=None, *, client=None, port=9321, sleep=time.sleep,
clock=time.monotonic)` accepts both the sleep operation and deadline clock. Its daemon adapter exposes
`evaluate(fn, arg=None, *, origin, session, tab)`, returning the structured origin-guard reply.
The real adapter encodes JavaScript and calls the daemon. Fakes use the same interface without
decoding expressions. `Tab.evaluate` still checks the guard's reply and raises `TabLost`.
