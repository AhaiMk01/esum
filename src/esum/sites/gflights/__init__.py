"""Google Flights over its public search page: the result rows and price insight the page carries inline."""

from __future__ import annotations

import re
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from typing import Literal, NamedTuple

from esum import (ArgumentError, HttpError, Listing, Next, NoSuchItem, Result, SiteRefused, Tier, Transport, action,
                  other_site_command, site)
from esum.browser.daemon import BrowserError
from esum.transport import TRANSIENT_ERRORS

from .parse import (CABIN_NAMES, CABINS, Leg, Query, Segment, booking_options, booking_url, flights, flights_name,
                    handle, has_more, history_text, hm, insight, insight_text, money, more_rows, named_flight,
                    named_flights, page_error, page_url, parse_handle, payload, pin, selected)

FORMAT = 1
EXTRAS = ()
PORTED_FROM = ("fli src/fli/search/_tfs.py, _proto.py, _decoders.py (github.com/punitarani/fli, MIT): the tfs "
               "search and booking tokens, the ds:1 page payload and the flight-row positions; the price "
               "insight block read from Google's own page")

# EU and EEA visitors get Google's consent page instead of results; a pre-set consent cookie skips it
# (the value fli sends; not a credential).
SOCS = "CAISNQgQEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjQwMzE3LjA5X3AwGgJlbiADGgYIgLC_rwY"
MAX_DATES = 31
DATE_WORKERS = 4
MORE_SCRIPT = (Path(__file__).parent / "more.js").read_text(encoding="utf-8")
MORE_WAIT = 20         # seconds for the results page to list the rows behind "View more flights"
MORE_READS = 8         # reads of the expanded page at most
MORE_SETTLE = 12       # seconds from a search's first request before a read that adds no row ends the reading
_clock = time.monotonic
# Days after a one-way's departure whose returns page is read for the flights' details, tried in turn:
# Google names the outbound only when it sells a round trip with a return that day (BR7+BR215 SFO-SIN
# on 2026-12-30: none 3 or 7 days later, some 10, 14 and 21 days later).
DETAIL_RETURNS = (7, 14, 21, 10)

gflights = site(
    name="gflights",
    base_url="https://www.google.com/travel/flights",
    transport=Transport.BROWSER_HTTP,
    label={"en": "Google Flights: flights and prices for a date, round-trip returns, Google's price insight, "
                 "cheapest dates",
           "zh": "Google 航班：按日期查航班与价格、往返回程、价格洞察、最低价日期"},
    headers={"Cookie": f"SOCS={SOCS}"},
    extras={"browser_cookies": False},
)

Cabin = Literal["economy", "premium", "business", "first"]
Sort = Literal["price", "best", "duration", "departure", "arrival"]
_CODES = re.compile(r"^[A-Za-z]{3}(,[A-Za-z]{3})*$")
SORTS = {
    "price": lambda r: (r["price"] is None, r["price"] or 0, r["depart"]),
    "duration": lambda r: (r["minutes"] is None, r["minutes"] or 0),
    "departure": lambda r: (len(r["depart"]) < 16, r["depart"]),     # a date without a time: time unknown, last
    "arrival": lambda r: (not r["arrive"], r["arrive"]),
}


def _refused(cls, message: str, help: str):
    error = cls(message)
    error.help = help
    return error


def _find_code() -> str:
    """Where to look an airport code up: Matrix's lookup, when that site is installed."""
    lookup = other_site_command("matrix", "airports", {"query": "<city>"})
    return f"Find a code with `{lookup}`" if lookup else ""


def _codes(value: str, flag: str) -> tuple[str, ...]:
    text = value.replace(" ", "")
    if not _CODES.match(text):
        raise ArgumentError(f"{flag} {value!r}: give 3-letter airport or city codes, comma-separated (JFK, NYC or "
                            f"JFK,EWR)", help=_find_code())
    return tuple(text.upper().split(","))


def _date(value: str, flag: str) -> date:
    text = value.strip()
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError
        day = date.fromisoformat(text)
    except ValueError:
        raise ArgumentError(f"{flag} {value!r}: give a calendar date as YYYY-MM-DD") from None
    if day < date.today():
        raise ArgumentError(f"{flag} {text} is in the past",
                            help=f"Give a date from today ({date.today()}) to about 330 days ahead")
    return day


def _query(origin: str, dest: str, depart: date, back: date | None, *, cabin: str, stops: int | None,
           adults: int, currency: str | None) -> Query:
    if stops is not None and not 0 <= stops <= 2:
        raise ArgumentError(f"--stops {stops}: give 0 (nonstop), 1 or 2; leave it out for any number")
    if not 1 <= adults <= 9:
        raise ArgumentError(f"--adults {adults}: Google Flights takes 1 to 9 passengers")
    cur = currency.strip().upper() if currency else None
    if cur is not None and not re.fullmatch(r"[A-Z]{3}", cur):
        raise ArgumentError(f"--currency {currency!r}: give an ISO currency code (USD, EUR, JPY)")
    o, d = _codes(origin, "--origin"), _codes(dest, "--dest")
    segments = [Segment(o, d, depart.isoformat(), max_stops=stops)]
    if back is not None:
        if back < depart:
            raise ArgumentError(f"--return {back} is before --depart {depart}")
        segments.append(Segment(d, o, back.isoformat(), max_stops=stops))
    return Query(tuple(segments), adults, CABINS[cabin], cur, one_way=back is None)


def _handle(value: str) -> Query:
    try:
        return parse_handle(value)
    except ValueError:
        raise ArgumentError(f"--search {value.strip()!r} is not a search value this site printed",
                            help="Copy the `search` value from an `esum gflights search` or `returns` result") from None


def _fetch(ctx, q: Query):
    """One search page's payload. Google's own refusals become errors that name the input."""
    data = payload(ctx.http.get_text(page_url(q), timeout=60))
    if data is None:
        raise _reason(_refused(SiteRefused, "Google Flights answered without results (no ds:1 data: a consent or "
                               "block page, or Google changed the page)",
                               "Retry in a minute; if it keeps failing, the site needs repair (esum-site-author)"),
                      "no results page")
    code = page_error(data)
    if code == 2:
        raise _reason(ArgumentError("Google Flights: a date in this search is in the past (ErrorResponse 2)",
                                    help=f"Give a date from today ({date.today()}) to about 330 days ahead"),
                      "date in the past")
    if code == 3:
        raise _reason(ArgumentError("Google Flights: a date in this search is too far ahead; Google sells about 330 "
                                    "days out (ErrorResponse 3)",
                                    help=f"Give dates up to about 330 days ahead "
                                         f"(about {date.today() + timedelta(days=330)})"),
                      "date too far ahead")
    if code is not None:
        raise _reason(SiteRefused(f"Google Flights refused the search (ErrorResponse {code})"),
                      f"Google refused it (ErrorResponse {code})")
    return data


def _reason(error: Exception, reason: str) -> Exception:
    error.reason = reason          # the few words a `dates` row shows for a date that failed this way
    return error


def _id(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()


def _unpinned(q: Query) -> Query:
    return replace(q, segments=tuple(replace(s, legs=()) for s in q.segments))


def _search_args(q: Query) -> dict:
    """The `search` flags that run this query again, defaults left out."""
    first = q.segments[0]
    return {"origin": ",".join(first.origins), "dest": ",".join(first.dests), "depart": first.date,
            "return_": q.segments[1].date if q.round_trip else None,
            "cabin": CABIN_NAMES[q.cabin] if q.cabin != 1 else None, "stops": first.max_stops,
            "adults": q.adults if q.adults != 1 else None, "currency": q.currency}


def _relist(q: Query) -> str:
    """The command that lists this search's rows again."""
    if q.pinned():
        return f"esum gflights returns --search {handle(_unpinned(q))} --id {flights_name(q.segments[0].legs)}"
    flags = {"return_": "--return"}
    return "esum gflights search " + " ".join(f"{flags.get(k, '--' + k)} {v}"
                                              for k, v in _search_args(q).items() if v is not None)


def _find(rows: list[dict], id: str, q: Query, looked: str = "on this search's first page") -> dict:
    want = _id(id)
    for row in rows:
        if row["id"] == want:
            return row
    raise _refused(NoSuchItem, f"Google Flights: no itinerary {want!r} {looked}",
                   f"Results change as fares sell: list them again with `{_relist(q)}` (add `--all` for the rows "
                   f"behind \"View more flights\") and use an `id` from that list")


class More(NamedTuple):
    """What expanding the results page gave: the rows the first page lacks, whether the page had a
    "View more flights" at all, the most rows one read could not name (they are left out), how
    many times the page was read, and why the reading stopped short of a read that added no row:
    empty when one did."""
    rows: list[dict]
    button: bool
    unreadable: int = 0
    reads: int = 1
    short: str = ""

    def notes(self) -> str:
        """What the reader should know about the rows from behind the button: empty when nothing."""
        said = []
        if self.short:
            said.append(f"{self.short}: run it again for more")
        count = lambda part: sum(part in row["missing"] for row in self.rows)      # noqa: E731
        unknown = [f"{name} unknown on {count(part)}" for part, name in
                   (("times", "times"), ("duration", "duration"), ("layovers", "layover lengths")) if count(part)]
        if unknown:
            unknown[-1] += " (the row's description was not recognized; `detail` reads the flights' own page)"
        said += unknown
        if count("price"):
            said.append(f"{count('price')} listed without a price (Google's row gave none)")
        elif count("currency"):
            said.append(f"currency unknown on {count('currency')}")
        if self.unreadable:
            said.append(f"{self.unreadable} more row{'s' if self.unreadable != 1 else ''} left out: the page gave "
                        f"no flights to name {'them' if self.unreadable != 1 else 'it'} by")
        return "; ".join(said)


def _expanded(ctx, q: Query, expected: bool) -> tuple[list[dict], int] | None:
    """One read of the results page in ESUM's browser, expanded by more.js: the rows it lists and
    how many it could not name. None when the page has no "View more flights" and none is
    `expected`; anything else that keeps the rest from being read is an error."""
    first_page = f"list the first page alone with `{_relist(q)}`"
    page = ctx.page_script(page_url(q), MORE_SCRIPT, {"timeout_ms": MORE_WAIT * 1000, "expect_more": expected})
    if not isinstance(page, dict) or not isinstance(page.get("rows"), list):
        raise _refused(SiteRefused, "Google Flights' results page gave ESUM's page script no rows to read",
                       f"Retry, or {first_page}")
    if not page.get("button"):
        if expected:
            raise _refused(SiteRefused, f"Google Flights says more flights exist, but its \"View more flights\" "
                                        f"button did not show within {MORE_WAIT} s",
                           f"Retry, or {first_page}")
        return None
    if not page.get("expanded"):
        raise _refused(SiteRefused, f"Google Flights' page did not list the flights behind \"View more flights\" "
                                    f"within {MORE_WAIT} s",
                       f"Retry, or {first_page}")
    rows, unreadable = more_rows(page["rows"])
    return rows, unreadable + (page.get("unread") if isinstance(page.get("unread"), int) else 0)


def _more(ctx, q: Query, known: list[dict], expected: bool, started: float) -> More:
    """The rows behind "View more flights", `known` (the first page's) left out.

    Google gathers a search's flights over some seconds and hands the page what it has: the first
    read of a search can list a fifth of what a read ten seconds later lists, and a later read can
    list fewer again (a subset). So the page is read again, `MORE_READS` times at most, and the
    rows are every read's together, each as the latest read gave it (its price can move).

    The reading ends with a read that adds no row, begun `MORE_SETTLE` seconds or more after
    `started`, the search's first request. A read that adds none can come sooner, while Google is
    still gathering (26 rows twice, then 40): in eight reads each of six first-time searches,
    growth ended 10 to 16 seconds in, and once a single row came at 24.

    A read that fails after one succeeded ends the reading with what there is; a browser check or
    a login the page asks for is not such a failure."""
    have = {flights_name(_legs(row)) for row in known}      # a row from behind the button is named so
    found: dict[str, dict] = {}
    unreadable, reads, settled, short = 0, 0, False, ""
    while reads < MORE_READS and not settled:
        begun = _clock()
        try:
            read = _expanded(ctx, q, expected)
        except (SiteRefused, BrowserError, *TRANSIENT_ERRORS) as e:
            if not reads:
                raise
            short = f"read {reads + 1} of the page failed ({e})"
            break
        if read is None:
            if not reads:
                return More([], False)
            short = f"read {reads + 1} of the page showed no \"View more flights\""
            break
        reads += 1
        rows, bad = read
        rows = [row for row in rows if row["id"] not in have]
        settled = reads > 1 and begun - started >= MORE_SETTLE and all(row["id"] in found for row in rows)
        found.update((row["id"], _mark_cabin(row, q)) for row in rows)
        unreadable = max(unreadable, bad)
    if not settled and not short:
        short = f"Google was still adding flights after {reads} reads"
    if unreadable and not found:
        raise _refused(SiteRefused, f"Google Flights' page listed more flights, but none of the {unreadable} rows "
                                    f"behind \"View more flights\" gave flights ESUM could read",
                       f"Retry, or list the first page alone with `{_relist(q)}`")
    return More(list(found.values()), True, unreadable, reads, short)


def _legs(row: dict) -> list[Leg]:
    return [Leg(leg["from"], leg["depart"][:10], leg["to"], leg["carrier"], leg["number"]) for leg in row["legs"]]


def _named_row(ctx, legs: list[Leg], q: Query) -> dict:
    """The itinerary these flights make, in full, for a search's first direction. The expanded list
    is not needed: the returns page for the flights names them (times, aircraft, seats), so that
    page is read, the round trip's own or, for a one-way, one with a return a week or more later
    (`DETAIL_RETURNS`, until one names the flights). Google
    decides whether the flights are a trip of the search (a city code covers airports ESUM can't
    list), and what it names must be the flights asked for. A round trip's price is the cheapest
    total with these flights; a one-way has none on that page."""
    first, name = q.segments[0], flights_name(legs)
    backs = [q.segments[1]] if q.round_trip else \
        [Segment(first.dests, first.origins, (date.fromisoformat(first.date) + timedelta(days=n)).isoformat())
         for n in DETAIL_RETURNS]
    data = found = None
    for back in backs:
        try:
            data = _fetch(ctx, replace(q, segments=(replace(first, legs=tuple(legs)), back), one_way=False))
        except ArgumentError:       # the invented return date is past what Google sells
            continue
        if (found := selected(data)) is not None:
            break
    if found is None:
        raise _refused(SiteRefused, f"Google Flights did not give the details of {name}",
                       f"Check the flights against `{_relist(q)} --all`; its booking page is "
                       f"{booking_url(pin(q, 0, legs))}" if q.one_way else
                       f"Check the flights against `{_relist(q)} --all`, or choose another outbound")
    if (given := flights_name(_legs(found))) != name:
        raise _refused(SiteRefused, f"Google Flights answered {name} with another itinerary ({given})",
                       f"Check each flight's airports and date against `{_relist(q)} --all` and use an `id` from "
                       f"that list")
    totals = [r for r in flights(data) if r["price"] is not None] if q.round_trip else []
    cheapest = min(totals, key=lambda r: r["price"], default=None)
    return _mark_cabin({**found, "id": name, "price": cheapest and cheapest["price"],
                        "currency": cheapest and cheapest["currency"], "best": False, "more": True}, q)


def _pick(ctx, data, id: str, q: Query) -> dict:
    """The row with this id: a first-page row by its flight numbers (UA837), or any itinerary of
    the search by its flights in full (SFO-NRT:2027-03-10:UA837), as `search --all` names the rows
    from behind "View more flights"."""
    rows = _rows(data, q)
    legs = named_flights(id)
    if legs is None:
        want = _id(id)
        if q.pinned() or not has_more(data) or any(row["id"] == want for row in rows):
            return _find(rows, id, q)
        # Flight numbers alone that the first page lacks: a row from behind "View more flights",
        # written short. Only that list has its airports and dates, so it is read again (the
        # browser, some 15 seconds) rather than refused.
        more = _more(ctx, q, rows, True, _clock())
        found = [row for row in more.rows if "+".join(leg["flight"] for leg in row["legs"]) == want]
        if len(found) > 1:
            raise _refused(ArgumentError, f"--id {want} names {len(found)} itineraries behind \"View more flights\"",
                           f"Use one's full id: {', '.join(row['id'] for row in found)}")
        if not found:
            return _find(rows, id, q, "on this search's first page or behind \"View more flights\"")
        full = _named_row(ctx, _legs(found[0]), q)
        if full["price"] is None:       # a one-way: the list has the price its own page lacks
            full.update(price=found[0]["price"], currency=found[0]["currency"])
        return full
    if q.pinned():
        raise _refused(ArgumentError, f"--id {id!r} names an outbound; this search value already has its outbound "
                                      f"chosen", f"Use a return's `id` from `{_relist(q)}`")
    name = flights_name(legs)
    return next((row for row in rows if flights_name(_legs(row)) == name), None) or _named_row(ctx, legs, q)


def _rows(data, q: Query) -> list[dict]:
    """The page's rows, with a note on legs sold in another cabin than the one searched."""
    rows = flights(data)
    for row in rows:
        _mark_cabin(row, q)
    return rows


def _mark_cabin(row: dict, q: Query) -> dict:
    searched = CABIN_NAMES[q.cabin]
    other = [leg for leg in row["legs"] if leg["cabin"] and leg["cabin"] != searched]
    if other and len(other) == len(row["legs"]):
        cabins = ", ".join(sorted({leg["cabin"] for leg in other}))
        row["warnings"].append(f"no {searched} on this itinerary: sold as {cabins}")
    elif other:
        row["warnings"].append(f"mixed cabin: {', '.join(leg['flight'] + ' ' + leg['cabin'] for leg in other)}")
    return row


def _stops(stops: int | None) -> str:
    return "any" if stops is None else "nonstop" if stops == 0 else f"at most {stops}"


def _route(q: Query) -> tuple[str, str]:
    first = q.segments[0]
    route = f"{','.join(first.origins)}→{','.join(first.dests)}" + (f"→{','.join(first.origins)}"
                                                                     if q.round_trip else "")
    dates = first.date + (f" – {q.segments[1].date}" if q.round_trip else "")
    return route, dates


def _about(q: Query, data, rows: list[dict], sort: str) -> dict:
    cur = next((r["currency"] for r in rows if r["currency"]), None) or q.currency or "USD"
    info = insight(data)
    route, dates = _route(q)
    stops = q.segments[0].max_stops
    return {"route": route, "dates": dates, "cabin": CABIN_NAMES[q.cabin],
            "stops": _stops(stops), "adults": q.adults if q.adults != 1 else None,
            "currency": cur, "sort": sort,
            "prices": ("round-trip total" if q.round_trip else "one way")
                      + (f" for {q.adults} adults" if q.adults != 1 else " for 1 adult"),
            "price_insight": insight_text(info, cur), "price_history": history_text(info, cur)}


def _insight(data, about: dict) -> dict | None:
    info = insight(data)
    return {**info, "currency": about["currency"]} if info else None


def _ordered(rows: list[dict], sort: str) -> list[dict]:
    return rows if sort == "best" else sorted(rows, key=SORTS[sort])


def _listing(name: str, rows: list[dict], about: dict, limit: int, steps: list[Next], empty: str,
             extra: dict | None = None) -> Listing:
    total = len(rows)
    if total > limit:
        steps = [*steps, Next("returns" if name == "returns" else "search", {"limit": total},
                              f"show all {total} itineraries")]
    rows = rows[:limit]
    cur = about["currency"]
    table = [{**{k: v for k, v in r.items() if k not in ("legs", "minutes", "currency", "best", "more", "missing")},
              # a row from behind the button whose currency can't be read shows its number bare
              "price": money(r["price"], r["currency"] or ("" if "currency" in r.get("missing", ()) else cur)),
              "layovers": " · ".join(r["layovers"]), "warnings": "; ".join(r["warnings"])} for r in rows]
    fields = ("id", "price", "itinerary")
    if about["cabin"] != "economy":
        fields += ("seats",)       # no legroom outside economy: the seat type tells the cabins apart
    if any(r["warnings"] for r in table):
        fields += ("warnings",)
    return Listing(name=name, rows=table, fields=fields, about=about, total=total,
                   data={**{k: v for k, v in {**about, **(extra or {})}.items() if v is not None}, "total": total,
                         name: rows},
                   empty=empty, next=steps)


@action(gflights, tier=Tier.READ)
def search(ctx, origin: str, dest: str, depart: str, return_: str | None = None, cabin: Cabin = "economy",
           stops: int | None = None, adults: int = 1, currency: str | None = None, sort: Sort = "price",
           limit: int = 25, all_: bool = False):
    """Flights for a date, one way or round trip, with Google's price insight (one page, about a second).

    Rows are the itineraries Google's results page lists before "View more flights" (often 6-60,
    the lowest price among them), cheapest first unless `--sort` says otherwise (`best` keeps
    Google's own ranking). Each row reads `ORIGIN date time → DEST date time · duration · via
    STOPS · flights`, in local times. A row's `id` is its flight numbers (UA901, or LH2473+LH458
    with a connection); `returns` and `detail` take it with the printed `search` value.

    `--all` adds the rows behind "View more flights" (often 30-150 more: other connections, times
    and airlines). Google's page loads them itself, so ESUM opens the page in a background tab of
    its browser, clicks "View more flights", which only shows the rest of the list, reads it and
    closes the tab. Google hands the page what it has gathered so far, so ESUM reads it again
    until a read adds no row, for 12 seconds at least, and lists every read's rows: 13-20 seconds
    more as measured, and more when Google's page is slow (eight reads at most). Such a row has its
    flights, times, stops, price and CO2; its aircraft, seats and amenities show as `?` until
    `detail` reads them. Its `id` is its flights in full, each with its airports and departure
    date (`SFO-ICN:2027-01-20:OZ211+ICN-NRT:2027-01-22:OZ102`): `returns`, `detail` and `fares`
    take that id with the `search` value and read the itinerary by its flights: the expanded list
    is not read again and no browser opens. Its flight numbers alone (`OZ211+OZ102`) work too, the
    slow way: the list is read again to find its airports and dates, some 15 seconds. `listed` says how many rows came from
    where, and what it could not read: a row whose times it can't read is still listed, by its
    flights.

    A round trip lists outbound flights priced at the cheapest round-trip total with each one; pick
    one and run `returns` to see the return flights and the exact totals.

    `price_insight` is Google's verdict on today's lowest price against what similar trips usually
    cost; `price_history` is how that lowest price moved over the last two months.

    Args:
        origin: Origin airport or city codes, comma-separated (SFO, NYC, SFO,OAK).
        dest: Destination airport or city codes, comma-separated.
        depart: Outbound date, YYYY-MM-DD.
        return_: Return date for a round trip, YYYY-MM-DD.
        cabin: economy, premium (premium economy), business or first.
        stops: Most stops: 0 (nonstop), 1 or 2. Default any.
        adults: Adult passengers (1-9); prices are the total for all of them.
        currency: Price currency, ISO code (USD, EUR, JPY). Default USD.
        sort: price (cheapest first), best (Google's ranking), duration, departure or arrival.
        limit: Itineraries to show.
        all_: Also list the rows behind "View more flights" (ESUM's browser, 13-20 seconds more).
    """
    out = _date(depart, "--depart")
    back = _date(return_, "--return") if return_ else None
    q = _query(origin, dest, out, back, cabin=cabin, stops=stops, adults=adults, currency=currency)
    started = _clock()
    data = _fetch(ctx, q)
    rows = _rows(data, q)
    listed = None
    if all_ and (rows or has_more(data)):
        shown = len(rows)
        more = _more(ctx, q, rows, has_more(data), started)
        rows += more.rows
        listed = (f"{len(rows)} on Google's expanded page, read {more.reads} times: {shown} from the first page, "
                  f"{len(more.rows)} from behind \"View more flights\"" if more.button else
                  f"the page's {shown}: Google's page has no \"View more flights\" for this search")
        if more.notes():
            listed += f"; {more.notes()}"
    elif has_more(data):
        listed = f"the first page's {len(rows)}; Google has more behind \"View more flights\""
    rows = _ordered(rows, sort)
    about = {**_about(q, data, rows, sort), "listed": listed, "search": handle(q)}
    h = handle(q)
    first = rows[0]["id"] if rows else None
    if rows and q.round_trip:
        steps = [Next("returns", {"search": h, "id": first}, "see the return flights and round-trip totals with "
                                                             "the first outbound"),
                 Next("detail", {"search": h, "id": "<id>"}, "see an outbound's legs, aircraft, seats and "
                                                             "amenities")]
    elif rows:
        steps = [Next("detail", {"search": h, "id": first}, "see the first itinerary's legs, aircraft, seats, "
                                                            "amenities and booking link"),
                 Next("detail", {"search": h, "id": "<id>"}, "see another itinerary")]
    if rows and has_more(data) and not all_:
        steps.append(Next("search", {**{k: v for k, v in _search_args(q).items() if v is not None}, "all_": True},
                          "list the flights behind Google's \"View more flights\" too: more connections, times and "
                          "airlines, from the lowest price here up (ESUM's browser, 13-20 seconds more)"))
    if rows:
        steps.append(Next("search", {"origin": origin, "dest": dest, "depart": out.isoformat(),
                                     "return_": back.isoformat() if back else None, "cabin": cabin, "stops": stops,
                                     "adults": adults, "currency": currency, "thorough": True},
                          "look for carriers this page leaves out, with fare basis codes and fare rules "
                          "(Matrix, several searches, 1-2 minutes; add --carriers for airlines not shown here)",
                          site="matrix"))
    elif not data:   # Google answers an unknown airport with an empty page, not with "no flights"
        steps = []
    else:
        nights = (back - out).days if back else None
        steps = [Next("dates", {"origin": origin, "dest": dest,
                                "start": max(out - timedelta(days=3), date.today()).isoformat(),
                                "end": (out + timedelta(days=3)).isoformat(), "nights": nights, "cabin": cabin,
                                "stops": stops, "adults": adults, "currency": currency},
                      "see which dates around it have flights (7 searches)",
                      when_empty=True)]
    return _listing("itineraries", rows, about, limit, steps,
                    "Google Flights lists no flights for this search; allow more stops or try other dates" if data
                    else (f"Google Flights knows no route between these codes ({origin.upper()} → {dest.upper()}). "
                          f"{_find_code()}").rstrip(),
                    extra={"insight": _insight(data, about)})


@action(gflights, tier=Tier.READ)
def returns(ctx, search: str, id: str, sort: Sort = "price", limit: int = 25):
    """Return flights for one outbound of a round-trip search, each priced at the round-trip total.

    Reads the outbound list again to find the chosen outbound, then the returns page for it (two
    pages, about two seconds). The result's `search` value pins that outbound; `detail` takes it
    with a return row's `id` for the whole trip and its booking link. The returns are the ones
    Google's page lists before "View more flights".

    Args:
        search: The `search` value a round-trip `search` printed.
        id: The outbound's `id` from that list (UA901, or the flights in full for a row from `search --all`).
        sort: price (cheapest first), best (Google's ranking), duration, departure or arrival.
        limit: Itineraries to show.
    """
    q = _handle(search)
    if not q.round_trip:
        raise ArgumentError("--search is a one-way search; it has no returns",
                            help=f"Search the round trip with `{_relist(q)} --return <YYYY-MM-DD>`, or see this "
                                 f"itinerary with `esum gflights detail --search {search.strip()} --id {id}`")
    if q.pinned():
        raise ArgumentError("--search already has its outbound chosen; it is a returns list",
                            help=f"Open a return with `esum gflights detail --search {search.strip()} --id <id>`")
    outbound = _pick(ctx, _fetch(ctx, q), id, q)
    pinned = pin(q, 0, _legs(outbound))
    data = _fetch(ctx, pinned)
    rows = _ordered(_rows(data, pinned), sort)
    h = handle(pinned)
    about = {**_about(pinned, data, rows, sort), "outbound": f"{outbound['id']}: {outbound['itinerary']}",
             "search": h}
    about.pop("price_history", None)
    first = rows[0]["id"] if rows else None
    steps = [Next("detail", {"search": h, "id": first}, "see the whole trip: legs, aircraft, seats, amenities "
                                                        "and booking link"),
             Next("fares", {"search": h, "id": first}, "see what the airline and the agencies sell on that trip: "
                                                       "each fare brand's price and terms"),
             Next("detail", {"search": h, "id": "<id>"}, "see the trip with another return")] if rows else \
        [Next("search", {k: v for k, v in _search_args(q).items() if v is not None},
              "list the outbounds again and choose another", when_empty=True)]
    return _listing("returns", rows, about, limit, steps,
                    "Google Flights lists no returns with this outbound; choose another outbound",
                    extra={"outbound": outbound, "insight": _insight(data, about)})


def _leg_lines(legs: list[dict], layovers: list[str]) -> list[str]:
    lines = []
    for i, leg in enumerate(legs):
        lines.append(f"  {leg['flight']} {leg['airline']} · {leg['from']} {leg['depart']} → {leg['to']} "
                     f"{leg['arrive']} · {leg['duration']}")
        about = [x for x in (leg["aircraft"], leg["cabin"], leg["legroom"] and f"{leg['legroom']} legroom",
                             leg["seat"]) if x]
        about += leg["amenities"]
        if about:
            lines.append(f"    {' · '.join(about)}")
        if leg["operated_by"]:
            lines.append(f"    operated by {leg['operated_by']}")
        if leg["also_sold_as"]:
            lines.append(f"    also sold as {' '.join(leg['also_sold_as'])}")
        if i < len(layovers):
            lines.append(f"  layover {layovers[i]}")
    return lines


def _slice_lines(title: str, row: dict, adults: int = 1) -> list[str]:
    co2 = None
    if row["co2_kg"]:
        co2 = f"{row['co2_kg']} kg CO2e" + (" for all passengers" if adults > 1 else "")
        if row["co2_typical_kg"]:
            co2 += f" (typical {row['co2_typical_kg']})"
    head = f"{title}: {row['itinerary']}" + (f" · {co2}" if co2 else "")
    notes = [w for w in row["warnings"] if " operated by " not in w]   # each leg already says it
    return [head, *_leg_lines(row["legs"], row["layovers"]), *(f"  note: {w}" for w in notes)]


@action(gflights, tier=Tier.READ)
def detail(ctx, search: str, id: str):
    """One itinerary: each flight's times, aircraft, cabin, legroom, seat and amenities, codeshares,
    layovers, CO2, the price, and a link to its Google Flights booking page (airline and agency fares).

    With a round-trip `search` value from `search`, the id is an outbound and the price is the
    cheapest round trip with it; with the `search` value from `returns`, the id is a return and the
    result is the whole trip.

    Args:
        search: The `search` value a `search` or `returns` result printed.
        id: The itinerary's `id` from that list (UA901, LH2473+LH458, or the flights in full for a row from `search --all`).
    """
    return _detail(ctx, search, id)


def _detail(ctx, search: str, id: str) -> Result:
    q = _handle(search)
    h = search.strip()
    data = _fetch(ctx, q)
    row = _pick(ctx, data, id, q)
    cur = row["currency"] or q.currency or "USD"
    # a one-way named by its flights is read off a page that prices other trips: no price to show
    price = "" if row["price"] is None and row.get("more") else money(row["price"], cur) + " "
    lines: list[str] = []
    steps: list[Next] = []
    if q.pinned():
        outbound = selected(data)
        if outbound is not None:
            _mark_cabin(outbound, q)
        if outbound is None:
            raise _refused(SiteRefused, "Google Flights' returns page did not name the chosen outbound",
                           "Run `returns` again from the round-trip search")
        trip = [outbound, row]
        title = f"{price}round trip: {outbound['id']} | {row['id']}"
        lines += _slice_lines("Outbound", outbound, q.adults) + _slice_lines("Return", row, q.adults)
    else:
        trip = [row]
        kind = "round trip from" if q.round_trip else "one way"
        title = f"{price}{kind}: {row['id']}"
        lines += _slice_lines("Outbound" if q.round_trip else "Flight", row, q.adults)
        if q.round_trip:
            steps.append(Next("returns", {"search": h, "id": row["id"]},
                              "see the returns with this outbound and the exact round-trip totals"))
    legs = [[Leg(leg["from"], leg["depart"][:10], leg["to"], leg["carrier"], leg["number"])
             for leg in part["legs"]] for part in trip]
    booking = None
    if len(legs) == len(q.segments):
        booked = q
        for i, chosen in enumerate(legs):
            booked = pin(booked, i, chosen)
        booking = booking_url(booked)
        lines.append(f"Book (airline and agency fares): {booking}")
        steps.append(Next("fares", {"search": h, "id": row["id"]},
                          "see what the airline and the agencies sell on these flights: each fare brand's price "
                          "and terms (opens the booking page in ESUM's browser, a few seconds)"))
    passengers = f"total for {q.adults} adults, " if q.adults != 1 else ""
    text = "\n".join([f"Itinerary {title} ({passengers}{CABIN_NAMES[q.cabin]})", *lines])
    return Result(data={"search": h, "id": row["id"], "price": row["price"], "currency": cur,
                        "cabin": CABIN_NAMES[q.cabin], "adults": q.adults, "slices": trip, "booking_url": booking},
                  text=text, summary=title, next=steps)


BOOKING_SCRIPT = (Path(__file__).parent / "booking.js").read_text(encoding="utf-8")
BOOKING_WAIT = 20      # seconds for the booking page to fill in its options


_FLIGHTS_HELP = ("Write each direction as ORIGIN-DEST:YYYY-MM-DD:FLIGHT, a connection as its flights joined by "
                 "`+`, each with its own airports and departure date: `esum gflights fares --flights "
                 "SFO-NRT:2027-03-10:UA837 NRT-SFO:2027-03-20:UA838`")


def _named(flights: list[str], cabin: str, adults: int, currency: str | None) -> Query:
    """The trip `--flights` names, every flight pinned: what the booking page's token needs."""
    if len(flights) > 2:
        raise ArgumentError(f"--flights has {len(flights)} directions: give one (one way) or two (a round trip)",
                            help=_FLIGHTS_HELP)
    if not 1 <= adults <= 9:
        raise ArgumentError(f"--adults {adults}: Google Flights takes 1 to 9 passengers")
    cur = currency.strip().upper() if currency else None
    if cur is not None and not re.fullmatch(r"[A-Z]{3}", cur):
        raise ArgumentError(f"--currency {currency!r}: give an ISO currency code (USD, EUR, JPY)")
    segments, last = [], None
    for value in flights:
        legs = []
        for part in re.sub(r"\s+", "", value).upper().split("+"):
            leg = named_flight(part)
            if not leg:
                raise ArgumentError(f"--flights {value!r}: {part!r} is not ORIGIN-DEST:YYYY-MM-DD:FLIGHT"
                                    if part else f"--flights {value!r}: a flight is missing around a `+`",
                                    help=_FLIGHTS_HELP)
            day = _date(leg.date, f"--flights {part}:").isoformat()
            if last and day < last:
                raise ArgumentError(f"--flights {value!r}: {part} departs before the flight ahead of it ({last})",
                                    help="Give the flights in the order flown, each with its own departure date")
            last = day
            legs.append(leg)
        segments.append(Segment((legs[0].origin,), (legs[-1].dest,), legs[0].date, tuple(legs)))
    return Query(tuple(segments), adults, CABINS[cabin], cur, one_way=len(segments) == 1)


def _named_text(q: Query) -> str:
    return " | ".join(" + ".join(f"{leg.origin}→{leg.dest} {leg.date} {leg.airline}{leg.number}" for leg in s.legs)
                      for s in q.segments)


@action(gflights, tier=Tier.READ)
def fares(ctx, search: str | None = None, id: str | None = None, flights: list[str] | None = None,
          cabin: Cabin | None = None, adults: int | None = None, currency: str | None = None):
    """What is sold on one itinerary's flights: the airline's fare brands (Basic Economy, Economy,
    Economy Fully Refundable) with each one's price and terms, and each agency's price.

    Google's booking page for the itinerary, read in ESUM's browser: the page computes its
    options itself, so this opens it in a background tab, waits for them (a few seconds) and
    closes the tab. Nothing is clicked and nothing is booked.

    Name the trip one of two ways. `--flights` names the flights themselves, with no search
    before it: one value per direction (one for a one way, two for a round trip), each
    `ORIGIN-DEST:YYYY-MM-DD:FLIGHT`. A connection is its flights joined by `+`, each with its
    own airports and its own departure date, local to the airport it leaves: after an overnight
    connection the next flight takes the next day's date
    (`SFO-MUC:2027-03-10:LH459+MUC-LHR:2027-03-11:LH2474`). Use the flight numbers Google shows,
    the operating airline's: a codeshare's own number (UA7949 for NH7) lists no options.
    `--cabin`, `--adults` and `--currency` go with it.

    Or give the `search` and `id` that `detail` takes, for a whole trip: a one-way flight, or a
    return with the `search` value `returns` printed; the cabin, passengers and currency are
    that search's.

    The airline's brands are the rows with a `brand`; an agency, or another airline selling the
    flights under its own numbers, gives one price, which can be below the airline's cheapest.
    A brand's terms are Google's summary of it (No refunds; Free change, possible fare
    difference): the fare's rules decide (`esum matrix rules`). Prices are the total for all
    passengers.

    Args:
        search: The `search` value a `search` or `returns` result printed (with `--id`).
        id: The itinerary's `id` from that list (its flight numbers, e.g. UA901 or LH2473+LH458).
        flights: The flights, one value per direction: SFO-NRT:2027-03-10:UA837 NRT-SFO:2027-03-20:UA838.
        cabin: With `--flights`: economy, premium (premium economy), business or first. Default economy.
        adults: With `--flights`: adult passengers (1-9). Default 1.
        currency: With `--flights`: price currency, ISO code (USD, EUR, JPY). Default USD.
    """
    if flights:
        if search or id:
            raise ArgumentError("give --flights, or --search with --id, not both", help=_FLIGHTS_HELP)
        q = _named(flights, cabin or "economy", adults or 1, currency)
        again = _relist(_unpinned(q))
        kind = "round trip" if q.round_trip else "one way"
        return _fares(ctx, booking_url(q), f"{kind}: {_named_text(q)}", CABIN_NAMES[q.cabin], q.adults,
                      {"flights": flights},
                      f"Check the flight numbers, airports and dates against `{again}`: each flight takes the "
                      f"number Google shows and its own departure date",
                      [Next("search", {k: v for k, v in _search_args(_unpinned(q)).items() if v is not None},
                            "see these dates' flights, times and prices")])
    if not search or not id:
        raise ArgumentError("name the trip: --flights, or --search with --id",
                            help=_FLIGHTS_HELP + ", or copy `search` and `id` from an `esum gflights search` result")
    given = [flag for flag, value in (("--cabin", cabin), ("--adults", adults), ("--currency", currency))
             if value is not None]
    if given:
        raise ArgumentError(f"{', '.join(given)} with --search: the search value already carries the cabin, "
                            f"passengers and currency",
                            help="Leave them out, or search again with them (`esum gflights search`)")
    found = _detail(ctx, search, id)
    url = found.data["booking_url"]
    if not url:
        raise _refused(ArgumentError, f"{found.data['id']} is the outbound of a round trip: the booking page is for "
                                      f"a whole trip",
                       f"Pick the return with `esum gflights returns --search {search.strip()} --id {found.data['id']}`, "
                       f"then run `fares` with the `search` value it prints and the return's id")
    return _fares(ctx, url, found.summary, found.data["cabin"], found.data["adults"],
                  {"search": search.strip(), "id": found.data["id"]},
                  f"Open the page yourself to see what it says: {url}",
                  [Next("detail", {"search": search.strip(), "id": found.data["id"]},
                        "see the flights' times, aircraft and seats")])


def _fares(ctx, url: str, itinerary: str, cabin: str, passengers: int, named: dict, no_options: str,
           steps: list[Next]) -> Listing:
    """The booking page's options for one trip; `named` is how the caller named it."""
    page = ctx.page_script(url, BOOKING_SCRIPT, {"timeout_ms": BOOKING_WAIT * 1000}) or {}
    rows = booking_options(str(page.get("text") or ""))
    if not rows:
        raise _refused(SiteRefused, f"Google's booking page listed no booking options for {itinerary} "
                                    f"within {BOOKING_WAIT} s", no_options)
    table = [{"seller": r["seller"] + (" (airline)" if r["airline"] else ""), "brand": r["brand"],
              "price": r["price"], "terms": "; ".join(r["terms"])} for r in rows]
    about = {"itinerary": itinerary, "cabin": cabin,
             "prices": f"total for {passengers} adult{'s' if passengers != 1 else ''}",
             # the page was read: it lists brands only for an airline that gives Google them
             "brands": None if any(r["brand"] for r in rows) else
             "none on Google's page for this trip: each seller's one price, the airline's too", "source": url}
    return Listing(name="fares", rows=table, fields=("seller", "brand", "price", "terms"), about=about,
                   total=len(table), data={**about, **named, "fares": rows}, next=steps)


@action(gflights, tier=Tier.READ)
def dates(ctx, origin: str, dest: str, start: str, end: str, nights: int | None = None,
          cabin: Cabin = "economy", stops: int | None = None, adults: int = 1, currency: str | None = None):
    """The cheapest flight for each departure date in a range, one way or a fixed-length round trip.

    Google's page has no fare calendar, so each date is its own search (a page, about a second;
    four run at once). At most 31 dates per call. `level` is Google's price insight for that
    date's own search: low, typical or high against what that date usually costs (empty when
    Google gave none). It does not compare dates with each other; the prices do.

    Args:
        origin: Origin airport or city codes, comma-separated (SFO, NYC).
        dest: Destination airport or city codes, comma-separated.
        start: First departure date, YYYY-MM-DD.
        end: Last departure date, YYYY-MM-DD.
        nights: Round trip: return this many days after departing. Leave out for one way.
        cabin: economy, premium (premium economy), business or first.
        stops: Most stops: 0 (nonstop), 1 or 2. Default any.
        adults: Adult passengers (1-9); prices are the total for all of them.
        currency: Price currency, ISO code (USD, EUR, JPY). Default USD.
    """
    first, last = _date(start, "--start"), _date(end, "--end")
    if last < first:
        raise ArgumentError(f"--end {last} is before --start {first}")
    count = (last - first).days + 1
    if count > MAX_DATES:
        raise ArgumentError(f"--start {first} to --end {last} is {count} dates; each costs a page, so the most is "
                            f"{MAX_DATES}", help="Split the range into several calls")
    if nights is not None and not 0 <= nights <= 330:
        raise ArgumentError(f"--nights {nights}: give 0 or more days between departing and returning")
    days = [first + timedelta(days=i) for i in range(count)]
    queries = [_query(origin, dest, d, d + timedelta(days=nights) if nights is not None else None, cabin=cabin,
                      stops=stops, adults=adults, currency=currency) for d in days]

    def one(q: Query) -> dict:
        row: dict = {"date": q.segments[0].date, "return": q.segments[1].date if q.round_trip else None}
        try:
            data = _fetch(ctx, q)
        except (HttpError, SiteRefused, ArgumentError, *TRANSIENT_ERRORS, ValueError) as e:
            why = getattr(e, "reason", None) or (f"HTTP {e.status}" if isinstance(e, HttpError) else type(e).__name__)
            return {**row, "price": None, "note": f"search failed ({why})", "error": e}
        found = [r for r in _rows(data, q) if r["price"] is not None]
        info = insight(data)
        if not found:
            return {**row, "price": None, "note": "no flights"}
        best = min(found, key=lambda r: r["price"])
        return {**row, "price": best["price"], "currency": best["currency"], "level": (info or {}).get("level"),
                "id": best["id"], "flight": best["itinerary"], "warnings": best["warnings"],
                "search": handle(q)}

    with ThreadPoolExecutor(max_workers=min(DATE_WORKERS, len(queries))) as pool:
        rows = list(pool.map(one, queries))
    errors = [r.pop("error") for r in rows if "error" in r]
    if len(errors) == len(rows):   # nothing answered: the call failed, with the first date's own error
        raise errors[0]
    priced = [r for r in rows if r["price"] is not None]
    cur = next((r["currency"] for r in priced if r.get("currency")), None) or \
        (currency.strip().upper() if currency else "USD")
    best = min(priced, key=lambda r: r["price"]) if priced else None
    failed = [r["date"] for r in rows if r.get("note", "").startswith("search failed")]
    route = f"{origin.upper()}→{dest.upper()}" + (f"→{origin.upper()}" if nights is not None else "")
    about = {"route": route, "dates": f"{first} – {last}", "nights": nights, "cabin": cabin,
             "stops": _stops(stops), "adults": adults if adults != 1 else None, "currency": cur,
             "prices": ("round-trip total" if nights is not None else "one way")
                       + (f" for {adults} adults" if adults != 1 else " for 1 adult"),
             "cheapest": f"{money(best['price'], cur)} on "
                         + ", ".join(r["date"] for r in priced if r["price"] == best["price"]) if best else None,
             "failed": ", ".join(failed) or None}
    table = [{"date": r["date"], "return": r["return"], "price": money(r["price"], cur) if r["price"] is not None
              else r.get("note"), "level": r.get("level") or "", "flight": r.get("flight") or "", "id": r.get("id"),
              "warnings": "; ".join(r.get("warnings") or []), "search": r.get("search")} for r in rows]
    fields = ("date", "return", "price", "level", "flight") if nights is not None else \
        ("date", "price", "level", "flight")
    if any(r["warnings"] for r in table):
        fields += ("warnings",)
    steps = []
    if best:
        if nights is not None:
            steps.append(Next("returns", {"search": best["search"], "id": best["id"]},
                              f"see the returns with the cheapest date's outbound ({best['date']})"))
        else:
            steps.append(Next("detail", {"search": best["search"], "id": best["id"]},
                              f"see the cheapest date's flight ({best['date']})"))
        steps.append(Next("search", {"origin": origin, "dest": dest, "depart": best["date"],
                                     "return_": best["return"], "cabin": cabin, "stops": stops, "adults": adults,
                                     "currency": currency}, "list the flights Google shows on the cheapest date"))
    return Listing(name="days", rows=table, fields=fields, about=about, total=len(rows),
                   data={**{k: v for k, v in about.items() if v is not None}, "days": rows},
                   empty="no dates", next=steps)
