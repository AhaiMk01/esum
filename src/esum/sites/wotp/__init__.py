"""Wings of the Points (uscreditcardguide.github.io/Wings-of-the-Points): which US card points
(Chase UR, AmEx MR, Citi TYP, Capital One, Bilt, Marriott Bonvoy) transfer to which airline
programs, and at what ratio. Read-only and public.

The page draws a chart from data its build step copies out of `_data/transfers.csv` in the
chart's GitHub repository, whose README says to edit that file to change a ratio. ESUM reads the
CSV and the README (notes and change log) from the repository's `gh-pages` branch on
raw.githubusercontent.com: the same data the page shows, without parsing the page's script (D-104).
"""

from __future__ import annotations

from typing import Literal

from esum import HttpError, Listing, Next, NoSuchItem, Result, Tier, Transport, action, command_line, parse_since, site

from .parse import (ALLIANCES, Chart, best_of, change_patterns, closest, day_of, label, match_airlines,
                    parse_changes, parse_chart, program_rows, reach, resolve_program, section, shared_programs,
                    spread, updated)

FORMAT = 1
EXTRAS = ()

RAW = "https://raw.githubusercontent.com/USCreditCardGuide/Wings-of-the-Points/gh-pages"
CSV = f"{RAW}/_data/transfers.csv"
README = f"{RAW}/README.md"

wotp = site(
    name="wotp",
    base_url="https://uscreditcardguide.github.io/Wings-of-the-Points/",
    transport=Transport.DIRECT,
    label={"en": "Wings of the Points: which US card points (Chase UR, AmEx MR, Citi TYP, Capital One, Bilt, "
                 "Marriott) transfer to which airline programs and at what ratio, by airline, currency or "
                 "alliance, with the chart's change log",
           "zh": "Wings of the Points：美国信用卡积分（Chase UR、AmEx MR、Citi TYP、Capital One、Bilt、Marriott）"
                 "能转到哪些航司里程计划、比例多少，可按航司、积分或联盟查，并有转点变更记录"},
)

Alliance = Literal["star", "oneworld", "skyteam", "none"]


def _chart(ctx) -> Chart:
    return parse_chart(ctx.http.get_text(CSV))


def _extras(ctx) -> tuple[list[dict], str | None]:
    """What the README adds to a chart answer: the airlines that share another's program and the
    date of the newest change. The chart is still answered when the README can't be read."""
    try:
        readme = ctx.http.get_text(README)
    except HttpError as e:
        return [], f"unknown (the README answered HTTP {e.status})"
    try:
        newest = updated(parse_changes(readme))
    except RuntimeError:
        newest = None
    return shared_programs(readme), newest or "unknown (the README's change log has no dates)"


def _program(ctx, chart: Chart, term: str) -> str:
    found = resolve_program(term, chart.programs)
    if found is None:
        known = ", ".join(f"{p} ({label(p)})" for p in chart.programs)
        missing = NoSuchItem(f"the chart has no points currency {term!r}; it has {known}")
        missing.help = command_line(ctx.site, ctx.site.all_actions["programs"], {})
        raise missing
    return found


def _window(rows: list[dict], offset: int, limit: int) -> tuple[list[dict], dict | None]:
    return rows[offset:offset + limit], {"offset": offset + limit} if offset + limit < len(rows) else None


def _past_end(rows: list[dict], offset: int) -> str | None:
    """Why a page is empty when rows did match: the offset is past the last of them."""
    return f"no row at offset {offset}: {len(rows)} matched" if rows and offset >= len(rows) else None


def _wider(airline: list[str] | None, filtered: bool) -> list[Next]:
    """Where to look after `airlines` found nothing: the same airlines without the other filters,
    the same filters without the airlines, then the whole chart."""
    reset = {"points": None, "match": "any", "alliance": None, "offset": 0}
    steps = []
    if airline and filtered:
        steps += [Next("airlines", reset, "see every currency's ratio for those airlines", when_empty=True),
                  Next("airlines", {"airline": None, "offset": 0}, "drop the airline filter", when_empty=True)]
    if airline:
        steps.append(Next("notes", {}, "see which airlines are listed under another airline's program",
                          when_empty=True))
    return [*steps, Next("airlines", {**reset, "airline": None}, "list the whole chart", when_empty=True)]


@action(wotp, tier=Tier.READ)
def airlines(ctx, airline: list[str] | None = None, points: list[str] | None = None,
             match: Literal["any", "all", "none"] = "any", alliance: Alliance | None = None,
             offset: int = 0, limit: int = 100):
    """Airline programs and the ratio each points currency transfers to them at: the whole chart, or the rows you filter to.

    One row per airline program, in the chart's order (grouped by alliance). Columns: code (the
    airline's IATA code), name (airline and its program), alliance, then one column per currency
    holding the ratio `points:miles` (`1:0.8` is 1,000 points for 800 miles; `null` when that
    currency doesn't transfer there), routes (how many of the chart's currencies reach the
    program; 0 is a program no points reach, the chart's N/A) and best (the ratio giving the
    most miles per point and the currencies that have it; it compares ratios only, and points of
    different currencies aren't worth the same). With `--points`, only those currencies' columns
    show and best is the best among them, while routes still counts every currency; the header
    then sums up each named currency's ratios over the rows found (the commonest ratio with its
    count, every other with its airlines). `--fields all` shows the other currencies' columns.

    Citi's ratios assume a premium ThankYou card, and Marriott's include the bonus for transfers in
    blocks of 60,000 points: `notes` has the conditions. An airline that uses another airline's
    program has no row of its own (KLM is under Air France's Flying Blue); searching for it shows
    that row and says so.

    Args:
        airline: Airline codes (`NH`, `UA`) or words from an airline or program name (`aeroplan`, `avios`, `"flying blue"`); a row matching any of them is shown.
        points: Points currencies you hold or ask about: `chase`, `amex`, `citi`, `c1`, `bilt`, `marriott` (also `UR`, `MR`, `TYP`, `"capital one"`, `bonvoy`).
        match: With `--points`: programs that `any` of them reach, that `all` of them reach (so balances can be combined there), or that `none` of them reach. Without `--points`, `all` and `none` count every currency: `--match none` lists the programs no points reach.
        alliance: Only this alliance's airlines; `none` is the airlines outside the three alliances.
        offset: Rows to skip.
        limit: Rows to show.
    """
    chart = _chart(ctx)
    shared, newest = _extras(ctx)
    named = list(dict.fromkeys(_program(ctx, chart, p) for p in points or []))
    wanted = {p for p in chart.programs if p in named} or set(chart.programs)
    rows = chart.rows
    about: dict = {}

    unmatched: list[str] = []
    if airline:
        codes, notes, unmatched = match_airlines(chart, airline, shared)
        rows = [r for r in rows if r["code"] in codes]
        about["airline"] = ", ".join(airline)
        if notes:
            about["shared"] = "; ".join(notes)
        if unmatched:
            about["unmatched"] = "; ".join(t + (f" (closest on the chart: {', '.join(near)})"
                                                if (near := closest(chart, t)) else "") for t in unmatched)
    if named or match != "any":
        keep = {"any": lambda got: bool(got & wanted), "all": lambda got: wanted <= got,
                "none": lambda got: not got & wanted}[match]
        rows = [r for r in rows if keep(reach(r, chart.programs))]
        about["points"] = f"{match} of {', '.join(p for p in chart.programs if p in wanted)}"
    if alliance:
        rows = [r for r in rows if r["alliance"] == alliance]
        about["alliance"] = alliance
    if len(rows) > 1:
        about["by_alliance"] = ", ".join(f"{a} {n}" for a in ALLIANCES
                                         if (n := sum(1 for r in rows if r["alliance"] == a)))
    columns = [p for p in chart.programs if p in named] if named and match != "none" else list(chart.programs)
    if named and match != "none":
        rows = [{**r, "best": best_of(r, columns)} for r in rows]
        if len(rows) > 1:
            about["ratios"] = " | ".join(f"{p}: {spread(rows, p) or 'none of these'}" for p in columns)
    elif named and (orphans := [r["code"] for r in rows if not r["routes"]]):
        about["unreachable"] = f"{len(orphans)} of these no currency reaches ({' '.join(orphans)})"
    about["updated"] = newest
    shown, more = _window(rows, offset, limit)
    past = _past_end(rows, offset)
    empty = past or "no airline program on the chart matches " + "; ".join(
        f"{k} {v}" for k, v in about.items() if k in ("airline", "points", "alliance"))
    if unmatched and not past:
        empty += (". The chart has one row per frequent flyer program: try the airline's IATA code or a word "
                  "from its program's name")
    wider = [Next("airlines", {"offset": 0}, "start from the first row", when_empty=True)] if past else \
        _wider(airline, bool(named or match != "any" or alliance)) if not rows else []
    return Listing(name="airlines", rows=shown, fields=("code", "name", "alliance", *columns, "routes", "best"),
                   about=about, total=len(rows), more=more, empty=empty,
                   next=[Next("changes", {"query": ["<code>"]}, "see what changed for an airline or a currency"),
                         Next("notes", {}, "read the conditions behind Citi's and Marriott's ratios"),
                         Next("programs", {}, "compare the currencies: partner counts, ratios, sole partners"),
                         *wider])


@action(wotp, tier=Tier.READ)
def programs(ctx):
    """The points currencies on the chart, compared: how many airline programs each reaches, by alliance, at which ratios, and which it alone reaches.

    One row per currency. Columns: key (what `airlines --points` takes), name, kind (bank, or
    hotel for Marriott, whose points are worth less each: don't rank it against the banks by
    partner count or ratio), partners (airline programs it transfers to), then how many of those
    are in star, oneworld, skyteam and none (outside the alliances), ratios (how its ratios
    spread: the commonest with its count, every other with the airlines that have it) and sole
    (programs no other bank currency reaches; the hotel currency Marriott reaches most programs
    at about 3:1, so it isn't counted against the banks, and its own sole are the programs no
    bank currency reaches). The header counts the airline programs on the chart and names the
    ones no currency reaches.
    """
    chart = _chart(ctx)
    _, newest = _extras(ctx)
    orphans = [r["code"] for r in chart.rows if not reach(r, chart.programs)]
    return Listing(name="programs", rows=program_rows(chart),
                   fields=("key", "name", "kind", "partners", *ALLIANCES, "ratios", "sole"),
                   about={"airlines": len(chart.rows),
                          "unreachable": f"{len(orphans)} ({' '.join(orphans)})" if orphans else 0,
                          "updated": newest},
                   total=len(chart.programs),
                   next=[Next("airlines", {"points": ["<key>"]}, "list the programs a currency reaches, with ratios"),
                         Next("airlines", {"points": ["<key>", "<key>"], "match": "all"},
                              "list the programs two currencies both reach"),
                         Next("airlines", {"match": "none"}, "list the programs no currency reaches"),
                         Next("notes", {}, "read the conditions behind Citi's and Marriott's ratios")])


@action(wotp, tier=Tier.READ)
def changes(ctx, query: list[str] | None = None, since: str | None = None, older: bool = True,
            offset: int = 0, limit: int = 30):
    """The chart's change log, newest first: partners added and dropped, ratios raised and cut.

    One row per logged change: date (the day the chart recorded it, which can be later than the
    day it took effect), change (the maintainer's line) and older (`true` when the log itself
    says the line is an older change written down late: it happened before its date, the log
    doesn't say when, so don't count it as a change of that month). The log is the maintainer's
    notes, not a full history: a partner on the chart may have no line here. It goes back to
    2018 and writes airlines as IATA codes and currencies by short names (UR, MR, TYP, C1).
    `--query` knows both: an airline code is matched as a code or by the airline's name, and a
    currency by its key, name and short names; any other word is searched as written.

    Args:
        query: Airline codes (`EK`), currencies (`chase`, `amex`, `c1`) or any words; a line matching any of them is shown.
        since: Only changes recorded on or after this: a date (`2026-01-01`) or an age (`90d`, `12w`).
        older: `--no-older` leaves out the lines the log marks as older changes written down late.
        offset: Rows to skip.
        limit: Rows to show.
    """
    readme = ctx.http.get_text(README)
    rows = parse_changes(readme)
    about: dict = {"updated": updated(rows), "oldest": min((d for r in rows if (d := day_of(r))), default=None)}
    if query:
        chart = _chart(ctx)
        patterns = [p for term in query for p in change_patterns(term, chart, shared_programs(readme))]
        rows = [r for r in rows if any(p.search(r["change"]) for p in patterns)]
        about["query"] = ", ".join(query)
    cutoff = parse_since(since)
    if cutoff:
        rows = [r for r in rows if (d := day_of(r)) and d >= cutoff.date()]
        about["since"] = cutoff.date().isoformat()
    late = [r for r in rows if r["older"]]
    left_out = f"{len(late)} older change{'' if len(late) == 1 else 's'} left out"
    if not older:
        rows = [r for r in rows if not r["older"]]
        about["older"] = left_out
    elif late:
        about["older"] = (f"{len(late)} of these are older changes written down on "
                          f"{', '.join(sorted({r['date'] for r in late}))}; the log doesn't say when they happened")
    shown, more = _window(rows, offset, limit)
    past = _past_end(rows, offset)
    empty = past or ("no line of the change log" + (f" mentions {about['query']}" if query else "")
                     + (f"{'' if query else ' is dated'} on or after {about['since']}" if cutoff else "")
                     + (f" ({left_out} by --no-older)" if late and not older else ""))
    wider = []
    if past:
        wider = [Next("changes", {"offset": 0}, "start from the first row", when_empty=True)]
    elif not rows:
        if late and not older:
            wider.append(Next("changes", {"older": True, "offset": 0}, "include the older changes", when_empty=True))
        if query and cutoff:
            wider.append(Next("changes", {"since": None, "offset": 0}, "search the whole log, back to 2018",
                              when_empty=True))
        wider.append(Next("changes", {"query": None, "since": None, "older": True, "offset": 0},
                          "read the whole change log", when_empty=True))
    return Listing(name="changes", rows=shown, fields=("date", "change", "older"), about=about, total=len(rows),
                   more=more, empty=empty,
                   next=[Next("airlines", {"airline": ["<code>"]}, "see an airline's ratios as they stand now"),
                         Next("airlines", {"points": ["<key>"]}, "see a currency's partners as they stand now"),
                         *wider])


@action(wotp, tier=Tier.READ)
def notes(ctx):
    """The chart's notes: what the ratios assume and which airlines share a program.

    The README's Note section as written: what the currency names stand for, the card a Citi ratio
    needs, how Marriott's bonus per 60,000 points makes the ratios shown, and the airlines listed
    under another airline because they use its program (KLM under Air France, SWISS under Lufthansa).
    """
    readme = ctx.http.get_text(README)
    text = section(readme, "Note")
    return Result(text=text, data={"notes": text, "shared": shared_programs(readme)},
                  next=[Next("airlines", {"airline": ["<code>"]}, "see an airline program's ratios")])
