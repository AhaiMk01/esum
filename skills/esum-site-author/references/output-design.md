# Output design

What an action returns decides how many calls and tokens an agent spends answering a question.
These rules are AXI's (the Agent eXperience Interface, D-43), rewritten for a site module: what
core already does, and what you decide. Read this before step 5 and check against it in step 9.

**Core does these; don't redo them.** A `Listing` prints as a TOON table with its count line;
`--fields` adds columns; an empty list prints its reason and exits 0; errors print as `error:` and
`help:` with the right exit code; `limit`, `offset`, `page` and unknown flags are checked before
your action runs; output over 20,000 characters (the default) spills to a file, and stdout gets
its path plus a summary, with a post index when the text is a thread; `Next` steps become
complete, checked commands; `esum`, `--help` and `--version` are generated.

**You decide these.**

## 1. Default columns

`fields` is the fewest columns that let an agent choose its next call: usually the id the next
action accepts, a title, and one or two numbers or states. Three or four columns, not ten.

- A field the recorded questions filter, sort or decide on (pinned, closed, expired, frontpage,
  score, price) is a default column, not one behind `--fields`.
- `rows` still hold every field the action knows, so `--fields` and `--json` can show them.
- Long text belongs in the detail or thread action. A list row gets at most a one-line excerpt
  (`html_to_label`, `clip`).

## 2. Default limit

Set a list's default `--limit` high enough that the common question is answered in one call. If
most boards show 30 rows a page and questions are about "this week", a default of 10 costs two
more calls. Write the ceiling as `(max N)` in the flag's `Args:` line.

## 3. Totals, counts and derived fields

The dearest cost is a follow-up call, or an agent counting by its own script.

- Pass `total` whenever the site gives one, so "how many are there" needs no paging.
- When a recorded question asks for a count, a group, a cheapest or a largest, and the data you
  already fetched holds the answer, compute it and return it. A list puts it in `Listing.about`
  (ITA Matrix's `by_carrier` and `by_stops`). A thread has no `about`: pass it in `notes` to
  `ThreadView.result(...)`, so it prints under the title and in a spill summary (Substack's
  largest sub-threads), and in `data` for `--json`.
- Put the state an agent would otherwise open each row to check into the row: reply count,
  last-reply date, closed, expired.
- Only what is cheap: derived from responses the action already has. If it costs a request per
  row, make it an opt-in flag and say so in its help.
- A value the site hides or doesn't give is `None`, never 0 or an empty string.

## 4. Empty results

An empty list is an answer. Return the `Listing` with no rows and `empty` saying what was searched
and why nothing matched ("0 topics in board 12 since 2026-11-01"), so the agent doesn't rerun
with other flags to make sure. When a wider call is the likely next move, add it as
`Next(..., when_empty=True)`.

## 5. Next steps

`next` lists a few commands that follow from this output. An agent learns the site from them.

- Relevant to what was printed: after a list, reading one row; after a thread with gaps, the
  range that fills them (`view.range_steps`).
- A list that shows only part of what exists must say how to get the rest: set `more` to the
  arguments of the next page (`{"page": 2}`) whenever there is one. A `total` alone prints no
  next-page command.
- Complete commands. Use a placeholder for a value the agent must pick (`{"id": "<id>"}`), never a
  guessed real value. Core repeats the call's own flags on a next-page step and on a step naming
  the same action; a step naming another action gets only what you put in its arguments, so pass
  the values it needs to find the same thing (the board, the publication, the URL).
- Several directions, not one fixed path: an agent that knows what it wants shouldn't be pushed
  through an extra call.
- None when the output already answers the question (a detail view, a count).
- A hand-off to another built-in site is `Next(..., site="other")`; it prints only where that
  site is installed, so the result must stand without it.
- For bad input, raise `ArgumentError(message, help=...)` whose help is the command that fixes it,
  not "see --help".

## 6. Cut text

Never drop a large field without saying so. Cut it, keep the start, and say where the whole is:
`clip(text, cap, hint="; whole in #3")`, a thread's gap markers, a spill index. A reader must be
able to tell "short" from "cut".

- Pick a cap that covers the common use, so most values aren't cut at all.
- Say how much there is when the site tells you. `clip` prints the cap, not the original size, so
  a detail view that cuts a body gives the full size in a field of its own (characters, replies,
  pages).

## Where ESUM differs from AXI

- **Errors keep the site's or the network's raw message** (D-52). AXI says to translate errors and
  hide the dependency; here the status, URL and body are what diagnose a failure. Add a `help`,
  don't rewrite the message.
- **Large output spills to a file** and stdout names it (`--no-spill` prints it all, and
  `ESUM_SPILL_CHARS` moves the threshold); there is no `--full` flag.
- **No session hooks** (AXI §7). The `esum` skill is the integration.
