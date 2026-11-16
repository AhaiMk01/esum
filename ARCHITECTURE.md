# ESUM — Architecture

> **ESUM** (Every Site, Unwrapped for Models) is a lightweight, agent-agnostic toolkit that gives coding agents
> (Claude Code, Codex, omp, or anything that reads `SKILL.md`) reliable tools for reading — and,
> carefully, acting on — websites, plus the workflow recipes that tie them together.

Status: **v1 implemented.** This document records the design decided in the 2026-10-26 design
session and remains the source of truth for the implementation. Decisions are referenced as `D-n` in the [decision log](#decision-log).

---

## 1. Goals and non-goals

**Goals**

- Give any coding agent a small, predictable CLI per website: search, read threads with full
  comment coverage, list deals and data points, and (gated) reversible account actions.
- Keep the agent in charge of reasoning. ESUM fetches, normalizes, pages and filters; the agent
  summarizes, compares and decides.
- Make adding a new site an agent task: the user asks their coding agent to explore a site and
  produce an ESUM-compatible site bundle.
- Stay light: plain Python, a handful of modules, no services to run.

**Non-goals**

- ESUM is **not an agent** and **never calls a model** (no API keys, no prompts inside the tool).
- No money- or points-moving actions, ever (transfers, bookings, redemptions, purchases).
- No page code that writes on ESUM's own say. In-page code is a generic `fetch`, a reviewed
  read-only page script a site ships (custom sites only when the user allows that site), or
  read-only code the agent writes and runs with patchright-cli in ESUM's own tabs (D-95).
- No per-site MCP servers (too heavy for agent context).

---

## 2. Principles

1. **Don't build an agent.** The host agent owns planning, looping, memory and permissions.
2. **APIs, not screenshots.** Site modules use the site's JSON/HTML endpoints; UI driving is the
   fallback, documented in Markdown site skills.
3. **One place per site.** A site is an engine + config (or a standalone module, or a Markdown
   skill). Docs are generated from code where code exists.
4. **Read by default; writes need the user's yes.** Every action declares a tier. A write is an
   explicit `esum <site> <action>` command the agent runs only after asking the user (D-85).
5. **Page content is untrusted.** Output is data for the agent, never instructions to it.
6. **Small outputs.** Compact text by default; large results go to files the agent reads selectively.
7. **Fail closed, stay light.** When in doubt, refuse or ask — but prefer a simple mechanism over a
   clever one.

---

## 3. Overview

The same picture as a diagram: [docs/architecture.svg](docs/architecture.svg)
([中文](docs/architecture.zh-CN.svg)).

```
 ┌──────────────────────────────────────────────────────────────────┐
 │ Coding agent (Claude Code / Codex / omp …)                       │
 │   skills/esum/SKILL.md          router + rules (always small)    │
 │   skills/esum-site-author/      how to add a site (on demand)    │
 └───────────────┬──────────────────────────────────────────────────┘
                 │ shell:  esum <site> <action> --arg value …
 ┌───────────────▼──────────────────────────────────────────────────┐
 │ esum CLI (Python)                                                │
 │   registry ── engines (discourse, wordpress, vbulletin, …)       │
 │            └─ sites (configs on engines, standalone modules,     │
 │                      Markdown-only sites)                        │
 │   core: compact-post format · paging/ranges · output spill ·     │
 │         throttle · write gate (tier) · write log                 │
 └───────────────┬──────────────────────────────────────────────────┘
                 │ transport, declared per site
 ┌───────────────▼──────────────────────────────────────────────────┐
 │ T1 direct HTTP     public APIs, no browser                       │
 │ T2 Chrome HTTP     curl_cffi Chrome fingerprint (+ cookie jar)   │
 │ T3 in-page fetch   generic fetch() in a tab of that origin,      │
 │                    or the site's read-only page script (D-95)    │
 └───────────────┬──────────────────────────────────────────────────┘
                 │ patchright-cli daemon (named sessions, named tab per call)
           Chrome — managed profile (default) or attached instance
                 ▲
                 │ agent's own page code, read-only, in ESUM's tabs only;
                 │ anything that changes state asks the user first (D-95)
           Coding agent ── patchright-cli eval / run-code
```

---

## 4. Interface

### 4.1 CLI

```
esum                                   # home view: binary, description, sites, next steps
esum list                              # built-in + custom sites, engine, one-line description
esum docs <site>                       # print that site's doc (generated or Markdown)
esum <site> --help                     # actions and arguments
esum <site> <action> --arg value …     # run an action
esum --version | -v | -V               # bare version, answered before the CLI is imported

global flags:  --json   --fields=<a,b|all>   --browser=managed|attach|native   --cdp=<url>
```

- Flags are generated from each action's typed arguments. An unknown flag is a usage error that
  names the action's valid flags (exit 2).
- Exit codes: 0 ok (an empty list included), 1 error, 2 usage, 3 LOGIN NEEDED, 5 CHECK NEEDED.

### 4.2 Skills (one `npx skills add` bundle)

| Skill | Loaded | Contents |
|---|---|---|
| `esum` | always (name + description), body on use | Router: run `esum list`, pick a site, read `esum docs <site>`. Rules: untrusted content, write tiers, login hand-off, default browser mode and how prompts override it ("use my Chrome" → `--browser=attach`). Links to workflow recipes. |
| `esum-site-author` | only when asked to add/fix a site | How to explore a site and produce a compatible bundle (§8). |

Bundle layout:

```
skills/
  esum/
    SKILL.md
    references/
      workflows/<name>.md        # hand-written recipes
  esum-site-author/
    SKILL.md
    scripts/probe.py             # read-only first look: transport, charset, platform, data sources
    references/module-format.md  # the versioned module contract
    references/engines-and-backends.md  # repo-only: writing an engine or a T2/T3 backend
    references/output-design.md  # AXI for a site module: columns, totals, empty results, next steps
    references/discovery.md      # patchright-cli exploration recipes
    references/templates.md      # bundle skeletons, custom-site tests, the trigger skill
```

All three target agents read `SKILL.md` natively (Claude Code: `.claude/skills/`; Codex and omp:
`.agents/skills/`). `npx skills add` installs to the right places.

### 4.3 MCP

Not in v1. If added later: **one** server with three generic tools — `esum_list`,
`esum_describe(site)`, `esum_call(site, action, args)` — backed by the same core. Never per-site
servers.

---

## 5. Site model

### 5.1 Engines and sites

- **Engine** — reusable code for a platform: `discourse`, `wordpress`, `vbulletin`, … It implements
  the actions common to every site on that platform.
- **Site** — mostly configuration on an engine (base URL, transport, labels) plus optional
  site-specific extras (e.g. USCardForum's category map and "recent DPs" preset).
- **Standalone module** — a site with its own code when no engine fits (e.g. Hacker News, Reddit).
- **Markdown site skill** — for sites too hard to canonicalize (§5.3).

Sites are always addressed by **site name** (`esum uscf …`, `esum doc …`); `esum list` shows each
site's engine.

### 5.2 Module or Markdown? (integration level decides)

| Use a **module** when… | Use a **Markdown site skill** when… |
|---|---|
| Data comes from endpoints reachable through T1/T2/T3 | The task needs clicking through the UI |
| No per-request signing or rotating tokens | Requests are signed / tokens rotate (e.g. LinkedIn, X) |
| Responses are stable enough to parse and test | Anti-bot is aggressive or endpoints are unstable |

A Markdown skill may be promoted to a module when a clean API is found.

### 5.3 Markdown site skills

- A guide for the agent: which pages, which patchright-cli steps, what to extract, known pitfalls.
- **Read-only by default.** A skill may explicitly allow writes; it must then tell the agent to ask
  the user before each one. The agent drives the UI directly, so this is **advisory** and is
  documented as such.
- Verified once end to end; the verification date is recorded so stale skills can be spotted.

### 5.4 Module format (versioned)

Each module declares the module-format version it targets; ESUM warns or refuses on mismatch. The
contract is documented in `esum-site-author/references/module-format.md`. Illustrative shape:

```python
# ~/.esum/sites/wordpress/doc/site.py   (illustrative, not final API)
from esum import site, action, Tier, Transport

FORMAT = 1
PORTED_FROM = "xgoose apps/chrome/skills/..."   # provenance, when ported

doc = site(
    name="doc",
    engine="wordpress",
    base_url="https://www.doctorofcredit.com",
    transport=Transport.DIRECT,            # T1: public WP REST
    label={"en": "Doctor of Credit — US cards, bank bonuses; comments hold readers' DPs"},
    extras={"op_user": "DoC"},
)

@action(doc, tier=Tier.READ)
def thread(id: int, mode: str = "medium", from_: int | None = None, to: int | None = None):
    """A post and its full comment section in compact-post format."""
    ...
```

### 5.5 Locations and loading

- Built-in engines and sites ship in the package.
- Custom sites: `~/.esum/sites/<engine>/<site>/`, or `~/.esum/sites/<site>/` for standalone and
  Markdown-only sites. Discovered at runtime; no reinstall of skills needed.
- No trust gate in v1: custom modules need no approval (they are the user's own code).
- Custom Python is loaded lazily. Discovery reads only the folder layout (a custom module's folder
  name is its site name) and parses `SITE.md` frontmatter. A custom `site.py` is imported only when
  its own site is invoked (`esum <site> …`, `esum docs <site>`). `esum list`, `esum docs --all` and
  every built-in site never import custom code, so custom code can't touch their writes or the
  write log (D-25).
- A custom site never replaces a built-in one. Built-in sites are a curated set the maintainer
  keeps working, and which sites are in it is the maintainer's own choice: there are no admission
  criteria to meet or argue from. Users add their own sites beside them. A custom folder with a built-in site's
  name is listed under `not_loaded` in `esum list`, its code is never imported, and the built-in
  keeps running (D-96).

### 5.6 Porting sources

Site knowledge is **ported into fresh Python**, never copied as JavaScript:

- [xgoose](https://github.com/xGoose-dev/xgoose) site plugins (MIT, © 2026 xGoose contributors) —
  endpoints and quirks for forums, offers, awards, social sites. Each ported module records the
  source plugin (`PORTED_FROM`), without a version or commit.
- The `nitan_plugin` userscript's adapters — full-comment coverage (WordPress REST + wpDiscuz),
  FlyerTalk paging and charset, Discourse megathreads and post-number ranges, Substack comment
  trees, Reddit limits, republished-post dating, link selection.
- [nitan-mcp](https://github.com/nitansde/nitan-mcp) (MIT, © 2025 Discourse; derived from Discourse
  MCP) — a working uscardforum.com Discourse client: the `/raw/{id}?page=N` topic reader with
  post-number page estimation, search modifiers (`@user`, `after:`, `before:`, `category:`,
  `order:`), the USCardForum category map, hot/top/badge topic lists, Cloudflare-challenge
  detection, and the User API Key login (D-46). Its other credential handling (admin API keys,
  passwords, auto-login) and Cloudflare-bypass stack (cloudscraper, nodriver) are **not** ported:
  ESUM never touches a password (D-14) and uses in-page fetch (T3) instead of bypassing.

The xgoose and nitan-mcp MIT notices are kept in `THIRD_PARTY_NOTICES.md`.

---

## 6. Transports

Declared **per site** (not per call):

| Tier | Mechanism | Used for |
|---|---|---|
| **T1 direct HTTP** | Plain HTTP client, no browser launched | Public APIs: HN Algolia, V2EX, WordPress REST, Substack, public Discourse reads |
| **T2 Chrome-fingerprint HTTP** | curl_cffi with Chrome's TLS/HTTP2 fingerprint; by default also the browser session's cookie jar and user agent (`extras["browser_cookies"]=False`: no browser at all) | Sites that block non-browser clients by fingerprint, with or without a login (Reddit). Not JS challenges: those need T3 |
| **T3 in-page fetch** | A generic `fetch()` evaluated in a tab of the site's origin | JavaScript challenges (FlyerTalk's Cloudflare "Just a moment…"), logged-in writes on such sites (the USCardForum bookmark) |

A site declares one transport for reads. A login-only write may use a separate write transport
(e.g. a public Discourse forum read on T1, its bookmark on T2), declared by the engine and overridable in the
site config (`extras["write_transport"]`), so reads never launch a browser just because a write
needs cookies (D-28). Context caches these clients by requested transport and, for T2 only, cookie policy through
`ctx.http_for(transport, *, browser_cookies=None)`. The runner closes them after planning and all
writes, including on failure. Modules choose a transport but never construct or close clients.

Cloudflare doesn't treat a fingerprint the same way every time: T2 often gets through and sometimes
meets a challenge. A site may declare a stronger `fallback` transport (`site(...,
transport=BROWSER_HTTP, fallback=IN_PAGE)`; shown as `T2→T3`). When a response is a Cloudflare
challenge (`cf-mitigated: challenge`, or a 403/429/503 challenge page), `HttpClient` closes its
backend, resends that request on the fallback (writes too: a challenge means the request never
reached the site) and stays there for the rest of the call, with a line on stderr. Nothing is
remembered between calls: each call starts on the declared transport again. A challenge on a site
without a fallback raises `ChallengeError` at once, including with `check=False`. Its message
keeps the HTTP status and URL with a fixed bot-check description, omitting challenge HTML;
its `help` names the next transport or browser step.
Waiting does not clear it (D-40).

Only T3 runs code in a page: a generic fetch, or a site's page script (D-95). A page script is a
static file the site ships that calls the page's own read functions or data (a request
signature, a token, state the page holds) and returns data; it may click to reveal what it reads
("View more flights"), and it never writes, submits or signs in. Built-in scripts are
reviewed and tested; a custom site's runs only after the user allows that site in config.
Responses are decoded by their declared charset (FlyerTalk serves ISO-8859-1). A fixed patch
shipped in ESUM may also run inside a captcha vendor's own frames (D-55); none ships yet.
Separately, the agent may write read-only page code and run it with patchright-cli in ESUM's
tabs; code that changes state needs the user's yes first, and nothing moves money or points.

A T3 tab waits up to 20 s for an anti-bot interstitial to clear. A check that is still there (a
Cloudflare Turnstile "Verify you are human" box) ends the call with the fixed **CHECK NEEDED**
message, exit 5, and ESUM leaves that tab open and names its patchright-cli session and tab. The
host agent completes the check in that tab with patchright-cli (a `screenshot`, then
`mousemove <x> <y>`, `mousedown`, `mouseup` on the box: the check sits in a closed frame that
`snapshot` can't see, checked 2026-11-02 on a Turnstile demo page). Mouse coordinates are CSS
pixels, so the message gives the page's CSS size: a screenshot can be in device pixels (twice as
wide on a HiDPI screen with patchright 1.58, measured 2026-11-02), and the agent scales by the
ratio. That ratio only holds for an unzoomed viewport screenshot: a pinch-zoomed page keeps its
CSS width while its content is magnified (a 1.5 zoom made a width-ratio click miss, found in
review), so ESUM also reads the visual zoom, and on a zoomed tab the agent doesn't click and
treats the check like login needed. A tab lost while ESUM reads its size is not handed over. It then reruns the call, which passes on the session's new clearance cookie, and closes the
tab. A write the check stopped midway lists the items it already wrote, and the agent reruns only
the rest. ESUM solves nothing, calls no solver service and runs no code for the check (D-54). In native
mode the tab is in the user's own Chrome, so the same check is LOGIN NEEDED instead.

Politeness in v1: retry with backoff on HTTP 429 (honoring `Retry-After` / Discourse
`wait_seconds`), writes included, since a 429 means the server did not process the request. Per-site concurrency limits are deferred (§13).

---

## 7. Browser

### 7.1 Modes

| Mode | What | v1 |
|---|---|---|
| `managed` | A patchright-cli persistent profile in its own window; the user logs in once | **Default** |
| `attach` | A Chrome the user started on a dedicated profile with a debugging port (`--cdp=<url>`), with its own cookies (host context) | Yes |
| `native` | The user's everyday Chrome via Chrome's consented remote debugging (`chrome://inspect/#remote-debugging`, Chrome 144+; address from `DevToolsActivePort`), host context, detached never closed. Opt-in: the session controls the user's whole browser | Yes (D-32) |
| `chrome-profile` | A sandboxed copy of the user's saved Chrome profile | Dropped: doesn't work on Windows (§13) |

- The default mode is stated in the skill; the user's prompt can override it, and the skill maps
  phrases to flags.
- The user's **default** Chrome profile can never be launched or debugged directly: Chrome 136+
  ignores remote-debugging switches on the default user-data dir.

### 7.2 Sessions

- ESUM drives Chrome through **patchright-cli's daemon and named sessions**. When a site action falls
  short, the agent can use plain patchright-cli commands (snapshot, click) on the same session.
- **Named tab per call** (`patchright-cli --tab=<name>`, from 0.8), shared session:
  every command names its tab, so parallel calls (e.g. an agent's sub-agents researching different
  sites) never touch each other's pages and their requests overlap. The only cross-process lock
  guards opening or attaching the session. ESUM never runs page code in a tab it didn't open (in
  attach and native mode the default tab is the user's), and never sends a plain `close` there.
- ESUM checks `patchright-cli daemon-info --json` (cached per install) and fails with an upgrade hint
  below 0.11: it speaks the daemon protocol directly, and older versions enable CDP `Runtime` on
  every page, which bot checks detect (D-56).
- **Failures are told apart by code** (D-58). The daemon client turns every failed reply into a
  `BrowserError` whose `code` is the daemon's own (0.11.1+) or the one its message implies; only
  that client reads message text. The session decides what to do (open it, reopen a closed browser,
  report a lost tab) from the code alone.
- The browser is started lazily: T1 sites never launch it.
- Every managed open uses the protected `background-open` daemon operation. Chrome stays
  headed and opens windows and pages without activating the desktop window. Existing owned
  headed sessions adopt this policy without losing pages. Stock daemons reject the operation
  before launching Chrome, including after a daemon restart. patchright-cli 0.12.0 ships the
  operation (D-70). Attach and native keep their existing behavior.

### 7.3 Login hand-off

When an action detects it is logged out, ESUM stops with a fixed, recognizable **login-needed**
message naming the site and where to log in. It ends that call (exit 3), not the task: the skill
tells the agent to carry on with the rest and report what needed a login at the end (D-47). The login
is **the user's to start**, and the agent retries those commands once they say they're done. The
agent never types passwords or handles credentials.

The active `HttpClient` supplies the login destination through its browser session, including after
transport escalation. `Context.login_needed` delegates to that client. A separate client from
`Context.http_for` supplies its own login error. `browser/session.py:login_place` formats every browser
location used by clients, sessions, and browser challenge help. A bare `LoginNeeded` uses the
mode-neutral `the browser ESUM uses`; modules raise through Context or the active client.
Discourse's User API Key flow is the sole site-specific location override. `LoginNeeded` owns its
message, help, and exit code, as `CheckNeeded` does. The CLI only renders them.

A Discourse site can also keep a **User API Key** (D-46). `esum <site> login` is interactive: the
user runs it in their own terminal, and it refuses without one, so an agent's tool shell can't start
it. It asks for the scopes and opens the authorization page; they log in in their own browser,
approve, and paste back the encrypted payload. ESUM decrypts it with a private key that lives only in
that process's memory, checks the nonce and keeps the key in the OS credential store, or in a user-only
`<ESUM_HOME>/auth/<site>.json` where there is none (D-79). The key is never printed or logged, and is sent only as request headers
to that site, never over T3 (transport drops the request credential when a call escalates), because T3 runs
inside a page. A key with the `write` scope lets write actions run without the browser; they still
run only after the user's yes. ESUM never revokes a key: the user revokes it on the site
(Discourse: `/my/preferences/apps`), and `esum keys ui` deletes ESUM's copy (D-86).

The key is added to a request in one place, `HttpClient`, and the rules that keep it on its site
live there too, for T1 and T2 alike. A request that carries a key has its redirects followed by the
client, hop by hop (backends only send): the key stays on the request's own host and port (or an
upgrade to https there) and is dropped, for the rest of the chain, from the first hop that leaves it;
the response then records that no key was used. A transport error that quotes a key anywhere, also in
a cause, is replaced by one carrying only the text with `[credential]` in its place.

`esum keys` lists which sites have a saved key, where it is stored and since when, never the key.
`esum keys ui` is the user's local page for the same list (D-79). Removing a key there deletes ESUM's copy
only (the user revokes it on the site); adding one runs the site's `login`. Both only after the OS confirms the user (Windows Hello, Touch ID or
the login password, or a fingerprint through fprintd); without such a check the page is read-only.

---

## 8. Adding a site (the `esum-site-author` skill)

The user gives a URL and what they want from the site; the skill walks the agent from there to a
working, documented site (D-44).

1. **Scope.** Record a free site name, URL, login answer, and the user's dogfood questions mapped
   to 2–5 actions, including a handoff such as list → thread.
2. **Probe.** Run `scripts/probe.py` with `uv run` for spaced, cookie-less T1/T2 checks of the
   platform, charset, challenges, feeds, embedded data, and API paths. `--page` checks an endpoint;
   `--save` captures raw responses for later scrubbing.
3. **Explore.** Follow discovery.md with patchright-cli. Use a fresh per-site guest session, or
   ESUM's managed session in your own named tab for logins, never native. Observe only. Confirm
   sources, ids, paging, guest behavior, and transports; login-only and T3 sources use in-page fetch.
   Record ground truth, one item per output kind as the rendered page shows it (D-82).
4. **Choose the kind.** Record engine config, standalone module, or Markdown site and the reason
   (§5.2). Markdown uses `SITE.md`, has no actions, parsers, fixtures, or CLI live run, and skips
   steps 6 and 10.
5. **Build.** Use templates.md and the module contract. Declare action tiers; never create tier-3
   actions. Custom module help must import `site.py` and list actions, since `esum list` never
   imports custom code (D-25). Built-ins must list without problems and show actions in help.
   Markdown writes `SITE.md` and its help must print the Markdown-site notice.
6. **Fixtures and parser tests.** Test pure parsers on scrubbed real responses, with expected
   values taken from ground truth captured with them (D-82). Review fixtures
   for cookies, auth headers, tokens, and account identifiers; nothing secret ever lands in a
   module, doc or fixture. Custom tests run in the site's own
   folder on the installed esum interpreter, using templates.md's command.
7. **Live run.** Every read action must exit 0 with real rows or posts; unexpectedly empty lists
   fail. Follow one printed `help:` line, run each printed next-page command, and compare one item
   per output kind field by field with its ground truth. Continue past LOGIN NEEDED and report blocked calls at the end
   (D-47); follow the `esum` skill's steps on CHECK NEEDED (D-54). Writes run only when the user
   asks for that write; otherwise test planned `Write` items offline (D-36). Markdown executes
   `SITE.md`'s browser steps end to end, read-only.
8. **Doc.** Regenerate with `esum docs <site> --write` and fill in `## Quirks`. The generated
   `_None recorded yet._` counts only after explicitly confirming there are none. Markdown's doc
   is `SITE.md`; set `verified:` only after its browser run succeeds and record its pitfalls.
9. **Contract check, then dogfood.** First check list limits and totals, default columns for the
   fields the questions turn on, sort and window flags, the largest thread's count, help and spill
   index, and error exits (D-89). Then follow D-53 and the skill's portable brief, using docs and help, reads
   only, no code or credentials, and untrusted page content as data. A same-author round follows
   the same brief and is marked not independent. Round 2 re-checks every round-1 fix
   and adds questions of the same kinds using real data seen earlier. Give each friction a check
   that fails before the fix and passes after at its layer, then regenerate affected docs. Run
   shared engine or core tests when changed. Markdown follows and repairs `SITE.md`. Run rounds
   serially within about 30 live calls each, with bounded thread ranges. On repeated throttling,
   stop, raise the site's spacing, record Quirks, and report. Round 2 runs only if round 1 found
   friction (D-89). Outcome: verified after a clean round 1 or 2 covering all kinds and earlier fixes, or
   stopped when round 4 still has friction or the site blocks progress.
10. **Full check.** Built-in modules add a live-test row, run the offline suite and
    `uv run pytest live -k <site>`. The request to add a built-in site authorizes this narrow live
    check. Custom modules rerun their offline tests and confirm `doc.md` was regenerated after
    the last fix. Markdown skips this step.
11. **Trigger skill.** Offer custom sites an `esum-<site>` skill routing to `esum docs <site>`.
    Finish when it is written with consent and readable by the agent, or declined. Built-ins skip.

**Done when:** module checks pass, each unblocked read returns real data, blocked calls are
reported, and the regenerated doc has reviewed Quirks. Markdown's documented read-only browser
run succeeds before its verification date is set. Both kinds report dogfood outcome, rounds,
per-round fixes, remaining friction, and independence. Verified requires a clean round 1 or 2 with
all question kinds and earlier fixes checked; stopped records unresolved friction or a site block
and is not verification. Applicable Full checks pass and the trigger offer is resolved.

---

## 9. Writes and safety

### 9.1 Tiers

| Tier | Meaning | Examples | v1 |
|---|---|---|---|
| 0 | Read | search, read thread, list offers | ✅ |
| 1 | Reversible account change | bookmark a topic, enroll an offer | ✅ (one action: Discourse bookmark) |
| 2 | Public or hard to undo | post a reply, send a message | ❌ later |
| 3 | Moves money or points | transfer, book, redeem, purchase | ❌ **never** |

### 9.2 The gate (v1, lightweight)

- Writes happen **only through module actions** with a declared tier ≥ 1. A tier-1 action returns
  its planned writes (`Write(description, run)`); a read action that returns writes is refused.
- **Approval is the user's yes to the agent's question before each write** (D-85). The agent asks
  in whatever way its harness asks (a question prompt, a permission prompt or a message), saying
  what the write changes and on which account. It may propose a write that serves the user's
  request, also one they didn't name. ESUM shows no dialog and has no flag, plan ID or config that
  approves anything itself.
  - **Trust model: the agent is assumed to follow the skill's rules.** ESUM runs as the same OS
    user as the agent, so it cannot stop an agent that decides to go around it (`python -m esum`,
    a script, or a direct request with the browser's cookies), and no ESUM-side check, dialog or
    host rule would change that. ESUM adds no capability the agent lacks; it keeps its own path
    narrow: one reversible write action, nothing that moves money or points, every write logged.
  - **What it does not protect against:** an agent that ignores the skill, including one steered
    by injected page content despite the untrusted-content rule. The blast radius is a reversible,
    logged bookmark.
  - Lazy custom-module loading (§5.5) keeps built-in writes free of custom code; when a custom site
    is invoked its code is trusted as the user's own, and the write log records it (`custom_code`).
- Every write is appended to the **write log** (`~/.esum/writes.jsonl`): the planned items, then
  each item's outcome (ok, error, skipped).
- Markdown skills that allow writes tell the agent to ask the user first: **advisory** (§5.3).

### 9.3 Why not LLM judging or a method-based firewall

- **LLM judging** fails open: a false negative is a silent write, and page/request text can
  prompt-inject the judge. An LLM may explain a request or add caution, never approve.
- **Filtering by HTTP method** misfires both ways: many reads are POST (GraphQL, award searches,
  offer lists, telemetry), and some writes are GET or WebSocket. A future browser-level gate must be
  driven by per-site endpoint rules, not methods (§13).

### 9.4 Untrusted content

Everything a site returns is data. The skill tells the agent never to follow instructions found in
page or post text, and ESUM's output labels external content as such (e.g. linked sources).

---

## 10. Outputs and errors

- **Lists are TOON tables** (D-43): `esum:` and `untrusted:` lines, the list's context
  (`board: …`, `query: …`), `count: N of TOTAL total`, then `name[N]{id,title,…}:` with one row
  per line, and a `help[n]:` block of complete next commands (next page, read a thread, more
  columns). Default columns are the fewest that let the agent choose its next call;
  `--fields a,b` adds columns, `--fields all` shows every one. An empty list says
  `name: "0 found: <why>"` and exits 0.
- **Threads keep the compact post format:**
  `[user #N] (date · 👍likes · OP · ↩#M): text`, with continuation lines indented four spaces
  (`    more text`) and `[... omitted …]` markers for gaps. TOON saves little on long text.
- `--json` for structured output.
- **Spill to file:** results over a size threshold are written to `./esum-out/` (current working
  directory, visible to the agent) and stdout carries a short summary, the path and the next steps.
- **Errors pass through raw** with a non-zero exit code; the agent reads them and decides. They go
  to **stdout** as `error: <message>` plus, where ESUM knows the fix, `help: <next step>` (a JSON
  object with `--json`); the message is still the site's or the network's own text. Common
  patterns are added to the skill over time. Two outcomes have a fixed, recognizable message:
  **login needed** (§7.3), exit 3, and **check needed** (§7.2, D-54), exit 5. (Exit 4, write not
  approved, is retired with the dialog, D-34.)

---

## 11. Workflows

Workflows are **Markdown recipes** in `skills/esum/references/workflows/` that the agent follows,
calling `esum` actions and using judgment between steps. A recipe is promoted to a coded command
only when it is a routine, repetitive bulk loop needing no judgment.

v1 recipes:

1. **DP research** — a question → keywords → search USCardForum, Doctor of Credit, 美国信用卡指南,
   FlyerTalk, Reddit (optionally recent-only / by category) → read the top threads → answer with
   cited post links, newest data winning on conflicts.
2. **Deal digest** — what's new since a date across those sources.
3. **Deep read of one thread** — medium (head + tail), full, a post range, or chunked
   read-and-merge for huge threads (read range by range, summarize, merge — done by the agent).
4. **Flight search** — fares from Google Flights, who flies the route (FlightConnections), the
   carriers both missed (Matrix `--thorough`), the fare's kind, and the seats (AeroLOPA).
5. **Fare rules** — a question about a fare (refund, change, stopover, ticketing) → find the fare
   on Matrix (by fare basis for a ticket the user holds, by `matrix ladder` for the cheapest
   fare with a condition) → read the filed rule categories it needs → answer with the quoted
   text → for what the rules leave to the airline, data points from the forums (recipe 1).

---

## 12. Repository and runtime layout

```
esum/
  pyproject.toml              # uv; console script `esum`
  ARCHITECTURE.md
  THIRD_PARTY_NOTICES.md      # xgoose and nitan-mcp MIT notices
  src/esum/
    cli.py                    # argument parsing generated from actions
    core/                     # registry, compact-post format, paging, output spill,
                              # throttle, write gate (tier), write log
    transport/                # direct.py, browser_http.py, in_page.py
    browser/                  # patchright-cli session management, attach mode
    engines/                  # discourse/, wordpress/, vbulletin/, discuz/
    sites/                    # one folder per built-in site (hn/, wordpress/doc/, …):
                              # module, doc.md, tests/ (+ tests/fixtures/, not in the wheel)
  skills/                     # the npx-skills bundle (§4.2)
  conftest.py                 # shared fixtures and the offline network guard
  tests/
    fixtures/<engine>/        # scrubbed saved responses for engine tests (a site's tests may read them)
    support/                  # helpers tests/ and the sites' tests share (engine fakes, needs_site)
    test_parsers_*.py         # engine tests, offline, always run, on synthetic sites
    test_docs_in_sync.py      # generated site docs match code

~/.esum/
  config.toml                 # optional local settings
  sites/                      # custom sites (§5.5)
  throttle/                   # per-site last-request stamps (request spacing)
  writes.jsonl                # write log
```

**Packaging:** Python + uv, installed from the git repository with
`uv tool install git+https://github.com/AhaiMk01/esum` (the skills with `npx skills add
AhaiMk01/esum`, §4.2). The repository stays private, with no PyPI release, so both need a `gh`
login with access to it and `gh auth setup-git` for uv's plain `git` fetch.
**Dependencies:** patchright-cli / patchright for the browser, httpx for T1, curl_cffi for T2.
**Platforms:** Windows first; macOS/Linux kept working by design (the
platform-specific part is browser/profile discovery), not tested in v1.

**Testing:** offline parser tests on scrubbed fixtures always run; a test fails if generated site
docs drift from code; live smoke checks (`uv run pytest live`, D-39) are run on request.

---

## 13. v1 scope and deferred work

**v1**

- CLI, the three transports, `managed` + `attach` browser modes, patchright-cli sessions.
- Engines: Discourse, WordPress, vBulletin, Discuz!. Sites: USCardForum and LINUX DO (and any
  Discourse forum), Doctor of Credit, 美国信用卡指南, FlyerTalk, Reddit, Hacker News, V2EX, Substack
  (D-35), 吾爱破解 (Discuz!), 看雪 Kanxue, NodeSeek (D-41), NGA (D-42), ITA Matrix (D-76).
- Compact output + spill to file, per-site request spacing, same-call request reuse, retry on 429
  and on short-lived read failures.
- Write gate (tiers + user's request + write log) with one tier-1 action (Discourse
  bookmark).
- Both skills, the three workflow recipes, generated site docs, module-format version 1.
- Offline parser tests and the docs-in-sync test.

**Deferred (planned)**

| Item | Notes |
|---|---|
| `chrome-profile` mode | Per-session minimal copy of the user's profile (Local State, cookies, local storage, IndexedDB, preferences; **no** saved passwords or cards), nothing written back, deleted after. **Verified not to work on Windows (2026-10-26, Chrome 156):** Chrome locks its cookie DB while running (copying fails), and with Chrome closed every cookie is App-Bound-encrypted (v20) and real Chrome on the copy reads none of them: the key is bound to the original profile location. browser-use's profile copy and Orca's direct import (which skips v20 cookies) hit the same wall. Remaining paths: cookie-file import (Orca's fallback: JSON array of `{domain, name, value, path, secure, httpOnly, sameSite, expirationDate}`) or Chrome's sanctioned `chrome://inspect/#remote-debugging` attach (Chrome 144+; consent prompt per connection, automation banner; confirm patchright can connect). |
| Per-site request limits | Concurrency caps and politeness beyond retry-on-429. |
| Automated live smoke tests | Opt-in, need network and sometimes logins. |
| Browser write firewall | Per-site endpoint rules recorded during authoring, GraphQL mutation detection, hold-and-ask for unknown first-party mutating requests, third-party ignored; would need a hold/rules hook in patchright-cli. Only if UI-driven writes become common. |
| MCP server | One server, three generic tools (§4.3). |
| Tier-2 actions, account sites | Amex offers first (tier 1 enrollment), then others. |
| Trust gate for downloaded modules | e.g. `esum site enable <name>` after review. |

---

## 14. Relationship to other projects

- **patchright-cli** — the browser layer (daemon, sessions, persistent profiles, CDP attach). ESUM
  depends on it and never duplicates it. Exploration in the authoring skill uses patchright-cli
  CLI subcommands (D-44).
- **xgoose** — source of ported site knowledge (MIT). ESUM does not run xgoose code.
- **nitan_plugin userscript** — stays independent as the one-click in-page summarizer; its adapter
  knowledge is ported into ESUM engines.

---

## Decision log

One row per decision, saying the rule as it stands. Each decision's full text as first written,
with its measurements, rejected options and before/after wording, is in
[docs/decision-log.md](docs/decision-log.md) (D-98).

| # | Decision |
|---|---|
| D-1 | Not read-only: include writes; the agent is capable, ESUM defines tools and workflows. v1 targets tiers 0–1; tier 3 never. |
| D-2 | Browser modes: managed, attach, and (deferred) sandboxed copy of the saved profile. |
| D-3 | Python. |
| D-4 | Standalone repo (`github.com/AhaiMk01/esum`); package, CLI and skill named `esum`. |
| D-5 | Transports T1/T2/T3 declared per site; no code injection beyond a generic in-page fetch. |
| D-6 | Compact agent text by default, `--json` optional, large results spilled to `./esum-out/`. |
| D-7 | Site knowledge ported into fresh Python from xgoose (MIT, provenance recorded) and the userscript. |
| D-8 | Userscript stays independent. |
| D-9 | Agent-agnostic: plain `SKILL.md` + CLI; distributed as an `npx skills` bundle. |
| D-10 | No MCP in v1; later only a three-tool generic server. |
| D-11 | Workflows are flexible Markdown recipes; promote to code only for routine bulk loops. |
| D-12 | A native Allow/Deny dialog for writes. **Superseded by D-34.** |
| D-13 | Default browser mode lives in the skill; the user's prompt can override it. |
| D-14 | Login needed → fixed message, user logs in, agent retries; agent never handles credentials. |
| D-15 | ESUM never calls a model. |
| D-16 | v1 sites: USCardForum/Discourse, Doctor of Credit, 美国信用卡指南, FlyerTalk, Reddit, Hacker News; one tier-1 action (Discourse bookmark) proves the gate. |
| D-17 | Errors pass through raw; "login needed" is a fixed signal. Its second fixed signal, "write not approved", is **superseded by D-34**; `help:` lines come from D-52. |
| D-18 | No cache between calls: every call fetches live (a disk cache could serve one account another's pages). Identical GETs within one read action are sent once; requests to a site are spaced ≥ 0.5 s apart, within and across processes (`extras["min_interval"]`, D-87). A 429 is retried for every method; reads are retried twice after 502/503/504, timeouts or dropped connections, writes never. State in `~/.esum/`. |
| D-19 | Offline parser tests always; live checks manual in v1; docs-in-sync test. |
| D-20 | uv packaging; Windows first, cross-platform by design. |
| D-21 | Parallel calls: tab per call in a shared session. |
| D-22 | Engines are code, sites are thin configs, grouped by engine on disk, addressed by site name; custom sites discovered at runtime via `esum list`. |
| D-23 | Separate `esum-site-author` skill; exploration via patchright-cli MCP, observe-only, fresh profile first. |
| D-24 | Integration level decides module vs Markdown site; Markdown sites are read-only by default. Its `esum approve` is **superseded by D-34**. |
| D-25 | Versioned module format; no trust gate for custom modules in v1. Custom modules are imported lazily, only when their own site is invoked; names come from folder names, never from running code; writes log `custom_code`. |
| D-26 | Not LLM judging and not HTTP-method filtering for write detection; any future browser gate uses per-site endpoint rules and fails closed. |
| D-27 | v1 trimmed: chrome-profile mode, per-site limits, live smoke automation and the firewall deferred. |
| D-28 | Refines D-5: a site's transport covers reads; a login-only write may declare its own write transport (engine default, `extras["write_transport"]` override). |
| D-29 | nitan-mcp added as a porting source (Discourse/USCardForum endpoints only, no auth or bypass code); USCardForum is a T3 site because Cloudflare challenges every plain-HTTP request. |
| D-30 | Refines D-5: T2 sends through curl_cffi with Chrome's fingerprint, not httpx. Borrowing the browser's cookies and user agent is the default and per-site optional (`extras["browser_cookies"]`), so T2 also covers fingerprint-checked public sites without a browser. |
| D-31 | Supersedes D-29's transport: USCardForum reads go over cookieless T2; the bookmark keeps T3 via `extras["write_transport"]`. Requests are spaced per site, not per host. |
| D-32 | Refines D-2: `native` browser mode attaches to the user's everyday Chrome through Chrome's consented remote debugging; opt-in, never default. It replaces `chrome-profile`. Attach and native use the host context and are detached, never closed. |
| D-33 | Refines D-21: a named patchright-cli tab per call replaces tab marks and the session lock, so parallel calls' browser requests overlap. (Minimum patchright-cli version: D-56.) |
| D-34 | No write dialog; `esum approve` and exit 4 (WRITE NOT APPROVED) are removed, and exit 4 is not reused. Tier-only writes, refusal of writes from read actions and the write log stay. Supersedes D-12, D-17's second signal and D-24's `esum approve`. Its approval mechanism (the host's permission prompt) is **superseded by D-36**. |
| D-35 | Adds V2EX and Substack: read-only standalone T1 sites ported from xgoose; V2EX's token v2 API is not ported. Out of scope from xgoose: X and LinkedIn, Amex and Capital One, and the chat skill. |
| D-36 | The trust model: the agent follows the skill. ESUM can't stop an agent that goes around it (same OS user) and asks for no host permission rules; it keeps its own path narrow (one reversible, logged write, no tier 3). What counts as approval is amended by D-85. |
| D-37 | Untrusted text can't forge the compact format: control characters are stripped from text output, user and title are one line, and post-body continuation lines are indented four spaces. Site `extras` keys are declared (`SITE_EXTRAS`, a module's `EXTRAS`); an undeclared key is a registry problem. `site()` / `engine()` take `origin=`, and an object no module registers is a registry problem. |
| D-38 | `--json` keeps one envelope (`site`, `action`, `untrusted_content`, `data`); the shape of `data` is each action's own. ESUM follows patchright-cli's published client protocol: `daemon-info --json`, an explicit `success` flag, an unknown protocol refused, the token read fresh and never printed. |
| D-39 | Supersedes D-19's "live checks manual": `live/` holds opt-in smoke checks (`uv run pytest live`), outside the offline suite. Per built-in site: one listing, one thread and one search, read-only, checking shape. LOGIN NEEDED skips. Report in `esum-out/live-report.md`. |
| D-40 | Amends D-5: a site may declare a stronger `fallback` transport, used for the rest of a call after a challenge. Challenge detection lives in the transport layer. Challenged writes are resent on the fallback. No memory between calls. A challenge without a fallback raises at once (D-61). |
| D-41 | Adds `wuaipojie` (a new Discuz! X engine; T2 with cookies, no search), `kanxue` (standalone, T2 with cookies), `linuxdo` (a Discourse config, T2→T3, inheriting the bookmark) and `nodeseek` (standalone, T2→T3). Site names must start with a letter. |
| D-42 | Adds `nga`, standalone, ported from xgoose: NGA's own JSON pages, T2 with cookies, no fallback. Guests can open almost nothing, so its guest messages are LOGIN NEEDED. Profiles, favourite boards and image attachment are not ported. |
| D-43 | Adopts AXI for the CLI surface. Lists return `Listing` and print as TOON tables with few default columns, `--fields` for more, the total, and a `help` block of complete next commands; an empty list exits 0. Errors go to stdout as `error:`/`help:`. `esum` alone prints a home view. `limit`, `offset` and `page` are checked before an action runs; a value out of range is a usage error. AXI's session hooks are not adopted. |
| D-44 | Refines D-23: `esum-site-author` is a step-by-step playbook from a URL to a working site, with a read-only probe script, patchright-cli recipes and templates. It live-runs read actions only. Custom sites keep scrubbed fixtures and tests in their own folder and may get an `esum-<site>` trigger skill, with the user's consent (reason: D-97). |
| D-45 | Batch use. A `--json` result that spills stays one JSON document on stdout, with the absolute path. `--max-retries` and `--min-interval` (or `ESUM_MAX_RETRIES` / `ESUM_MIN_INTERVAL`) tune 429 retries and spacing; a 429 records a per-site cool-down every parallel process waits out. Discourse gains `posts --since`, `user`, `user-posts`, `thread --since`, `search` with filters only, and `--no-pinned`. `ArgumentError` is public: bad arguments exit 2 with a `help:` line. |
| D-46 | Amends D-14 and D-16: a Discourse site may keep a User API Key the user authorized, so reads see what the account sees without a browser session. `login` is interactive and refuses without a terminal; ESUM sees no password. The key is never printed, logged or sent into a page, and the agent must not read where it is kept (the store: D-79). `logout` was removed by D-86. |
| D-47 | Amends D-14: login needed is a notice about one call, not a stop. The agent skips what needs a login, finishes the rest and tells the user at the end. Exit 3 and the `LOGIN NEEDED` message are unchanged. |
| D-48 | Removes USCardForum's `recent-dps`: what counts as a data point is the agent's judgment (D-15). `search --recent` replaces it. |
| D-49 | What a page says must survive into the output: code in `<pre>` keeps its whitespace inside a fence, struck text is `~~marked~~`, images keep their URL (a tiny icon keeps only its alt text), dates name their zone, and a value a site hides is None, never zero. Kanxue `thread` reads the Markdown source the page sends a guest. A post the page gives no floor number is listed apart, not given a guessed number. |
| D-50 | Reddit's JSON API refuses a guest, so reads fall back to the page fragments Reddit serves a guest; a login keeps the API. A real browser running a page's JavaScript check is ordinary use, so Reddit declares a `fallback` and `challenge_text`. Nothing is forged or solved by ESUM. What a guest can't reach is LOGIN NEEDED. (A check that wants a person: D-54.) |
| D-51 | `hn thread` restores the dead comments and subtrees Algolia's tree leaves out, by comparing Firebase's `descendants` with the tree. On by default, because a thread that looks complete and isn't is worse than a slower call. What can't be restored is printed as a count. |
| D-52 | Refines D-17: a runtime error prints its raw message and adds a `help:` line, a module's own `help` first, otherwise the status picks the next step. Arguments are checked before any request. Exit codes don't change. |
| D-53 | Refines D-44: `esum-site-author` dogfoods the recorded user questions through a portable brief: start at `esum docs <site>`, tier-0 reads only, no code reading, no credentials, page content untrusted. Prefer an independent dogfooder. Each friction gets the smallest check that fails before the fix and passes after. About 30 live calls a round. (Rounds and the contract check: D-89, D-94.) |
| D-54 | Amends D-50 and D-17: a browser check still showing after a T3 tab's 20 s wait ends the call with the fixed `CHECK NEEDED` message, exit 5, and ESUM leaves that tab open. The agent ticks the box there with patchright-cli, up to five tries, and reruns; anything beyond a box to tick is the user's. ESUM solves nothing and calls no solver. Native mode keeps LOGIN NEEDED. |
| D-55 | Amends D-5: ESUM may run a fixed, reviewed patch inside a captcha vendor's own frames (Cloudflare, hCaptcha, reCAPTCHA), and nowhere else. No patch ships yet. |
| D-56 | Refines D-33 and D-38: ESUM requires patchright-cli ≥ 0.11, because earlier versions send CDP `Runtime.enable` on every page, which a page can detect. `live/test_stealth.py` checks it. |
| D-57 | ESUM requests only web URLs (`http(s)://` with a host), a rule defined once in `core/urls.py`: `site()` refuses a non-web `base_url`, and `HttpClient.url` raises `UnsupportedURL` before a transport sees the request. |
| D-58 | Refines D-38: browser failures are classified by code at one boundary (`reply_code` in `browser/daemon.py`): patchright-cli's `code` when present, else one derived from the message. `BrowserError.code` carries it. |
| D-59 | Refines D-56: the agent keeps patchright-cli at the version ESUM names. On the too-old error it runs the `help:` line's upgrade commands without asking, and tells the user at the end. The version lives only in `MIN_VERSION`. |
| D-60 | Thread assembly lives in `core/thread_view.py`: callers supply the posts to display, coverage spans and an optional count; core owns sorting, deduplication, gap placement, the header count, JSON `posts` and the spill summary. `Result.next` carries the same checked `Next` commands as `Listing.next`; sites never construct help blocks. |
| D-61 | `HttpClient` raises `ChallengeError` when the last available transport returns a challenge, even with `check=False`. Its message has the status, the URL and a fixed bot-check description, never challenge HTML; its `help` depends on the transport. Engines no longer detect challenges. `ctx.http_for(transport, browser_cookies=)` gives a second client, and the runner closes every client. |
| D-62 | Balanced-element extraction (`element_end`, `element_inner`, `remove_elements`) and forum quote rendering (`quote_html`) live in `core/text.py`. Engines keep their selectors, author extraction and link policy. |
| D-63 | Two HTML conversion jobs in `core/text.py`: `html_to_body` keeps block structure (posts); `html_to_label` gives one line with an explicit anchor-link policy (titles, excerpts, poll options, flair). |
| D-64 | `HttpClient` takes a browser session and shares it with a call's other clients; one process session per browser mode and CDP address. `LoginNeeded` owns its message, help and exit 3; `CheckNeeded` owns exit 5. The active client's session names the login location; only Discourse's key login overrides it. |
| D-65 | A Discourse write may switch from key authentication to browser cookies during the POST. The account verifies the active browser user against the planned account before any browser POST. Only an exact `BAD CSRF` 403 permits one token-backed retry; ambiguous failures are never replayed. |
| D-66 | One failure outcome boundary: `core.outcomes.failure_outcome(error, argv)` gives every exception its message, help and exit code. The write gate wraps a `Write.run` failure in `WriteInterrupted(cause, written)`, and the outcome lists the completed items. Public site outcomes: `ArgumentError` (exit 2), `NoSuchItem` (exit 1), `SiteRefused` (exit 1). |
| D-67 | Action tests inject one `BackendFactory(site, options)` at `run_action` (the `action_runner` and `fake_http` fixtures). An autouse guard in the root `conftest.py` rejects outbound network in the offline tests and allows loopback; `live/conftest.py` turns it off. |
| D-68 | Reddit reads share `_read(...)`: a refusal returns `GuestAllowed(detail)` when the caller has a guest path, and a closed read raises LoginNeeded. A refusal while loading more comments keeps the tree read so far and its missing-comments note. |
| D-69 | Public `core.paging.read_pages(load, loaded, wanted, page_of, floors)` shares numbered-page fetching across Kanxue, Discuz, vBulletin, NGA and NodeSeek: missing pages are fetched once and the first copy of a floor wins. Sites keep totals, selection, page bounds and #0 policy. |
| D-70 | Managed mode's `background-open` needs patchright-cli 0.12.0; ESUM detects it by the daemon's reply, not by version. The general minimum stays 0.11 (D-56). |
| D-70 | (The number was used twice.) Discourse account ownership lives in `engines/discourse/account.py`: one account per action, which loads the key once, chooses the write client and verifies the planned account before browser writes. Requests carry `Credential(headers, page_safe, fallback, verify)` instead of key headers; transport drops a credential unsafe for T3 and strips `User-Api-Key` headers before every T3 send. Amends D-46, D-61, D-64 and D-65; its logout handling was removed by D-86. |
| D-71 | HTTP 429 on the last available transport is rate limiting, even with challenge markers: it takes the shared waits and retries and ends as a plain `HttpError`. Its help says to wait and rerun, with the complete command. `Response.http_error()` is the one way a failed response becomes an `HttpError`. |
| D-72 | Refines D-52 for Substack: an HTML body in an API error is replaced by `(a web page, not Substack's API)`, and a 404 from the archive gets help saying Substack has no search across publications. |
| D-73 | Thread reads say where to go next: `ThreadView.range_steps` turns the first gaps into complete `thread --from/--to` commands. An inverted range, or `--from` past the end (`RangePastEnd`), is a usage error. Gap markers count post numbers. A usage error names the nearest valid flag for a typo. |
| D-74 | The repository stays private and there is no PyPI release. Install is `uv tool install git+https://github.com/AhaiMk01/esum` plus `npx skills add AhaiMk01/esum`, after `gh auth login` and `gh auth setup-git`. |
| D-75 | A long US Credit Card Guide comment section pages through the Disqus API with Disqus's public embed key, read from the embed's script on every call. The key is never stored in code, docs or fixtures, and errors scrub it. |
| D-76 | Adds `matrix` (ITA Matrix; standalone, T1, tier 0 only, nothing books), the first site that is not a discussion site: Matrix's own JSON API, no key or login. A `search` value carries Matrix's session ids to `results` and `detail`, so ESUM caches nothing (D-18). `calendar --check N` runs exact-date searches for the cheapest dates. |
| D-77 | Adds `gflights` (Google Flights; standalone, cookieless T2, tier 0 only): it reads the public results page's embedded data, not the signed RPCs. The `search` value is Google's own `tfs` token, so it is stateless. `dates` runs one search per date, at most 31 per call. |
| D-78 | Adds `offerlove` (standalone, T1 with a T3 fallback): the site's server-rendered pages, and its v1 API only for `search` and `banks`. No key (the API-key `my-offers` was removed); `my-offers` now reads the My Offers page in the browser (D-103). |
| D-79 | Amends D-46: saved keys live in the OS credential store through `core/keystore.py` (`keyring`), falling back to `<ESUM_HOME>/auth/<site>.json`; `ESUM_KEYSTORE=file` forces the file store. `esum keys` lists which sites have a key, never a key. `esum keys ui` is the user's local page; changing a key there needs the OS to confirm the user. |
| D-80 | Adds `cardcenter` and `alignedincentives` (gift card buyers; standalone, T2 with the user's login, read-only): `rates`, `deals` and `deal` only. Reserving a deal, submitting cards and payout settings are never actions, at any tier. Every request is a GET to a fixed path with `follow_redirects=False`; parsers drop the account's id and e-mail. |
| D-81 | Adds `savewise` (standalone, T1, read-only, public): portal rates and card-linked offers from the API Savewise's extension calls, with the same anonymous per-call token. The extension's fixed bearer tokens and write endpoints are never used. |
| D-82 | Refines D-44 and D-53: `esum-site-author` checks a site against ground truth, what the site's own interface shows a person, in the login state the action uses. Fixture tests take expected values from ground truth, never from the parser's output; the live run compares one item per output kind; a repair starts by comparing the failing output with ground truth. |
| D-83 | Self-service override of built-in sites. **Superseded by D-96.** |
| D-84 | No custom engines in v1. A custom engine may never replace a built-in one; a built-in engine bug is fixed in esum itself (D-96). Custom sites on a platform with no ESUM engine are standalone sites. |
| D-85 | Amends D-36: approval is the user's yes when the agent asks before each write, in whatever way its harness asks; the original request alone is not approval. The agent may propose a tier-1 write that serves the request. No ESUM dialog, flag, plan ID or approving config. |
| D-86 | Amends D-16, D-46 and D-79: the Discourse `logout` action is removed, so the bookmark is the only write in v1. ESUM never revokes a key: the user revokes it on the forum, and `esum keys ui` removes ESUM's copy only. Logging out of a browser login is the agent clearing that site's cookies in ESUM's managed profile on the user's request (`patchright-cli cookie-clear` with a domain regex, 0.13.0). |
| D-87 | Refines D-18: a site may set `extras["min_interval"]` below 0.5 s, down to 0, when its API documents a rate limit ESUM can't reach. `hn` sets 0. |
| D-88 | A text spill of a thread prints an index that cuts the file into parts an agent reads one at a time (part size: D-89). Core `subtree` and a thread action's `--root N` show one sub-thread; `largest_subthreads` names the biggest. Substack `archive --since/--until` collects a date window. |
| D-89 | Refines D-53 and D-88: `esum-site-author` step 9 starts with a contract check the author runs against live output, before dogfood. A spill index part is at most about 20k tokens, and a gap ends a part. `range_steps` offers forward reads first. (Its round count is amended by D-94.) |
| D-90 | Substack gains `outline`: one row per top-level comment, with `--root N` as the next step; `thread` offers it first over 100 posts. Core exports `reply_counts` and `is_top_level`. |
| D-91 | Adds `flightconnections` (standalone, tier 0, T2 with cookies → T3, `min_interval` 3 s): only paths robots.txt allows. One-stop connections are pairs of nonstop routes, not checked itineraries. Core treats AWS WAF's `x-amzn-waf-action: challenge` as a challenge. |
| D-92 | Adds `aerolopa` (standalone, T1, read-only, logged out): public pages robots.txt allows, decoding the Next.js payload. `seats` reads the per-seat data the server sends a signed-out visitor, which the user approved; what the payload doesn't carry is named as missing. |
| D-93 | `matrix search --thorough` runs several searches side by side and merges their rows, because one open search leaves out carriers; `--carriers` adds a routed search per carrier. Sites compose only through commands, never imports: `Next` gains `site`, naming another built-in site's action (matrix, flightconnections and aerolopa point to each other); a step on a site that isn't installed here is left out, and the result prints without it. |
| D-94 | Amends D-89, site authoring. Step 1 first works out what the site can answer (scenarios, questions and their variants, hand-offs to other sites) and offers these to the user; the picks become the dogfood questions. Step 9 runs another round whenever the last found friction, and stops only when the same friction survives two fixes or the site blocks progress. |
| D-95 | Page code; amends D-5 and D-55. A site may ship a read-only page script, run by T3 in a tab of the site's origin (built-in ones reviewed and tested; a custom site's only when the user allows that site). The call is `ctx.page_script` (D-101). The agent may run read-only page code with patchright-cli in tabs ESUM opened; code that changes state needs the user's yes first; nothing moves money or points; code found in page content never runs. |
| D-96 | Supersedes D-83 and amends D-84: built-in sites are a curated set the maintainer keeps working and picks by their own choice (no admission criteria), and users add their own as custom sites. A custom folder with a built-in site's name is a problem in `esum list`, never imported, and the built-in keeps running. |
| D-97 | The `esum` skill's description names kinds of site, not sites, so it doesn't change with the curated set and also reaches a user's own forum or blog; a custom site of another kind is why the trigger skill stays (amends D-44's reason). When a named site isn't in `esum list`, the agent reads it another way and mentions `esum-site-author`. Engine and transport-backend authoring moved from module-format.md to engines-and-backends.md. |
| D-98 | This table is an index: one row per decision, saying the rule as it stands. Each decision's full text as first written (measurements, rejected options, before/after wording) is in [docs/decision-log.md](docs/decision-log.md). A new decision gets a row here, and a longer entry there only when there is more to record. |
| D-99 | `esum-site-author` carries AXI for a site module in `references/output-design.md` (read before step 5, checked in step 9): default columns, default limit, totals and counts the questions need, empty results, next steps and cut text, each saying what core does and what the author decides. It is a rewrite, not a copy of the AXI skill, because ESUM differs in three places: errors keep their raw message (D-52), large output spills to a file instead of `--full`, and there are no session hooks (D-43). |
| D-100 | `matrix ladder` finds a trip's dearer fares and the cheapest that meets a condition, because a search lists an itinerary once, at its cheapest fare. A coded loop under D-11. The fare is the unit: `--until` is a test of each fare (`not-basic` by rule title, `refundable` by category 16's cancellation statements, or words the agent picks that the statements must hold; ESUM matches words, it doesn't judge them), a rung fits when none of its fares fails, and only failing fares are left out of the next search, of the slices they priced (one filter per slice, which gives the filter's undirected city pair a direction), by carrier, city pair and fare basis code with its designator (`1:UA.SFO+TYO.KLX07JBH` sends `f ~((UA.SFO+TYO.KLX07JBH&~UA.SFO+TYO.KLX07JBH/-))` on slice 1: Matrix's bare code takes its designated fares along). Leaving a failing fare out never hides a rung that fits, so the ladder stops when a search's cheapest itinerary fits. Where the filter can't name a fare alone (Matrix takes 10 characters of a code, designator included), it goes wide, and a `caution` line names that fare, since the code's other fares on the slice go out unread; it also names a fare Matrix listed where its filter left it out. (A rung in a higher cabin leaves out the fares that price that slice; when Matrix doesn't say which flights a fare prices, all the rung's fares.) One booking class holds several fares: leaving classes out passed over the cheapest fare that fit (measured: USD 2,702 reported where USD 1,732 fit). A search reads up to 6 itineraries, one per price. Each rung quotes its fares' first statements of category 16; kind and those statements are ESUM's reading, the notes are Matrix's, and the agent confirms the answer in the rules. The climb is pure (`Climb`, `Condition`, `Fare` in `parse.py`); the action does the searches. It reports the cheapest among the fares Matrix lists, not a proven minimum. At most 6 searches a call, 3 by default; a failed first search is an error, a later one keeps the rungs found. |
| D-101 | The first page script: `gflights fares` reads Google's booking page, whose fare brands and agency prices come from a request only the page signs. `ctx.page_script(url, script, arg)` opens a background tab on a page of the site's own origin, runs the site package's static script, closes the tab and returns its data; built-in sites only (a custom site's is refused until the per-site allow of D-95 exists). The script waits for the options and returns the page's text; Python parses it. A page script reads, and may click to reveal what it reads: `gflights search --all` clicks "View more flights" on the results page and returns each row's own attributes (opt-in; the default search stays one HTTP page). It never submits, books, signs in or changes anything at the site. A row from behind the button is named by its flights in full (each flight's airports, date and number, the form `fares --flights` takes): `returns`, `detail` and `fares` read the itinerary by that name from the returns page for those flights over HTTP, so the expanded list is not read again, no browser opens, and the `search` value is the same with or without `--all`. Google hands that page what it has gathered so far, so `--all` reads it again until a read adds no row and lists every read's rows. The agent doesn't open these pages itself with patchright-cli: a pattern worth repeating is a page script (D-95). |
| D-102 | One folder per site, built-in or custom: its module, helpers and page scripts, its generated `doc.md` and its `tests/` (`test_<site>*.py`, `fixtures/`), so moving a site between two trees is moving that folder. Built-in: `src/esum/sites/<site>/` or `src/esum/sites/<engine>/<site>/`. The registry never imports a folder named `tests`; the wheel ships `doc.md` beside each module and leaves `tests/` out. Agents read a doc with `esum docs <site>`. Deleting one site's folder leaves the offline suite passing: core tests in `tests/` run on the example world of the root `conftest.py`, engine tests on synthetic site configs and the engine's own fixtures (`tests/fixtures/<engine>/`, helpers in `tests/support/`), never on a built-in site; a site config's tests may use its engine's fixtures and helpers; a test of a step on another site is skipped where that site isn't installed; tests generated from the registry are contracts every installed site meets, and fail on an empty registry. |
| D-103 | `offerlove my-offers` reads the signed-in user's own offers, every bank and card, from the My Offers page with a page script (D-95, D-101), at the user's request (amends D-78). The page gets them over a WebSocket and keeps them in its IndexedDB store `OFFER_LOVE_DB`; its pager stops at page 22, so the script waits for the page to say signed in and synced, then reads the store's `offersData` and returns a fixed set of fields per offer, leaving out tokens and account ids. It never reads the sign-in store (`firebaseLocalStorageDb`). A sign-in button means LOGIN NEEDED; a page showing neither button is a page problem, not a sign-out. Still no key: the user signs in in ESUM's browser. |
