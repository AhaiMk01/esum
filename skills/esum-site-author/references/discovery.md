# Discovering a site

How to learn enough about a site to write its ESUM bundle. Run the probe first, then explore in the
browser with patchright-cli to fill the gaps. Everything here is read-only.

## 1. Probe

```
uv run <skill>/scripts/probe.py https://site.example
uv run <skill>/scripts/probe.py https://site.example --page <list URL> --page <thread URL> --save <dir>
```

`<skill>` is this skill's folder. The probe makes a few plain GETs, one second apart, with no
cookies, and reports:

- **transport:** whether T1 answers with ESUM's own request, whether T1 answers only with a browser
  user agent (then set `headers={"User-Agent": …}` or use T2), whether T2 (Chrome's TLS
  fingerprint) answers, and whether an answer is a challenge page, a login wall or empty. The
  verdict line names the `Transport` to try first.
- **charset:** from the header or `<meta>`. Set `charset=` on the site when it isn't UTF-8 and the
  header is missing or wrong. ESUM, like the probe, decodes ISO-8859-1, Latin-1 and ASCII as
  windows-1252.
- **platform:** generator meta or structural markers, confirmed with one JSON endpoint where the
  platform has one, resolved against the page's final URL (after redirects, sub-path kept). A
  platform with an ESUM engine (discourse, wordpress, vbulletin, discuz) means you write a site
  config, not a module.
- **data sources:** RSS/Atom feeds, embedded page state (`__NEXT_DATA__`, `__NUXT__`, JSON-LD),
  `/api/…` paths the HTML mentions, sitemaps and `robots.txt` disallows.

`--page` fetches more URLs over both transports, so you can check a JSON endpoint or a thread page
that you found while exploring. Pass full URLs, because Git Bash rewrites a bare `/path`. `--save`
writes each page a transport got cleanly (never a challenge or login page) to a folder, named after
its path, query and a digest of the URL, as a raw fixture you must scrub before you keep it.

The probe has no cookies and no browser, so it can't check a source that needs the user's login or
a page that ran JavaScript. Check and capture those in the browser (sections 2 and 3).

Platforms without an ESUM engine sometimes have a public API that makes a standalone module
easy to write: Flarum `/api/discussions`, NodeBB `/api/…` (the same path as the page),
Lemmy `/api/v3/…`, MediaWiki `/api.php`, Substack `/api/v1/…`, and many Rails or Lobsters-style
sites answer `<page>.json`.

## 2. Explore with patchright-cli

ESUM needs patchright-cli 0.12 or newer. Check with `patchright-cli --version`, and upgrade with
`uv tool install -U patchright-cli`, then `patchright-cli kill-all` so a running daemon restarts
on it. If `--version` still shows an older version, another copy
comes first on the PATH (`where patchright-cli` on Windows, `which -a patchright-cli` elsewhere,
often a `pip install --user` one): remove it with the tool that installed it.

**Guest exploration: a session and profile of its own.** patchright-cli keeps every profile, named
after its session by default, so a reused name carries old cookies and logins. Use a session named
for the site, start from an empty profile, and delete it when you're done:

```
patchright-cli -s=explore-<site> delete-data      # start empty (harmless if it never existed)
patchright-cli -s=explore-<site> open https://site.example
… explore …
patchright-cli -s=explore-<site> close
patchright-cli -s=explore-<site> delete-data
```

**Logged-in exploration: ESUM's managed session, in a tab of your own.** Use it only when the
content needs a login. The login has to land in ESUM's profile, so the session must be the one ESUM
opens, with `<ESUM_HOME>/browser/profile`:

1. `patchright-cli list`. If a session `esum` is running, use it as it is.
2. If not, open it the way ESUM does:
   `patchright-cli -s=esum open --profile=<ESUM_HOME>/browser/profile` (an absolute path; a value
   with a slash is a directory).
3. Work only in a named tab: add `--tab=explore` to every command. Ask the user to log in themselves
   in that window, and wait until they say they're done.
4. At the end, close only your tab: `patchright-cli -s=esum --tab=explore close`. Never close the
   `esum` session or tabs you didn't open, and never use `--browser=native` or the user's everyday
   Chrome for exploration.

patchright-cli writes snapshots into `.patchright-cli/` under the current folder, so run it from a
scratch folder, not from a repo.

**Ground truth** is what the site shows a person through its own interface: the rendered page, or,
when the page hides the source from the action's user (a value only its browser extension or a
logged-in view shows), the interface that does show it, noted as such. Read it in the login state
the action uses: logged in (ESUM's session, in your tab) when the action sends any credential,
browser cookies or a stored key, and the guest session otherwise. When no URL takes the action's
input (a POST search, a search token), enter the same input in the site's own form. Read values
with `snapshot` or an `eval` on the visible element, never from the response the parser reads,
so a parser that takes the wrong field disagrees with it. Compare meaning, not characters: the
same instant across time zones, ESUM's Markdown for the same post text, a computed field against
the shown values it comes from, and `--json` for a value a table cell cut short.

The loop for each kind of page (home or board list, a thread, page 2 of a long thread, a search,
the board/category catalog). `<s>` is `-s=explore-<site>`, or `-s=esum --tab=explore`:

```
patchright-cli <s> network --clear
patchright-cli <s> goto <page URL>
patchright-cli <s> network               # the calls the page made: #<id> METHOD STATUS URL
patchright-cli <s> request <id> --body   # one call in full: headers, POST body, response body
patchright-cli <s> snapshot -i           # page structure, with refs
patchright-cli <s> eval "<expression>"   # read state the page already holds
```

Useful `eval` expressions:

```
Object.keys(window).filter(k => /^__|state|data|config/i.test(k))
JSON.stringify(window.__NEXT_DATA__?.props?.pageProps).slice(0, 3000)
[...document.querySelectorAll('script[type="application/ld+json"]')].map(s => s.textContent.slice(0, 500))
document.querySelector('<post container selector>')?.outerHTML.slice(0, 2000)
fetch('/api/<endpoint>?page=2').then(r => r.status + ' ' + r.headers.get('content-type'))
```

The last one is a same-origin GET run from the page, with the page's cookies. It confirms a source
for T3, or for T2 with the browser's cookies. Never POST from `eval`.

**Diff to find the source and its parameters.** When several calls fire, change one thing at a
time. `network --clear` drops earlier call ids, so read bodies (`request <id> --body`) before
each clear:

1. Load the page: note which call's body holds the rows you see. That call is the source; one
   whose body never holds them (analytics, polling) is noise.
2. Do one revealing step (next page, another board, another sort), then again with another value
   (page 3): the URL or POST-body parameter that tracks your input is the one you want. If no
   call changes, the page sorts or filters what it loaded in step 1.

If no call or page state holds the data, save a script the page loads with `probe.py --page
<script URL> --save <dir>` and search it for `api`, `graphql` or `ajax`.

**Rules while exploring:**

- **Observe only.** Navigate, scroll, read, and open search result pages through their URL. Don't
  submit forms that change anything, and don't click like, reply, follow, vote, buy or check-in
  buttons. A click that only reveals content (expand, "load more", next page) is fine. Anything
  else needs the user's approval first.
- **Never handle credentials.** If a login wall appears, the user logs in themselves in the
  session window. Don't type, read or store passwords, codes, cookies or tokens.
- **Be polite.** Read a few pages per kind, not the whole site. ESUM later spaces its own requests
  0.5 s apart; set `min_interval` higher if the site is small or touchy.

## 3. Capturing a fixture in the browser

For a source the probe can't fetch, save the body the page itself gets:

```
patchright-cli <s> --raw eval "fetch('<source URL>').then(r => r.text())" | python -c "import json,sys; open(sys.argv[1], 'w', encoding='utf-8').write(json.load(sys.stdin))" <dir>/<name>.json
```

`eval` prints its result as a JSON string, so the pipe decodes it back to the body.

Check that the file holds the data and not a login or challenge page, then scrub it like any other
fixture: the logged-in user's name, ids, e-mail and any token in the body become placeholders.
Cookies are never part of it, so never export them.

## 4. What to write down

For each page kind, find the **source** (a JSON endpoint, embedded state or HTML) and, from that
source:

| Need | Look for |
|---|---|
| Ids | the stable id in each URL (`/t/<slug>/<id>`, `?tid=`, `/item/<id>`); actions take ids, not URLs |
| List rows | id, title, author, date, reply count, score; the total count if the source gives one |
| Paging | page number, offset or cursor param; the page size; how the last page shows |
| Thread posts | author, date (and its time zone), body HTML, likes, whom a reply answers, the post number or floor |
| Long threads | how to fetch post N without fetching 1…N−1 (page = N // per_page) |
| Search | the endpoint and params (keyword, board, sort, date); guests may be refused |
| Logged out | what a login wall looks like: redirect URL, status, a phrase in the body. This becomes `login_check` |
| Ground truth | per output kind (list row, post, detail), one item: its page URL (or the form input that shows it) and the value the page shows for every field the action will print, dates as shown with their time zone |
| Fragility | hashed CSS classes (avoid them), signed params (`sign`, `x-s`, `w_rid`, `_signature`), tokens that rotate per request, an anti-bot page a real browser also gets |

**Pick the source in this order:** a public JSON API, then JSON embedded in the HTML, then HTML
parsing. For HTML, anchor on ids, `data-*` attributes and URL patterns, not on layout classes.

**Decide the transport** from the probe and the in-page `fetch` checks:

| Observation | Transport |
|---|---|
| ESUM's plain request gets the data | `Transport.DIRECT` (T1) |
| Only a browser user agent gets it over plain HTTP | T1 with `headers={"User-Agent": …}`, or T2 |
| Plain HTTP gets 403 or a challenge, Chrome's fingerprint gets the data | `Transport.BROWSER_HTTP` (T2), `extras={"browser_cookies": False}` |
| Data needs the user's login, or guests see less | T2 with browser cookies (the default) plus `login_check` |
| Only a page that ran JavaScript gets the data (Cloudflare JS challenge) | `Transport.IN_PAGE` (T3), or T2 with `fallback=Transport.IN_PAGE` if T2 usually passes |
| Requests are signed, tokens rotate, or the data appears only after UI clicks | Markdown site (`SITE.md`) |

Never send a User API Key into a page, including an exploratory fetch.
