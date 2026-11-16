# ESUM module format, version 1

This is the contract between ESUM and its engine, site and Markdown-site modules. Every module that
creates an engine or a site declares the version it targets:

```python
FORMAT = 1
```

ESUM refuses to load a module that has no `FORMAT`, a non-integer `FORMAT`, a newer format than it
supports ("upgrade esum") or a format it no longer supports ("port the module"). A refused module
only breaks its own site, and every other site keeps working. A refused built-in module is listed
under `! not loaded:` by `esum list`; a custom module is imported only when its site is used (see
§1), so its error shows when you run `esum <site> --help`. Markdown sites declare `format: 1` in their
frontmatter.

The public API is `esum`'s top-level namespace. Import from `esum` only, never from `esum.core.*`:

```python
from esum import (FORMAT, Tier, Transport, Credential, engine, site, action, Write, Result, Listing, Next,
                  Post, Gap, thread_view, read_spans, before_span, render_posts, format_post, parse_date,
                  parse_since, subtree, largest_subthreads, reply_counts, is_top_level, command_line,
                  Mode, PageRead, read_pages, select_numbers, to_spans, chunked, html_to_body, html_to_label, inner_div, one_line,
                  element_end, element_inner, remove_elements, quote_html, QUOTE_CHAR_CAP, text_of, clip,
                  LoginNeeded, HttpError, ArgumentError, NoSuchItem, SiteRefused)
```

## 1. Kinds of site and where their files go

| Kind | Use when | Built-in (in this repo) | Custom (user's own) |
|---|---|---|---|
| **Engine** | Reusable platform code (Discourse, WordPress, vBulletin) | `src/esum/engines/<engine>/__init__.py` (+ any helper modules beside it) | not supported in v1 |
| **Site config** | A site on an existing engine | `src/esum/sites/<engine>/<site>/__init__.py` | `~/.esum/sites/<engine>/<site>/site.py` |
| **Standalone module** | No engine fits (HN, Reddit) | `src/esum/sites/<site>/__init__.py` (+ helpers) | `~/.esum/sites/<site>/site.py` (+ helpers, imported as `from . import x`) |
| **Markdown site** | The task needs UI driving, signed requests or unstable endpoints | `src/esum/sites/<site>/SITE.md` | `~/.esum/sites/<site>/SITE.md` |

Discovery is automatic. There is **no central list** to edit. The registry imports every `.py` module
under `src/esum/engines/` and `src/esum/sites/` recursively and registers every `Engine` or `Site`
object a module *creates* (an imported or re-exported object does not count twice). It also registers
every `SITE.md` it finds under `src/esum/sites/`. Details:

- A module registers what it creates. When a helper function in another module builds the object
  (a factory shared by several sites), pass `origin=__name__` to `site()` / `engine()` so the
  calling module registers it. A module holding an `Engine` or `Site` that no module registers is
  reported as a problem in `esum list` instead of being dropped silently.

- `__init__.py` in subfolders is optional. Files and folders whose names start with `_` or `.` are
  skipped, so name private helpers `_parse.py` if you don't want them imported eagerly. Importing a
  helper that defines nothing is harmless. A folder named `tests` is never imported: it holds a
  site's offline tests and fixtures (D-102).
- A module may define several sites (for example several Discourse forums in one file).
- Site names and engine names match `^[a-z][a-z0-9-]*$`, must be unique, and cannot be an esum
  command (reserved names: `list`, `docs`, `help`). Sites are addressed by name:
  `esum <site> <action>`.
- `~/.esum/` can be moved with the `ESUM_HOME` environment variable. A folder in `~/.esum/sites/`
  named after a known engine holds site configs for that engine. Any other folder is a standalone
  or Markdown site. A custom site can't take a built-in site's name: `esum list` reports the
  folder under `not_loaded`, never imports it, and the built-in keeps running (D-96).

Custom sites are discovered **without running their code** (D-25):

- **The folder name is the site name.** `~/.esum/sites/<site>/site.py` and
  `~/.esum/sites/<engine>/<site>/site.py` must define exactly one site, named `<site>`. A custom
  `site.py` can't define several sites, and a site with another name is refused. A Markdown site's
  name comes from its `SITE.md` frontmatter (`name:`, else the folder name), which is parsed, not
  executed.
- A custom `site.py` (and anything it imports) is imported only when that site is invoked:
  `esum <site> …`, `esum <site> --help` or `esum docs <site>`. Nothing else imports it, including
  `esum list`, `esum --help`, `esum docs --all` and every built-in site.
- `esum list` shows a custom module site that hasn't been used yet by name and engine folder only:
  transport `?` and a placeholder description. `esum list --json` gives it `"loaded": false` and
  `"actions": null`. Run `esum <site> --help` to import it and see its actions or its load error.
- Custom modules load without a trust gate in v1, because they are the user's own code. When you
  invoke a custom site, its code runs in the same process as the write gate and is trusted like the
  rest of ESUM. Every `writes.jsonl` entry records the custom `site.py` files loaded in that process
  (`"custom_code": [...]`, empty when only built-in code ran).

**One folder per site, built-in or custom (D-102).** A module site's folder holds its whole trail:

```
<site>/                 src/esum/sites/[<engine>/]<site>/  or  ~/.esum/sites/[<engine>/]<site>/
  __init__.py | site.py   the module: __init__.py built-in, site.py custom
  parse.py, *.js          helpers and page scripts, when it has them
  doc.md                  generated: esum docs <site> --write (keeps ## Quirks)
  tests/
    test_<site>*.py       offline tests, named after the site so every site's tests run together
    fixtures/             scrubbed saved responses only this site's tests read
```

Moving a site between two trees is moving that folder, and deleting it leaves every other test
passing (§9). The registry never imports its `tests/`, and the wheel leaves every site's `tests/`
out. An engine's own tests and fixtures and the core tests are in the checkout's `tests/`, and
depend on no built-in site. A Markdown site's folder holds its `SITE.md`.

## 2. Engine

Engines are platform code shared by several sites, and they are built-in only: custom engines
aren't supported (D-84). Writing one in the esum repo is in
[engines-and-backends.md](engines-and-backends.md).

Engine actions run for every site on the engine. They read per-site settings from `ctx.site`, which
has the fields `name`, `base_url`, `transport`, `charset`, `headers`, `extras` and `label`.

**Site extras.** `extras` is a plain dict; there is no schema. The registry checks only its keys,
so a misspelled key (`browser_cookie`) shows up in `esum list` as a problem instead of silently
falling back to a default. The site stays usable. A module declares the keys its actions read in a
module-level `EXTRAS` tuple, next to `FORMAT`. Known keys for a site are:

- the keys the transports and browser layer read, `SITE_EXTRAS` in `esum.transport`
  (extras every site may set: `min_interval`, `browser_cookies`, `browser_home`, `login_check`, `challenge_text`,
  `key_entry`),
- its engine module's `EXTRAS`,
- its own module's `EXTRAS`, for site-specific actions that read further keys.

A site on an engine is always checked. A standalone site is checked only when its module declares
`EXTRAS` (an empty tuple is fine), so older standalone modules keep working unchanged.

## 3. Site config (on an engine)

```python
# src/esum/sites/forum/myforum/__init__.py
from esum import Tier, Transport, action, site

FORMAT = 1
PORTED_FROM = "nitan_plugin My.user.js (forum adapter)"   # when knowledge was ported

myforum = site(
    name="myforum",
    engine="forum",
    base_url="https://forum.example",
    transport=Transport.DIRECT,          # T1; or BROWSER_HTTP (T2) / IN_PAGE (T3)
    fallback=None,                       # e.g. Transport.IN_PAGE: switch to it after a Cloudflare
                                         # challenge, for the rest of the call (D-40)
    label={"en": "Example forum: what it is good for, one line", "zh": "…"},
    charset=None,                        # e.g. "iso-8859-1" to override the response's charset
    headers={},                          # sent with every request
    extras={"categories": {"dp": 12}},   # read by the engine via ctx.site.extras; keys are checked
)


@action(myforum, tier=Tier.READ)          # optional site-specific extras
def recent_dps(ctx, days: int = 7):
    """Recent data-point topics."""
    ...
```

A site action with the same name as an engine action overrides it for that site
(`@action(myforum, tier=…, name="thread")`).

## 4. Standalone module

This is the same as a site config without `engine=`. All of the site's actions are declared on the
site object. If its actions read `extras`, declare those keys in the module's `EXTRAS` tuple so a
misspelled key is reported (see "Site extras" in section 2).

## 5. Markdown site

`SITE.md` is the whole site: a guide the agent follows with patchright-cli. It needs this
frontmatter:

```markdown
---
name: mysite
description: One line for `esum list`
format: 1
base_url: https://mysite.example
verified: 2026-10-26        # date it was last run end to end
writes: none                # or "ask": the guide tells the agent to ask the user before each write
---
# mysite
…steps, what to extract, pitfalls…
```

Markdown sites are read-only by default. If one allows writes, it must tell the agent to ask the
user before each write and to stop unless the user agrees. This gate is advisory and is documented
as such.

## 6. Actions

`@action(owner, tier=Tier.X, name=None)` takes the engine or site object. The tier argument is
required.

- The first parameter must be `ctx`. Every other parameter becomes a CLI flag: `from_` → `--from`,
  `max_pages` → `--max-pages`. The action's CLI name is the function name with `_` → `-` (or
  `name=`).
- Every parameter needs an annotation. These are supported:

  | Annotation | Flag |
  |---|---|
  | `int`, `float`, `str` | `--x VALUE` |
  | `bool` (needs a default) | `--x` / `--no-x` |
  | `Literal["a", "b"]` (all str or all int), e.g. `Mode` | `--x a\|b` (choices) |
  | `list[int]`, `list[str]`, … | `--x V1 V2 …` |
  | `X \| None = None` | optional `--x` |

  A parameter without a default is a required flag. `*args`/`**kwargs` are rejected, and so are the
  global flag names (reserved global flags: `json`, `browser`, `cdp`, `fields`, `no-spill`, `max-retries`,
  `min-interval`, `help`, `h`, `version`).
- **Docstring (required).** The first paragraph is the summary shown in `esum <site> --help` and in
  the docs. Further paragraphs become the description. A Google-style `Args:` section supplies the
  help text for each flag.
- **Return value** (tier 0):
  - `Listing(name, rows, fields, about={}, total=None, more=None, empty=None, next=[], data=None)`
    for every list (topics, stories, search hits, boards; D-43). It prints as a TOON table:
    `name[N]{fields…}:` with one row per line, after `about` (one `key: value` line each: the
    board, query, sort, URL) and `count: N of TOTAL total`. `rows` are dicts holding every field
    the action knows; `fields` are the default columns, the fewest that let an agent choose its
    next call (an id, a title and one or two numbers). `--fields a,b` adds columns and
    `--fields all` shows them all. `more` holds the arguments that differ for the next page
    (`{"page": 2}`); the next-page hint repeats the call's other arguments. `next` lists
    `Next(action, args, purpose)` steps, written with placeholders (`{"id": "<id>"}`).
    `Next(..., site="other")` names another built-in site's action (sites hand off through
    commands, never imports; core checks the action and its flags). When that site isn't
    installed here the step is left out and the result prints without it: a distribution can
    carry some sites and not others. For a `help` string on an error, build the command with
    `other_site_command(site, action, args)`, which returns None in that case. An empty
    list is an answer: return `Listing` with no rows and `empty` saying why, never raise. `--json`
    prints `data`, by default `{**about, "total": total, name: rows}`.
  - `str`: printed as is (`--json` prints `{"text": …}`).
  - `Result(data=…, text=…, summary=…, next=[])`: `text` is the compact rendering, `data` is what
    `--json` prints, and `summary` replaces the preview shown when the output is spilled.
    `next` holds the same `Next(action, args, purpose)` steps as a `Listing`. Core checks each
    action name and quotes argument values through the command builder. Core appends a TOON
    help block; actions never build that block themselves. For both result types, JSON
    includes an optional envelope `help` array of complete next-command hints (a list's next
    page first), and a spilled result keeps them on stdout. With no steps, the JSON envelope is
    unchanged. `Next.when_empty` applies only to listings.
  - JSON-able data (dicts, lists, `Post`/`Gap`, dataclasses, datetimes): `--json` prints it, and
    text mode prints it as indented JSON.
  - The `--json` envelope (`site`, `action`, `untrusted_content`, `data`, optional `help`) is the same everywhere;
    the shape of `data` is the action's own and differs between sites (D-38). Name its fields
    after what they hold, reuse the names a similar action already uses (`posts`, `total`, `url`)
    where they fit, and describe them in the docstring when they aren't obvious.
- The output is labeled for the agent as untrusted site content. Text gets a one-line banner (a
  `untrusted:` line in a list) and JSON gets an `"untrusted_content": true` envelope. Output over 20,000 characters (override with
  `ESUM_SPILL_CHARS`) is written to `./esum-out/` and stdout gets the path plus a summary or a
  preview. Don't print from actions; return the result.

### Compact posts and paging

- Build each post as `Post(number, user, text, date=None, likes=0, is_op=False, reply_to=None,
  url=None)`. It renders as `[user #N] (date · 👍likes · OP · ↩#M): text`, and parts that don't
  apply are left out. When a post replies to one whose number isn't known, set `reply_to_id` to the
  site's post id: it prints as `↩post N`, so "not read" stays apart from "not a reply". Continuation lines are indented four spaces (`    more text`).
- `thread_view` assembles exactly the supplied posts into a `ThreadView` (D-60). Callers choose
  the posts to show, including #0 articles and any opening post for context, and describe what
  missing numbers mean with inclusive `(first, last, reason)` spans:

  ```python
  from esum import thread_view, read_spans, select_numbers

  wanted = set(select_numbers(highest, mode, from_, to))  # also used to plan requests
  shown = [p for p in fetched_posts if p.number in wanted]
  return thread_view(shown, read_spans(wanted, highest), count=total).result(
      title, url=url, data={"id": id}, notes=["Times: UTC"])
  ```

  The signatures are:

  ```python
  thread_view(posts: Iterable[Post], spans: Iterable[tuple[int, int, str]], *,
              count: int | None = None) -> ThreadView

  read_spans(numbers: Iterable[int], highest: int) -> list[tuple[int, int, str]]
  before_span(cutoff: datetime, last: int) -> tuple[int, int, str]

  ThreadView.result(title: str, *, url: str | None = None, data: dict | None = None,
                    notes: list[str] | None = None, suffix: str = "",
                    next: list[Next] | None = None) -> Result
  ```

  `posts` contains exactly the posts to display. Core sorts and deduplicates them by number.
  The view extends from the smallest to the largest number named by a post or nonempty span.
  The first span covering a missing number supplies its reason; numbers no span covers use
  `not read`. Reasons are opaque text: core has no site-specific priorities or selection flags.
  It splits gaps at span boundaries and merges adjacent gaps with the same reason, without
  iterating through every missing number. Reversed spans are ignored. An empty view has count 0.

  `read_spans` returns `deleted or hidden` spans for the supplied numbers, clipped at `highest`, followed by a
  `not read` fallback over #1 through `highest`. `before_span` returns
  `(1, last, "before YYYY-MM-DD HH:MM UTC")`, converting an offset datetime to UTC and treating
  a naive datetime as UTC. More specific reasons precede fallback spans:

  ```python
  spans = [(11, 20, "members only"), *read_spans(wanted, highest)]
  spans.insert(0, before_span(cutoff, first_new_number - 1))
  ```

  A focused range uses posts and spans limited to that range. A #0 article is a supplied post,
  not a flag. When it is omitted, no #0 span is needed. `count` is the actual total post count,
  including omitted articles when appropriate; it defaults to the size of the view's numbering
  domain. It never controls gap placement. Supply it explicitly for focused reads, deleted
  floors, or partially loaded comment trees where numbering and total count differ.

  `view.items` is the ordered `Post | Gap` list and `view.count` is the header's total count.
  `view.result` adds `posts` to the supplied JSON metadata and uses the same count in text and
  summary. `notes` precede the posts; `suffix` follows them (for site-specific linked sources or
  recommended posts). The summary contains the title, URL, notes, and `N of M posts shown`.
- The FORMAT 1 helpers `with_gaps(posts, total, reason="")` and
  `render_thread(title, items, url=, total=, notes=)` remain supported for custom sites.
  `with_gaps.total` is the highest post number; `render_thread.total` is the actual post count.
  They keep their existing behavior, including unlabelled gaps and #0 handling. New modules
  use `thread_view` to keep these meanings separate. A gap renders as
  `[... omitted 140 post numbers (#31–#170), not read …]` when built through the new view (it
  counts numbers, since a deleted post keeps its number).
- `view.range_steps(args=None, chunk=300)` returns up to two `Next` steps that read what a
  `not read` or `before …` gap left out, as `thread --from A --to B`: a gap with no shown post
  before it, or a cutoff gap, is read backwards from its end; any other gap forwards from its
  start. Pass them as `next=` (plus any site steps). The runner fills in the call's other
  arguments, so `args` only needs values that must differ, such as a plain thread id where the
  caller may have passed a post URL. `--mode`, `--since` and `--posts` are cleared; clear any
  other flag that would override a range yourself (vBulletin passes `{"page": None, "post": None}`).
  The thread action must be named `thread` and take `from_` and `to`.
- Paged forums use `read_pages` (D-69) to fetch missing pages and keep the first item at
  each floor. Sites compute totals, select wanted floors, and probe any required pages first:

  ```python
  from esum import read_pages, read_spans, select_numbers, thread_view

  first = load(1)
  wanted = set(select_numbers(first.total, mode, from_, to))
  read = read_pages(load, {1: first}, wanted,
                    lambda n: (n - 1) // per_page + 1,
                    lambda page: ((p.number, p) for p in page.posts))
  shown = [convert(p) for n, p in read.floors.items() if n in wanted]
  return thread_view(shown, read_spans(wanted, first.total), count=first.total).result(title)
  ```

  ```python
  read_pages(load: Callable[[int], P | None], loaded: Mapping[int, P | None],
             wanted: Iterable[int], page_of: Callable[[int], int],
             floors: Callable[[P], Iterable[tuple[int, T]]]) -> PageRead[P, T]
  ```

  `loaded` maps page numbers to already fetched page objects. `None` marks an unavailable
  page that must not be retried. The reader fetches each missing wanted page once in ascending
  order. It copies `loaded` and never mutates the caller's mapping. `load` returns `None` for
  unavailable pages; other errors propagate. `floors` yields `(floor_number, item)` pairs from each page.
  The first item at each floor wins in ascending page order, including unselected quote context.

  `PageRead.pages` holds available page objects, `floors` holds deduplicated items by floor
  number, and `unavailable` lists unavailable page numbers in ascending order. Sites filter
  `floors` by `wanted` for display and use all fetched items for quote/name resolution as needed.
  Sites retain total calculation, `select_numbers`, last-page probes, page bounds, #0 selection,
  post-ID deduplication, and login-wall stopping in ordinary code around the reader. Sites build
  coverage with `read_spans` and any special reasons. Discourse's API fetching stays in its engine.
- `select_numbers(total, mode, from_, to, head=30, tail=30)` returns the post numbers to fetch.
  `medium` is head + tail, `full` is everything, and `--from`/`--to` override the mode. An inverted
  range raises ArgumentError, and a `from_` past `total` (when `total` > 0) raises `RangePastEnd`,
  an ArgumentError whose `help:` the runner completes with the command that reads the last posts.
  The runner also refuses an inverted `from_`/`to` before the action runs. Use
  `to_spans` to turn the numbers into ranges and `chunked(ids, 50)` to batch them per request.
- `subtree(posts, root)` returns `root` and every post replying under it (by `reply_to`, any depth), in
  number order: a thread action's `--root N` shows one sub-thread with it, labelling the rest as a gap
  such as `outside #N's sub-thread` (Substack's `thread` does). A missing root is an ArgumentError.
- `largest_subthreads(posts, n=5, least=3)` returns `(number, user, replies)` for the top-level comments
  with the most replies under them, largest first: name them in a note and offer `--root` steps, so an
  agent reads the biggest arguments first.
- `reply_counts(posts)` maps each post number to the replies under it (any depth), and
  `is_top_level(post)` is true for a comment replying to nothing or to #1. Together they make an
  outline action: one row per top-level comment with its replies, likes and first line, so an agent
  sees a big comment section's shape before reading it (Substack's `outline`).
- Balanced HTML helpers share one implementation:

  ```python
  element_end(html: str, start: int, tag: str = "div") -> int
  element_inner(html: str, opening: str, tag: str = "div", *, start: int = 0) -> str | None
  remove_elements(html: str, opening: str, tag: str = "div", repl: str = "") -> str
  ```

  `start` in `element_end` points at the opening tag; the result is just past its balanced
  closing tag, or EOF when unclosed. `opening` is a case-insensitive regex selecting an opening
  tag. `element_inner` returns its inner HTML, including nested elements, or `None` when absent;
  its `start` is a search offset. `remove_elements` replaces each selected element, including
  its children, and does not search replacement text. Self-closing elements do not increase depth.
  `inner_div(html, start)` remains supported and returns the inner HTML of the div at `start`.
- Quotes use the same body conversion as ordinary posts:

  ```python
  quote_html(author: str, reference: str | int | None, body_html: str, *,
             cap: int | None = QUOTE_CHAR_CAP, hint: str = "", keep_links: bool = False) -> str
  ```

  This returns HTML to insert into the post before calling `html_to_body`, which converts the
  quote body once. `author` and `reference` are plain labels, escaped by core, joined with a
  space and followed by a colon when the body is nonempty; pass an author such as `@alice`
  and a reference such as `#3`.
  Empty labels are omitted. Every quote line has a `> ` prefix. Paragraphs, code indentation,
  code fences, images and struck text keep their structure. `QUOTE_CHAR_CAP` is 200 characters
  of converted body text, including fence lines and nested quote prefixes, excluding the heading
  and the outer quote prefixes added by this helper. A cut adds `…`; a nonempty
  hint adds `[cut at 200 chars<hint>]`, for example `hint="; whole in #3"`. A truncated code block
  gets a closing fence outside the cap so following prose stays outside the code. `cap=None`
  keeps the complete body; other caps must be positive. Discourse passes body budgets of 299/79
  characters so the appended ellipsis fits within its 300/80-character output limits. A body
  exactly 300/80 characters long is now clipped too. vBulletin disables the
  cap when the source post is absent from the output. Sites select boxes and labels; core owns
  conversion and clipping. Labels and hints receive the same backtick escaping as ordinary
  prose. Core recognizes generated quote metadata only with its process-local random marker;
  arbitrary `data-esum-quote` attributes in page HTML have no special meaning.
- `text_of(fragment: str | None) -> str` strips HTML tags, decodes entities and collapses
  whitespace for titles, names and other one-line fields. `clip(text: str, cap: int | None,
  hint: str = "") -> str` keeps up to `cap` characters and appends an ellipsis when cut;
  a hint adds the same source annotation as quotes. `None` disables clipping.
- HTML conversion has two named jobs, exported from `esum`:

  ```python
  html_to_body(html: str, *, keep_links: bool = True, url_cap: int = 120) -> str
  html_to_label(html: str, *, keep_links: bool, url_cap: int = 120) -> str
  ```

  `html_to_body` converts post HTML (cooked/content) with block line breaks, quote prefixes,
  fenced code with its whitespace, strike marks (`~~`), numbered/nested lists, and image
  descriptions/URLs under D-49's tiny-image rules. Scripts are dropped and entities decoded.
  `html_to_text` remains a compatible body entry point for FORMAT 1 custom sites.
  `html_to_label` is for titles, poll choices and listing excerpts: it collapses all body
  whitespace, including code indentation and list nesting, to one space and strips terminal
  controls. Visible fences, quote prefixes, strike/list markers and image descriptions remain;
  they no longer promise block structure. Choose `keep_links` explicitly: `False` drops anchor
  destinations, `True` includes them under the same `url_cap` as bodies. Image URLs follow the
  body rules independently of anchor policy. Use a body for a preview that must retain code or
  paragraph structure; do not flatten a converted body by hand.
- `one_line(text)` is for plain-text fields in list and search rows you render yourself, so a
  newline in an author or category can't start a fake row. HTML fields use `html_to_label` instead.
  Threads rendered
  with `ThreadView.result` / `format_post` need nothing extra; the output layer strips control
  characters from everything.

### Failure outcomes

Import these three outcomes from `esum` and keep the message specific to the input:

| Exception | Meaning | Exit | Default next step |
|---|---|---|---|
| `ArgumentError(message: str, help: str | None = None)` | Malformed id, unsupported argument or out-of-range page | 2 | `esum <site> <action> --help` |
| `NoSuchItem(message: str)` | A valid id or name has no item | 1 | Check the id or name; it may have been removed |
| `SiteRefused(message: str)` | The site refused the request for a reason login would not fix | 1 | `esum docs <site>` says what it needs |

A refusal that logging in fixes stays `raise ctx.login_needed(...)` (exit 3).

A `help` that suggests another call names a complete command. Build it with
`command_line(site: Site, act: Action, args: dict[str, Any], *, placeholders: bool = True) -> str`
(`command_line(ctx.site, ctx.action, {...}, placeholders=False)` for the same action), the builder
`Next` steps use: it omits arguments at their defaults, quotes values for the shell and glues a
value starting with `-` to its flag (`--query=-spam`). With `placeholders`, a `<id>` value stays
unquoted. Don't assemble commands by hand.
Unexpected layout/data errors retain the generic repair guidance (exit 1). A custom module's plain
`ValueError` or `LookupError` is treated as a layout failure (exit 1, site-repair help), not as bad
input. Raise `ArgumentError`, `NoSuchItem` or `SiteRefused` for those expected outcomes.
Distinguish user input from parsed site data: a malformed canonical URL or missing page structure
is a layout failure. For a name lookup in a parsed catalog, report `NoSuchItem` only when the
parser found a non-empty catalog; an empty parsed catalog may mean the layout changed.
Thread-reference parsers raise `ArgumentError` on malformed input; they return a parsed
reference only on success, never a missing-reference sentinel.

`esum.core.outcomes.failure_outcome(error: Exception, argv: Sequence[str] = ()) -> FailureOutcome`
accepts command arguments only after global flags have been parsed (or an empty sequence when
parsing failed) and is the rendering boundary. `FailureOutcome` is a named tuple `(message: str, help: str, exit_code: int)`.
The CLI calls it once per failure, for text and JSON alike; callers rendering failures use the same
interface. Error messages remain raw, except the CLI removes the existing `esum: ` prefix from
LOGIN NEEDED and CHECK NEEDED.

The gate raises `esum.core.errors.WriteInterrupted(cause: Exception, written: list[str])` from a
failed `Write.run`, retaining the original exception as `cause` and its exception chain. It owns a
copy of the completed descriptions; the cause has no `written` attribute. The outcome preserves
the cause's message and exit code, and appends the write guidance once, after the cause's help. Nested interruptions merge completed descriptions and render one suffix.
A local write-log failure names the log path and completed items; skipped records are attempted
individually even if an earlier log record fails.
Read each item's state and retry only unapplied items. LOGIN NEEDED and CHECK NEEDED keep their
existing first line and browser guidance.

## 7. Fetching: `ctx.http`

Site code fetches through Context-owned clients. `ctx.http` uses the site's declared transport:

```python
ctx.http.get_json(path, params=None, headers=None)       # Accept: application/json, parsed
ctx.http.get_text(path, …)                               # decoded text
ctx.http.get(path, …) -> Response                        # .status .url .headers .content .text() .json()
ctx.http.request(method, path, params=, headers=, data=, json=, content=, check=True,
                 timeout=30, dedupe=None, credential=None) -> Response
ctx.http.url(path, params=None) -> str                   # absolute URL
ctx.http_for(transport: Transport, *, browser_cookies: bool | None = None) -> HttpClient
ctx.login_needed(detail: str | None = None, where: str | None = None) -> LoginNeeded
ctx.http.login_needed(detail: str | None = None, where: str | None = None) -> LoginNeeded
ctx.http.uses_browser_login -> bool
ctx.http.login_where -> str
```

- **URLs.** ESUM requests only web URLs: `http://` or `https://` with a host. A path (with or
  without a leading `/`) is appended to `base_url`, which must be one (`site()` refuses any other, so
  the module fails to load). A URL with its own scheme (`x://…`) is used as is, and any non-web one
  raises `UnsupportedURL` before a request is sent. `params` are URL-encoded and `None` values are
  dropped.
- **Redirects.** T1 and T2 follow redirects. A site that must only ever be asked for fixed paths (an account back end, D-80) passes `follow_redirects=False` with `check=False` and reads the 3xx itself, usually as `ctx.login_needed(...)`; raise `resp.http_error("")` there when an error body could carry the account.
- **Errors.** A non-2xx status raises `HttpError(status, url, body)`. Pass `check=False` to inspect
  the status yourself, and raise `resp.http_error(body=None)` for a failure you don't translate: it
  keeps a 429's requested wait (`.wait`) and leaves a Cloudflare page's HTML out (D-71). A bot challenge always raises `esum.transport.ChallengeError`, a
  `HttpError` with the status, URL, a fixed bot-check description, and transport-specific `help`,
  even with `check=False`. Challenge HTML is omitted. Only Cloudflare checks are named Cloudflare.
  A declared stronger fallback is tried first. Browser CHECK NEEDED handling stays in the browser
  layer (D-54). A site can translate this error to its documented outcome, as Reddit does for
  LOGIN NEEDED (D-50). `ChallengeError(response: Response, site: Site, *, where: str)` receives the
  active session's location from the client. Network errors pass through raw.
- **Credentials.** Pass authentication through `credential=Credential(headers)`, imported from
  `esum`, rather than ordinary request headers or site defaults. Transport drops that credential
  before T3, including after escalation, so secrets never enter an untrusted page (D-46).
  For FORMAT 1 compatibility, T3 also strips `User-Api-Key` and `User-Api-Client-Id`
  case-insensitively from every header source before every send, including site defaults, plain
  request headers and page-safe credentials, after escalation and on retries.
  `Credential(headers={}, page_safe=False, fallback=None, verify=None)` can supply a separate
  page-safe fallback. Its optional `verify()` checks the account before each actual attempt;
  raising stops the send. Discourse owns its browser account check and supplies a headerless,
  page-safe credential for browser writes (D-65). `Response.transport` and `Response.credential`
  report what actually answered the request, including after escalation and on reused responses.
- **Client lifetime.** `ctx.http_for` caches clients by requested transport, and by browser cookie
  policy for T2 only. T1 and T3 ignore the cookie policy for client reuse. `browser_cookies=None` uses the site's policy; `True` gives a login-only T2 write its
  browser cookies even when reads are cookieless. The default policy returns `ctx.http` for the
  declared transport, including after that client has escalated to its fallback. Each client
  keeps any stronger declared fallback. The runner closes every
  client after planning and all `Write.run` calls finish, including on failure. Modules neither
  build nor close clients. Clients share the action's site throttle and GET reuse policy.
- **429.** Up to 3 retries, for every method, writes included: a 429 means the server did not
  process the request, so resending it can't duplicate a write. The wait comes from `Retry-After`
  (seconds or HTTP date), else Discourse `{"extras": {"wait_seconds": N}}` + 1 s, else 2/4/8 s. If
  the server asks for more than 30 s, the call fails with that wait in the message, and the agent
  decides what to do. A 429 carrying a Cloudflare challenge escalates to a declared fallback like
  any challenge, but on the last transport it is rate limiting: it is retried as above and ends as
  a plain `HttpError` 429, never `ChallengeError` or CHECK NEEDED (D-71). Error messages clip the
  URL at 200 characters; `.url` keeps it whole.
- **Short-lived failures.** A GET (or HEAD) that gets 502, 503 or 504, times out, or loses its
  connection is retried twice, after 1 s and then 2 s, with a note on stderr. Writes are never
  resent after such a failure (only after a 429), since the first attempt may have reached the site; on T3 that includes a write whose tab
  was closed mid-fetch, which fails instead. 4xx, 500 and Cloudflare challenges
  (`cf-mitigated: challenge`) are not retried. If the last try fails, its error passes through raw.
- **Charset.** The site's declared `charset` comes first, then the `Content-Type` charset, then a
  `<meta charset>` in the first 4 KB, then UTF-8. The labels ISO-8859-1, Latin-1 and ASCII decode
  as windows-1252, as browsers do, which keeps FlyerTalk's curly quotes intact.
- **No cache between calls.** Every `esum` call fetches live. Within one tier-0 action, a
  successful GET is sent once and reused for identical GETs (same URL and headers), in memory
  only. `dedupe=False` opts one request out. Reads
  made inside write actions are never reused.
- **Per-site spacing.** Right before each request goes out (after any browser set-up; 429 retries
  included), ESUM waits until 0.5 s have
  passed since any ESUM process last sent a request to that site (all its hosts count as one), so parallel calls queue up
  instead of bursting. Set `extras={"min_interval": <seconds>}` on a site to change it (0 turns it
  off).
- **Login.** When the response shows a logged-out session (login redirect, `current_user` missing,
  403 on an account page), `raise ctx.login_needed(detail="…")`. ESUM prints the fixed
  `LOGIN NEEDED` message and exits 3. The user logs in themselves, and modules never handle
  credentials. `ctx.login_needed` delegates to `ctx.http`; the active client names its browser
  session, including after escalation. When a module fetches through a client from `ctx.http_for`,
  it raises that client's `http.login_needed(...)`. `uses_browser_login` says whether the active
  transport carries browser cookies, so Discourse can choose its User API Key login override without
  inspecting transport state. `where` overrides the destination for that key flow only.

  `LoginNeeded(site: str, base_url: str | None = None, where: str | None = None,
  detail: str | None = None)` keeps its message template. Its `help` names the next step and
  `exit_code` is 3. Without `where`, it uses the mode-neutral `the browser ESUM uses`.
  Modules raise via `ctx.login_needed` or the active client's `http.login_needed` so the destination
  names the selected browser session.
  `CheckNeeded.help` keeps its browser-check steps and `CheckNeeded.exit_code` is 5.
  The CLI renders these errors' messages and help and returns their exit codes.

### Browser transports

Writing a T2/T3 backend, and the `HttpClient`, `BrowserSession` and `Options` internals, are in
[engines-and-backends.md](engines-and-backends.md); a site module needs none of it. A built-in
site's page script (D-95) is a static `.js` file in the site's package holding one function,
`async (arg) => data`, that reads what only the page can produce and returns it raw. It may click
to reveal what it reads (a "View more" button); it never submits, signs in or changes anything
at the site. The action runs it with `ctx.page_script(url, script, arg)`, which opens a background tab on `url` (a page
of the site's own origin), runs the function, closes the tab and returns the data. Parsing
stays in Python, tested on a fixture of what the script returned (`gflights/booking.js` for
`fares`, `gflights/more.js` for `search --all`). A custom site's page script doesn't run: one that needs it (a signed request, a
token only the page holds) stays a Markdown site.

**Browser sessions.** T3, and T2 sites that borrow browser cookies, drive Chrome through the
patchright-cli daemon (started on demand). Nothing starts until the first such request, so T1 sites
and T2 sites with `browser_cookies: False` never launch a browser.

| `--browser` | patchright-cli session | Browser |
|---|---|---|
| `managed` (default) | `esum` | persistent profile in `~/.esum/browser/profile`, in its own window |
| `attach` | `esum-attach-<host>-<port>` | the Chrome at `--cdp=<url>` (default `http://127.0.0.1:9222`), its own cookies (host context) |
| `native` | `esum-native` | the user's everyday Chrome, found through `DevToolsActivePort` once they turn on `chrome://inspect/#remote-debugging`; host context; detached, never closed |

Each call works in its own named tab (patchright-cli ≥ 0.11, checked at start) and closes it at the
end; parallel calls on a session never touch each other's pages, and their requests overlap. T3 opens one tab per origin. The tab first loads the
site's `base_url` (or `extras["browser_home"]`), and ESUM waits up to 20 s for an anti-bot
interstitial to clear. When an action falls short, the agent can drive the same session with
plain patchright-cli commands, such as `patchright-cli -s=esum snapshot` (never in native mode,
where the session is the user's whole browser).

**Site extras the browser transports read:**

- `extras["login_check"]` declares what a logged-out response looks like. T2 and T3 run it on
  every response (implemented in `src/esum/browser/login.py`). It is either a dict, where any one
  match counts:

  ```python
  extras={"login_check": {
      "status": [401, 403],                    # response statuses
      "url": [r"/login\b", r"/session/new"],   # regexes searched in the final URL (after redirects)
      "body": [r'<form[^>]+id="loginform"'],   # regexes searched in the decoded body
  }}
  ```

  or a callable `check(response) -> str | bool | None` that returns a truthy value when the
  response is logged out (a string becomes the detail). On a match the backend raises
  `LoginNeeded`, the fixed `LOGIN NEEDED` message, naming the site and the browser window to log
  in to (exit 3). The user logs in there themselves and the agent retries. The agent and ESUM
  never type or handle credentials. Keep checks narrow. A body pattern that also appears on
  logged-in pages (such as a "Log in" link) would stop every read, so prefer statuses and
  login-page URLs. Without a `login_check`, an action can still `raise ctx.login_needed()` after
  parsing a response.
- A site whose `login` saves a key the user pastes asks for it with `ctx.ask_secret(prompt)`: hidden
  input in the user's own terminal, a refusal without one (an agent's shell can't supply it), or the
  key the user typed into `esum keys ui`. The action never learns which. Validate it without quoting
  it, and keep it with `esum.core.keystore` (`load`, `save`, `delete` by site name; never a path of
  its own). `extras["key_entry"]` (the prompt, e.g. "API key from example.com/settings") lists the
  site in `esum keys ui` (D-79). Other interactive actions start with `ctx.require_terminal()`.
- A browser check that outlasts a T3 tab's wait is `CHECK NEEDED` (exit 5), raised by the browser
  layer, never by a module: the host agent ticks it in ESUM's tab and reruns (D-54). A module
  doesn't detect or solve checks.
- `limit`, `offset` and `page` are checked before an action runs, so don't clamp them: `limit` is at least
  1, `offset` at least 0, and `page` starts at its default of 0 or 1. Write `(max N)` in `limit`'s
  `Args:` line if the site has a ceiling; that text is the bound, and a value past it is a usage error.
- `extras["challenge_text"]` is words only the site's own bot check has, for a check served with
  HTTP 200 or a status ESUM doesn't treat as a check. With a `fallback`, such a response moves the
  call to it, as a Cloudflare challenge (`cf-mitigated`) or an AWS WAF one (`x-amzn-waf-action:
  challenge`) does. A new T3 tab of such a site asks, by one request from the tab every 3 s,
  whether a request gets a page with neither this text, a check's title nor a challenge header,
  and stops waiting when it does, whatever the tab's own title says (AWS WAF's check page has no
  telling title). Each ask fetches the tab's home page.
- `extras["browser_home"]` is a URL or a path on the site that a new T3 tab loads before
  fetching. The default is `base_url`. Set it when the base URL is a poor landing page (a
  redirect or a very heavy page), or when the bot check spares it, so the tab would never solve
  the check (FlightConnections' `/`, D-91).

## 8. Tiers and writes

| Tier | Meaning | v1 |
|---|---|---|
| `Tier.READ` (0) | Read | yes |
| `Tier.REVERSIBLE` (1) | Reversible account change (bookmark, enroll) | the Discourse bookmark |
| `Tier.PUBLIC` (2) | Public or hard to undo (reply, message) | not in v1 |
| — (3) | Moves money or points | **never**: there is no `Tier` member for it, and `tier=3` raises |

A write action (tier ≥ 1) **does not write**. It returns the planned writes, and ESUM performs them
when the agent runs the command, which it does only after asking the user (D-85):

```python
@action(forum, tier=Tier.REVERSIBLE)
def bookmark(ctx, ids: list[int]):
    """Bookmark topics (reversible: remove the bookmark on the site)."""
    return [Write(f"Bookmark topic #{i} “{title_of(ctx, i)}” on {ctx.site.base_url}",
                  lambda i=i: ctx.http.request("PUT", f"/t/{i}/bookmark.json").status)
            for i in ids]
```

- ESUM shows no dialog: the agent asks the user before it runs the `esum <site> <action>`
  command (D-85). Each `Write.description` appears in the output and the write
  log, so write each one to say exactly what changes, where, and for which account.
- The `run` callables run in order. If one raises, the rest are skipped, and the error passes
  through `WriteInterrupted`, with its original cause and completed items.
- The planned items and every item's outcome are appended to `~/.esum/writes.jsonl`.
- Reads that prepare the plan (for example fetching titles) are fine, but **no write may happen
  before the `Write` list is returned.** A tier-0 action that returns writes is refused.
- Don't add a flag, plan ID or config that writes on ESUM's own say, and don't gate on HTTP method
  (many reads are POST). Custom code can't affect a built-in site's writes, because it isn't loaded
  for them. When a custom site is invoked, its code is trusted as the user's own, and the write
  log's `custom_code` field records that it was loaded.

## 9. Fixtures and tests

- Save real responses to the site's own `tests/fixtures/` and test the parsers offline in
  `tests/test_<site>.py` beside them (built-in: `src/esum/sites/<site>/tests/`).
  The registry never imports a folder named `tests`, and the wheel leaves those folders out. These
  tests always run. For a built-in site, also add a row
  to `live/test_sites.py` (a listing, its thread, a search) and run `uv run pytest live -k <site>`.
- **Who may depend on whom, in the ESUM checkout.** Deleting any one built-in site's folder must
  leave the offline suite with no failure and no collection error:
  - A test in the checkout's `tests/` never imports a built-in site's module, looks one up in the
    registry by name, or reads a file under `src/esum/sites/`. Core tests run on the example world
    in the root `conftest.py` (`example_registry`, `guarded`). A case about one site's own pages or
    quirks goes in that site's `tests/`.
  - An engine's tests own their fixtures, `tests/fixtures/<engine>/`, and run on synthetic site
    configs (`engine_sites` in `tests/support/`), never on a built-in site. A page an engine's test
    reads is the engine's fixture even when it was captured from a built-in site.
  - A site config's tests may read its engine's fixtures and import its engine's test helpers
    (`from support import engine_fixtures, find_fixture`; `from support.discourse import …`): a
    site needs its engine anyway. `tests/support/` is on pytest's path for both. A site's folder
    keeps only the fixtures its own tests alone read.
  - A test that asserts a step on another site (D-93) runs only where that site is installed:
    mark it `@needs_site("matrix")`, or call `require_site("matrix")` before the last assertions
    when only those are about the other site (both from `support`). It is then skipped, naming the
    missing site.
  - A test generated from the registry (every built-in site's paging flags, every site doc) is a
    contract each installed site meets. Name it for what it asserts, and make it fail when the
    registry yields no case.
- **Scrub every fixture before saving it.** Remove cookies, `Set-Cookie`, auth and CSRF headers,
  tokens, API keys, session ids, e-mail addresses, and account ids or usernames tied to the user's
  own logged-in session. Replace them with obvious placeholders (`"user_id": 0`,
  `"csrf": "SCRUBBED"`). Then review the file once more. Nothing secret may land in a module, a
  doc or a fixture.
- Keep fixtures small. Trim to the posts a test needs; don't save whole 500-post threads. Keep
  whatever the parser anchors on or outputs, and the whitespace inside `<pre>` and `<textarea>`;
  the kept items must parse the same after trimming. In a URL carrying the user's account id,
  name or a token, scrub only that part, keeping its host and shape (`https://forum.example/u/0`).
- **Expected values come from ground truth** (discovery.md §2): read off the rendered page when
  the fixture is captured, never copied from the parser's own output, which would pass whatever
  the parser gets wrong. Assert them for the ground-truth item of each output kind, compared as
  discovery.md §2 says: text fields hold the whole shown text and nothing else (so a neighbouring author name, button label or date glued on fails), counts at
  their shown value (so a fallback `0` or `None` from a missed selector fails), and dates equal to
  the one the field names (posted, not edited or last reply). Change an expected value only when
  new ground truth shows the site itself changed; when a test fails otherwise, fix the parser.
- Keep parsing in pure functions (`parse.py`: JSON/HTML in, `Post`s out) so tests don't need
  `ctx`. In the ESUM checkout, test whole actions with `action_runner` from the root `conftest.py`:
  populate `fake_http.routes` with `Response` / `json_response`, then call
  `action_runner(site, site.all_actions["latest"], {}, Options(), stream=output)`.
  This runs the declared T1/T2/T3 transport, fallback and `ctx.http_for` through the real client.
  For a custom response backend set `action_runner.backend`; for transport-specific responses set
  `action_runner.backend_factory = lambda site, options: backends[site.transport]`.
  Use `action_runner.cli(["mysite", "latest"], registry=registry)` when checking CLI outcomes;
  it returns the exit code, and `capsys` captures output. See templates.md for a complete example.
  Do not patch transport modules or their dispatch table. The checkout's offline tests reject outbound
  network connections while allowing loopback test servers; this guard does not apply to `live/`.

## 10. Docs

A module site's doc is `doc.md` in the site's folder (`src/esum/sites/<site>/doc.md` for a built-in,
`~/.esum/sites/<site>/doc.md` beside a custom `site.py`). It is generated from the site's metadata
and from its actions' signatures and docstrings. Only the `## Quirks` section is hand-written, and it is kept
when the doc is regenerated.

```
uv run esum docs <site> --write      # one site
uv run esum docs --all --write       # every built-in site
uv run esum docs <site>              # print it
```

`tests/test_docs_in_sync.py` fails when a built-in site's committed doc differs from what the code
generates, or when a generated doc's site no longer exists. Regenerate after every change to
actions, arguments, docstrings or labels. A Markdown site's `SITE.md` is its doc. The wheel ships
each built-in `doc.md` beside its module, so `esum docs <site>` prints the quirks in an install too.

## 11. Provenance

When site knowledge comes from xgoose, nitan-mcp or the nitan_plugin userscript, port it into fresh
Python. Never copy the JavaScript. Record the source at module level:

```python
PORTED_FROM = "xgoose skills/discourse-forum.source.xgs.js (github.com/xGoose-dev/xgoose)"
```

The generated doc shows it. The xgoose and nitan-mcp MIT notices are in `THIRD_PARTY_NOTICES.md`.
