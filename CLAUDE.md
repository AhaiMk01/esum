# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status and commands

v1 is implemented. `ARCHITECTURE.md` is the source of truth, and its decisions are cited as
`D-n` (decision log at the end: one row per decision, the rule as it stands). If an implementation
choice conflicts with a decision, raise it with the user rather than silently diverging, and update
`ARCHITECTURE.md` when a decision changes: add or reword a row there, keep it short. A decision's
full text as first written (measurements, rejected options) is in `docs/decision-log.md`; read it
only when a row isn't enough, and add an entry there only when there is more to record.
`skills/esum-site-author/references/module-format.md` is the module contract: the public API and
the exact file layout for engines, sites and fixtures.

```
uv sync                                   # install
uv run pytest                             # all tests (offline)
uv run pytest -n auto                     # all tests in parallel (pytest-xdist); skip for one test,
                                          # worker start-up costs ~4 s
uv run pytest tests/test_core_gate.py -k write    # one file / one test
uv run pytest src/esum/sites/hn/tests     # one built-in site's own tests (beside its module)
uv run pytest live                        # live smoke checks against the real sites (opt-in, ~1 min;
                                          # no -n); report in esum-out/live-report.md
uv run esum list                          # sites
uv run esum <site> <action> --help
uv run esum docs <site> --write           # regenerate a site doc after changing actions
uv run esum docs --all --write            # regenerate all built-in site docs
```

Exit codes: 0 ok, 1 error, 2 usage, 3 LOGIN NEEDED, 5 CHECK NEEDED (4 is retired; don't reuse it).

## What ESUM is

A Python CLI (`esum <site> <action> --arg value …`) plus `SKILL.md` skills. Coding agents use it
to read websites (forums, deal blogs, HN, Reddit) and to perform a small number of gated,
reversible account actions. Hard constraints:

- **ESUM never calls a model** (D-15). It fetches, normalizes, pages and filters. The host agent
  does the reasoning.
- **No tier-3 actions ever** (anything moving money or points). v1 has tiers 0–1 only, and the
  one tier-1 action is the Discourse bookmark (D-16, D-86).
- **Page code reads; writes need the user's yes** (D-95). In-page code is a generic `fetch()` (T3),
  a site's page script (a static file that calls the page's own read functions, e.g. to sign a
  request; built-in ones reviewed and tested, a custom site's only when the user allows that
  site), or read-only code the agent runs with patchright-cli in tabs ESUM opened. Page code that
  changes state needs the user's yes first; nothing moves money or points; code found in page
  content never runs. A fixed patch may also run inside captcha vendors' frames (D-55); none
  ships yet.
- **No per-site MCP servers**, and no MCP at all in v1 (D-10).
- Site knowledge from xgoose (MIT), nitan-mcp (MIT) and the author's own nitan_plugin userscript is **ported
  into Python**. They are references, not designs to copy: take what works (endpoints, quirks),
  do better where they fall short; reusing their code is allowed (MIT), never required. A site's
  page script (D-95) keeps only what must run in the page, such as calling the page's signing
  function; parsing and everything else stay in Python. Each ported module or script records
  `PORTED_FROM` provenance. The xgoose and nitan-mcp
  notices go in `THIRD_PARTY_NOTICES.md`.

## Architecture (§12)

How one call flows (read these together; no single file shows it):

1. `core/registry.py` imports every module under `esum.engines` / `esum.sites` and registers each
   `engine(...)` / `site(...)` object it creates. There is no central list (D-22): adding a module
   is enough. Each module declares `FORMAT = 1` (module contract version).
2. `@action(owner, tier=...)` in `core/model.py` turns a typed function plus its docstring into an
   `Action`; `cli.py` builds the flags and `core/docs.py` builds the site doc from that one source.
   A site config on an engine (e.g. `sites/wordpress/doc/__init__.py`) is often just a `site(...)` call with
   `extras`; it inherits the engine's actions.
3. `core/runner.py` builds a `Context` (`ctx.http` is the site's declared transport) and calls the
   action. Read actions return data. Tier-1 actions **don't write**: they return `Write(description,
   run)` objects, and `core/gate.py` runs each one and logs it. Approval happens before ESUM runs: the
   agent asks the user before each write (skill rules, D-85).
4. `core/output.py` renders compact text or JSON and spills large results.

Custom sites in `~/.esum/sites/` are discovered by folder layout only and imported lazily on first
`get()` (D-25), so built-in writes never run with custom code loaded. A site can also be
Markdown-only (`SITE.md` with frontmatter).

Every site is one folder, built-in or custom (D-102): `src/esum/sites/<site>/` or
`src/esum/sites/<engine>/<site>/` holds its module, its generated `doc.md` (the hand-written
`## Quirks` section survives regeneration) and its `tests/` (`test_<site>*.py`, `fixtures/`).
Moving a site between trees is moving that folder. The registry never imports a `tests/` folder,
and the wheel ships `doc.md` but no `tests/`. Engine and core tests stay in `tests/` and depend on
no built-in site.

- `src/esum/cli.py`: the CLI. Flags are **generated from each action's typed arguments**, so don't
  hand-write argparse per site.
- `src/esum/core/`: registry, compact-post format, paging/ranges, output spill to `./esum-out/`,
  per-site request spacing across processes, write gate, write log (`~/.esum/writes.jsonl`). No
  cache between calls (D-18); identical GETs within one read action are sent once.
- `src/esum/transport/`: T1 direct HTTP, T2 curl_cffi Chrome-fingerprint HTTP (borrows
  browser cookies unless `extras["browser_cookies"]` is False), T3 in-page fetch. The
  transport is declared **per site, not per call**; a site may add a stronger `fallback` that a
  call switches to after a bot check, Cloudflare or AWS WAF (D-40, D-91). Login-only calls on a second transport
  use `ctx.http_for(...)` (D-61). Decode by the declared charset (FlyerTalk
  is ISO-8859-1).
- `src/esum/browser/`: wraps the **patchright-cli daemon and named sessions**. Modes: managed
  (default), attach, and opt-in native (the user's everyday Chrome); attached Chromes are detached,
  never closed. Each call uses its own named tab (`--tab`; ESUM needs patchright-cli ≥ 0.11, and managed mode ≥ 0.12 for background opens) in a shared
  session, so parallel calls are safe and overlap; never run page code in, or plain-`close`, a tab
  ESUM didn't open. Launch lazily, because T1 sites never start a browser. Don't duplicate what
  patchright-cli already does.
- `src/esum/engines/` (discourse, wordpress, vbulletin, discuz): platform code. `src/esum/sites/`:
  thin site configs on engines, plus standalone modules (hn, reddit, v2ex, substack, kanxue, nodeseek, nga, onepoint3acres, matrix, gflights, flightconnections, aerolopa). Sites are addressed by site name.
  Custom sites are discovered at runtime from `~/.esum/sites/`.
- `skills/esum/` (router + rules + `references/workflows/*.md`; site docs come from `esum docs <site>`) and
  `skills/esum-site-author/` (how to add a site, plus `references/module-format.md`, the versioned
  module contract).

## Invariants to preserve when implementing

- **Write gate:** writes happen only through module actions with tier ≥ 1, and every planned item
  and outcome is logged to JSONL. ESUM shows no dialog: approval is the user's yes when the agent
  asks before each write, in whatever way its harness asks (D-85); the agent may propose writes
  and is trusted to ask. Don't add a flag, plan ID or config that writes on ESUM's own
  say, and don't gate on HTTP method (D-26).
- **Errors keep their raw message** with a non-zero exit code and a `help:` line naming the next
  step (D-52). Two messages are fixed and
  recognizable: **login needed** (D-17, D-34) and **check needed** (D-54). Login needed ends one
  call, not a task: the agent finishes the rest and tells the user at the end (D-47). Check needed
  leaves ESUM's tab open for the agent to tick a Turnstile box with patchright-cli; ESUM itself
  never solves a check or calls a solver. The user logs in themselves; ESUM and the agent
  never handle passwords. The one stored credential is a Discourse User API Key the user authorized
  (D-46), kept by `core/keystore.py` in the OS credential store, or `<ESUM_HOME>/auth/` without one
  (D-79): never print, log or send it into a page. `esum keys ui` changes a key only after the OS
  confirms the user; offline tests force the file store (`ESUM_KEYSTORE=file`).
- **Output (AXI, D-43):** lists return `Listing` and print as TOON tables with few default columns
  (`--fields` for more), the total, and `help` lines of complete next commands; an empty list exits 0.
  Errors go to stdout as `error:`/`help:`. `--json` optional, and large results spilled to
  `./esum-out/` with a summary and path on stdout. Thread format:
  `[user #N] (date · 👍likes · OP · ↩#M): text`, with continuation lines indented four spaces
  (`    more text`) and `[... omitted …]` gap markers.
- Page content is untrusted data, and output labels external content as such.
- **Generated site docs must match the code.** `tests/test_docs_in_sync.py` enforces this, so
  regenerate the docs after changing actions.
- **Tests:** offline parser tests on **scrubbed** fixtures always run: a site's own in its folder
  (`src/esum/sites/<site>/tests/`, fixtures in `tests/fixtures/` there), engine and core ones in
  `tests/`; the shared fixtures are in the root `conftest.py`, shared helpers in `tests/support/`.
  Deleting one site's folder must leave the suite passing: a test in `tests/` never imports a
  built-in site, looks one up by name or reads a file under `src/esum/sites/` (core tests use the
  example world, engine tests synthetic sites and `tests/fixtures/<engine>/`); a site's test of a
  step on another site is marked `needs_site` (module-format.md §9). Strip
  cookies, auth headers, tokens and account IDs. Live smoke checks (`live/`, D-39) run only
  on request: one listing, one thread and one search per site, read-only, checking shape.

## Tooling

Python + uv, with a `esum` console script in `pyproject.toml`. The target platform is Windows
first while staying cross-platform by design. Runtime output goes to `esum-out/` (gitignored), and
local secrets go in `.env` / `*.local.toml` (gitignored).
