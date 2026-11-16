"""ITA Matrix: Google's airfare search engine (QPX) over its public JSON API, no browser and no login."""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from datetime import date, timedelta
from collections.abc import Callable
from typing import Literal, NamedTuple

from esum import (ArgumentError, HttpError, Listing, Next, Result, SiteRefused, Tier, Transport, action,
                   command_line, site)
from esum.transport import TRANSIENT_ERRORS

from .parse import (amount, api_error, booking, calendar_rows, CATEGORIES, city_airports, Climb, condition,
                    currency_of, fare_traits, full_id, google_fares, location_rows, operators, PRESETS, rows_route,
                    rule_sections, Rung, rung, Skip, solution_page, SolutionPage, split_token, summary, without)

FORMAT = 1
EXTRAS = ()
PORTED_FROM = ("itamx src/itamx/client.py (github.com/YogevKr/itamx, MIT): the /v1/search, /v1/summarize "
               "and location-lookup request shapes; calendar, stop-count and sort inputs read from Matrix's "
               "own web client")

API = "https://content-alkalimatrix-pa.googleapis.com/v1"
SEARCH_TIMEOUT = 150      # a search runs 20-60 s on Matrix's side; a month's calendar about as long

matrix = site(
    name="matrix",
    base_url="https://matrix.itasoftware.com",
    transport=Transport.DIRECT,
    label={"en": "ITA Matrix: Google's airfare search; fares by date, routing language, fare basis, booking "
                 "classes and fare construction",
           "zh": "ITA Matrix：Google 机票搜索引擎；低价日历、路由语言、票价基础、舱位与票价构成"},
    extras={},
)

Cabin = Literal["economy", "premium", "business", "first"]
Sort = Literal["price", "duration", "departure", "arrival"]
CABINS = {"economy": "COACH", "premium": "PREMIUM-COACH", "business": "BUSINESS", "first": "FIRST"}
SORTS = {"price": "default", "duration": "duration", "departure": "departure", "arrival": "arrival"}
SEARCH_SUMMARIZERS = ["solutionList", "itineraryCarrierList", "itineraryStopCountList", "currencyNotice"]

_CODES = re.compile(r"^[A-Za-z]{3}(,[A-Za-z]{3})*$")
_LEG = re.compile(r"^([A-Za-z,]+)-([A-Za-z,]+):(\d{4}-\d{2}-\d{2})$")


def _codes(value: str, flag: str) -> list[str]:
    text = value.replace(" ", "")
    if not _CODES.match(text):
        raise ArgumentError(f"{flag} {value!r}: give 3-letter airport or city codes, comma-separated (JFK or NYC "
                            f"or JFK,EWR)", help="Find a code with `esum matrix airports --query <city name>`")
    return text.upper().split(",")


def _date(value: str, flag: str) -> str:
    text = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ArgumentError(f"{flag} {value!r}: give a date as YYYY-MM-DD")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ArgumentError(f"{flag} {value!r} is not a calendar date") from None


def _slice(origins: list[str], dests: list[str], day: str | None, *, flex: int = 0,
           routing: str | None = None, extension: str | None = None) -> dict:
    out: dict = {"origins": origins, "destinations": dests, "filter": {"warnings": {"values": []}},
                 "selected": False}
    if day:
        out.update(date=day, dateModifier={"minus": flex, "plus": flex}, isArrivalDate=False)
    if routing:
        out["routeLanguage"] = routing
    if extension:
        out["commandLine"] = extension
    return out


def _inputs(slices: list[dict], *, cabin: str, stops: int | None, adults: int, currency: str | None,
            sales_city: str | None, sort: str = "price", limit: int = 25, children: int = 0) -> dict:
    if stops is not None and not 0 <= stops <= 2:
        raise ArgumentError(f"--stops {stops}: Matrix takes 0 (nonstop), 1 or 2; leave it out for any number")
    inputs = {"filter": {}, "page": {"current": 1, "size": limit}, "slices": slices,
              "pax": {"adults": adults, **({"children": children} if children else {})},
              "firstDayOfWeek": "SUNDAY", "internalUser": False, "sliceIndex": 0, "sorts": SORTS[sort],
              "cabin": CABINS[cabin], "changeOfAirport": True, "checkAvailability": True}
    if stops is not None:
        inputs["maxStopCount"] = stops
    if currency:
        inputs["currency"] = currency.strip().upper()
    if sales_city:
        inputs["salesCity"] = _codes(sales_city, "--sales-city")[0]
    return inputs


def _post(ctx, path: str, body: dict, *, timeout: float = 60, search: str | None = None,
          id: str | None = None) -> dict:
    data = ctx.http.request("POST", f"{API}/{path}", json=body, timeout=timeout).json()
    err = api_error(data)
    if err:
        kind, message = err
        if search is not None and "invalid session id" in message.lower():   # a search Matrix no longer keeps
            raise ArgumentError(f"Matrix: {message} (--search {search})",
                                help="Matrix no longer has this search (or the --search value is wrong): run "
                                     "`esum matrix search` again and use the `search` value it prints")
        if id is not None and ("unknown solution id" in message.lower()
                               or "valid value for x.solution" in message.lower()):
            raise ArgumentError(f"Matrix: no itinerary {id!r} in this search (Matrix: {message})",
                                help="Use an `id` from this search's own list (a row number), or list them again "
                                     f"with `esum matrix results --search {search}`")
        if kind == "input":
            raise ArgumentError(f"Matrix: {message}")
        raise SiteRefused(f"Matrix: {message}")
    return data


def _check_codes(ctx, slices: list[dict]) -> None:
    """After an empty answer: Matrix returns no itineraries, not an error, for some unknown codes."""
    codes = dict.fromkeys(c for s in slices for c in (*s["origins"], *s["destinations"]))
    for code in codes:
        found = location_rows(ctx.http.get_json(f"{API}/locationTypes/CITIES_AND_AIRPORTS/partialNames/{code}/locations",
                                                params={"pageSize": 10}))
        if not any(code in (r["code"], r["city"]) for r in found):
            raise ArgumentError(f"Matrix knows no airport or city with the code {code}",
                                help=f"Look the place up with `esum matrix airports --query <name>`")


def _token(value: str) -> tuple[str, str, str | None]:
    parts = split_token(value)
    if not parts:
        raise ArgumentError(f"--search {value!r}: give the `search` value a `search`, `results` or `calendar` "
                            f"result printed (ids joined by dots)")
    return parts


def _matrix_id(id: str, prefix: str | None, search: str) -> str:
    """A row number (`38`) or a raw Matrix id back to the id Matrix knows."""
    text = str(id).strip()
    if text.isdigit() and len(text) <= 6:
        full = full_id(text, prefix)
        if full is None and prefix:
            raise ArgumentError(f"--id {text} is not a row number of this search",
                                help=f"List them with `esum matrix results --search {search}`")
        if full is None:
            raise ArgumentError(f"--id {text}: this --search value carries no row numbers; pass the id as its list "
                                f"printed it", help=f"List the ids with `esum matrix results --search {search}`")
        return full
    return text


def _add_operators(ctx, token: str, rows: list[dict]) -> None:
    """Who flies each listed itinerary: one booking-details request (about a second) per row."""
    for row in rows:
        search = row.get("search") or token     # a thorough search's row names its own sub-search
        session, solution_set, _ = split_token(search)
        body = {"summarizers": ["bookingDetails"], "inputs": {"solution": f"{solution_set}/{row['matrix_id']}"},
                "summarizerSet": "viewDetails", "solutionSet": solution_set, "session": session}
        row["operators"] = operators(_post(ctx, "summarize", body, search=search, id=row["id"]))


def _try_search(ctx, body: dict) -> SolutionPage | str:
    """One search that may fail without failing its caller: the page, or why it failed."""
    try:
        return solution_page(_post(ctx, "search", body, timeout=SEARCH_TIMEOUT))
    except HttpError as e:
        return f"search failed (HTTP {e.status})"
    except (SiteRefused, ArgumentError) as e:
        return f"search failed ({e})"
    except (*TRANSIENT_ERRORS, ValueError) as e:
        return f"search failed ({type(e).__name__}: {e})"


def _page_listing(page, about: dict, sort: str, limit: int, *, more: bool, empty_steps: list[Next] = (),
                  with_operators: bool = False) -> Listing:
    about = {**about, "search": page.token, "sort": sort,
             "page": f"{page.page} of {page.pages}" if page.pages else None,
             "currency": page.currency or about.get("currency"), "by_carrier": page.by_carrier or None, "by_stops": page.by_stops or None}
    steps = [Next("detail", {"search": page.token, "id": page.rows[0]["id"]},
                  "see the first itinerary's fare basis, booking classes, taxes and segments"),
             Next("detail", {"search": page.token, "id": "<id>"}, "see another itinerary's detail")] \
        if page.rows else list(empty_steps)
    if not more and page.page < page.pages:
        steps.append(Next("results", {"search": page.token, "page": page.page + 1, "limit": limit, "sort": sort},
                          "read the next page of this search (fast: Matrix keeps the search)"))
    if page.rows and not with_operators:
        steps.append(Next("results", {"search": page.token, "page": page.page, "limit": min(limit, 10), "sort": sort,
                                      "operators": True},
                          "see who operates each flight (codeshares; about a second per row)"))
    fields = ("id", "price", "duration", "itinerary") if sort == "duration" else ("id", "price", "itinerary")
    if with_operators:
        fields += ("operators",)
    table = [{**{k: v for k, v in r.items() if k not in ("slices", "matrix_id")},   # --json only
              "stops": " | ".join(map(str, r["stops"])) if isinstance(r["stops"], list) else r["stops"]}
             for r in page.rows]
    return Listing(name="itineraries", rows=table, fields=fields, about=about, total=page.total,
                   data={**{k: v for k, v in about.items() if v is not None}, "total": page.total,
                         "itineraries": page.rows},
                   more={"page": page.page + 1} if more and page.page < page.pages else None,
                   empty="Matrix found no itineraries; widen the search (other dates, --flex, more stops, "
                         "another cabin or nearby airports)",
                   next=steps)


class Trip(NamedTuple):
    make: Callable[..., list[dict]]     # make(routing, extension, origins=None, dests=None) -> slices
    route: str
    dates: str
    ends: tuple[list[str], list[str], str, str | None] | None   # origins, dests, out, back; None for --legs


def _plan(origin: str | None, dest: str | None, depart: str | None, return_: str | None,
          legs: list[str] | None, flex: int = 0, *, what: str = "search") -> Trip:
    if legs:
        if origin or dest or depart or return_:
            raise ArgumentError("--legs replaces --origin, --dest, --depart and --return: give one or the other")
        parts = []
        for leg in legs:
            m = _LEG.match(leg.replace(" ", ""))
            if not m:
                raise ArgumentError(f"--legs {leg!r}: write each slice as ORIGIN-DEST:YYYY-MM-DD (JFK-NRT:2027-02-16)")
            parts.append((_codes(m[1], "--legs"), _codes(m[2], "--legs"), _date(m[3], "--legs")))

        def make(routing: str | None, extension: str | None, origins=None, dests=None) -> list[dict]:
            return [_slice(a, b, day, flex=flex, routing=routing, extension=extension) for a, b, day in parts]
        route = " | ".join(f"{','.join(a)}→{','.join(b)}" for a, b, _ in parts)
        return Trip(make, route, " | ".join(day for *_, day in parts), None)
    missing = [f for f, v in (("--origin", origin), ("--dest", dest), ("--depart", depart)) if not v]
    if missing:
        raise ArgumentError(f"{what} needs {', '.join(missing)} (or --legs for multi-city)")
    o, d = _codes(origin, "--origin"), _codes(dest, "--dest")
    out = _date(depart, "--depart")
    back = _date(return_, "--return") if return_ else None
    if back and back < out:
        raise ArgumentError(f"--return {back} is before --depart {out}")

    def make(routing: str | None, extension: str | None, origins=None, dests=None) -> list[dict]:
        a, b = origins or o, dests or d
        there = [_slice(a, b, out, flex=flex, routing=routing, extension=extension)]
        return there + ([_slice(b, a, back, flex=flex, routing=routing, extension=extension)] if back else [])
    route = f"{','.join(o)}→{','.join(d)}" + (f"→{','.join(o)}" if back else "")
    return Trip(make, route, out + (f" – {back}" if back else ""), (o, d, out, back))


@action(matrix, tier=Tier.READ)
def airports(ctx, query: str, limit: int = 10):
    """Find airport and city codes by name, as Matrix's search box does.

    A city code (TYO, NYC, LON) covers all of that city's airports in `search` and `calendar`.

    Args:
        query: Part of a city or airport name, or a code (tokyo, heathrow, SFO).
        limit: Rows to return (max 50).
    """
    if not query.strip():
        raise ArgumentError("--query is empty: give part of a city or airport name")
    if limit > 50:
        raise ArgumentError(f"--limit {limit}: Matrix returns at most 50 matches")
    data = ctx.http.get_json(f"{API}/locationTypes/CITIES_AND_AIRPORTS/partialNames/{quote(query.strip(), safe="")}/locations",
                             params={"pageSize": limit})
    rows = location_rows(data)
    top = rows[0]["code"] if rows else "<code>"
    return Listing(name="locations", rows=rows, fields=("code", "name", "type"),
                   about={"query": query}, empty=f"Matrix knows no city or airport matching {query!r}",
                   next=[Next("search", {"origin": top, "dest": "<code>", "depart": "<YYYY-MM-DD>"},
                              f"search fares from {top} (or use it as --dest)")])


@action(matrix, tier=Tier.READ)
def search(ctx, origin: str | None = None, dest: str | None = None, depart: str | None = None,
           return_: str | None = None, legs: list[str] | None = None, cabin: Cabin = "economy",
           stops: int | None = None, flex: int = 0, routing: str | None = None, extension: str | None = None,
           adults: int = 1, children: int = 0, currency: str | None = None, sales_city: str | None = None,
           sort: Sort = "price",
           limit: int = 25, operators: bool = False, thorough: bool = False, carriers: str | None = None):
    """Search fares for specific dates: one way, round trip, or multi-city.

    Rows are whole itineraries (every slice, `|` between them) with the total price for all
    passengers, cheapest first unless `--sort` says otherwise. Each slice reads
    `ORIGIN date time → DEST date time · duration · via STOPS · flights`, in local times.
    A search takes 20-60 seconds. Its `search` value lets `results` page or re-sort it and
    `detail` open one itinerary, both in about a second, without searching again.

    A row's `id` is a small number that `detail` takes together with the `search` value. It names
    the itinerary, not its position: it stays the same across sorts and pages, so the first row
    is not necessarily `1`. Flight
    numbers are the ones on the ticket; `--operators` adds who actually flies each codeshare
    (one more request, about a second, per row, so keep `--limit` small with it).

    `by_carrier` gives the cheapest price per marketing carrier (`multiple` = itineraries mixing
    carriers) and `by_stops` per number of stops. The `stops` field has one number per slice
    (`1 | 0`: one stop out, nonstop back).

    Matrix routing language and extension codes go in `--routing` and `--extension` and apply to
    every slice; for different rules per slice, use `--legs` for each slice on its own search.

    Matrix answers an open search within a compute budget and can leave out whole carriers, often
    the cheapest. `--thorough` looks for them: it splits each city code into its airports,
    searches every origin and destination airport pair, then searches each pair again without
    the carriers that pair found so far (`-AIRLINES …`, up to 3 searches a pair, until one finds
    no new carrier); `--carriers KE,PR` adds one search per carrier (`KE+`). The searches run side
    by side (1-2 minutes in all) and their rows merge, sorted, without duplicates. `searches`
    above the rows names each search and `failed` any that failed (the rest still count). Each
    row names the search that found it (`found_by`) and that search's own `search` value, which
    `detail` and `results` take with the row's `id`: the merged list has no `search` value of its
    own, since nothing is kept between calls.

    Args:
        origin: Origin airport or city codes, comma-separated (JFK, NYC, JFK,EWR).
        dest: Destination airport or city codes, comma-separated.
        depart: Outbound date, YYYY-MM-DD.
        return_: Return date for a round trip, YYYY-MM-DD.
        legs: Multi-city slices instead of origin/dest/depart, each ORIGIN-DEST:YYYY-MM-DD (JFK-NRT:2027-02-16 NRT-HKG:2027-02-20).
        cabin: economy, premium (premium economy), business or first. Short connecting flights may be in another cabin; `detail` shows each segment's.
        stops: Most stops per slice: 0 (nonstop), 1 or 2. Default any.
        flex: Also search this many days before and after each date (0-2).
        routing: Matrix routing language, e.g. `BR+` (EVA flight numbers only, codeshares included), `O:BR+` (flights EVA operates), `BR TPE BR` (EVA via Taipei).
        extension: Matrix extension codes, e.g. `f bc=I|bc=J` (every flight in booking class I or J), `f ~(bc=K|bc=S)` (neither K nor S).
        adults: Adult passengers.
        children: Child passengers (2-11 years old), priced at the child's fare.
        currency: Price currency, ISO code (USD, EUR, JPY). Default: Matrix's choice for the sales city.
        sales_city: Point-of-sale city code; changes which fares are offered.
        sort: price (cheapest first), duration, departure or arrival.
        limit: Itineraries per page (max 500); with --thorough, per search and in the merged list.
        operators: Also show who operates each flight (about a second more per row).
        thorough: Find the carriers an open search leaves out: search each airport pair, then again without the carriers found (several searches side by side, 1-2 minutes).
        carriers: With --thorough, also search each of these carriers on its own, at most 10: 2-character codes, comma-separated (KE,PR,CX); carriers the open searches already find need no search of their own. `esum flightconnections route` and `connections` list who flies a route.
    """
    if limit > 500:
        raise ArgumentError(f"--limit {limit}: Matrix pages at most 500 itineraries")
    if not 0 <= flex <= 2:
        raise ArgumentError(f"--flex {flex}: Matrix searches 0 to 2 days around a date; for a wider window use "
                            f"`esum matrix calendar`")
    given = {"origin": origin, "dest": dest, "depart": depart, "return_": return_, "cabin": cabin, "stops": stops,
             "flex": flex, "routing": routing, "extension": extension, "adults": adults, "children": children or None,
             "currency": currency,
             "sales_city": sales_city, "sort": sort, "limit": limit}
    wanted = _carrier_codes(carriers) if carriers else []
    if wanted and not thorough:
        again = command_line(ctx.site, ctx.action, {**given, "thorough": True, "carriers": carriers},
                             placeholders=False)
        raise ArgumentError("--carriers adds searches to a thorough search: add --thorough", help=f"Run `{again}`")
    if thorough and legs:
        raise ArgumentError("--thorough searches one origin and destination (and the return), not --legs",
                            help="Search each slice on its own with --origin, --dest and --depart")
    if wanted and routing:
        raise ArgumentError("--carriers searches each carrier with its own routing (`KE+`), so it can't take "
                            "--routing as well", help="Drop --routing, or drop --carriers and write the routing yourself")
    make, route, dates, ends = _plan(origin, dest, depart, return_, legs, flex)
    slices = make(routing, extension)
    if ends:
        o, d, out, back = ends

        def trip(origins: list[str], dests: list[str], *, routing: str | None = routing,
                 extension: str | None = extension) -> list[dict]:
            """The outbound slice, and the return when there is one, with these codes and rules."""
            return make(routing, extension, origins, dests)
    about = {"route": route, "dates": dates + (f" (±{flex} days)" if flex else ""), "cabin": cabin,
             "stops": "any" if stops is None else stops, "routing": routing, "extension": extension,
             "adults": adults if adults != 1 else None, "children": children or None, "sales_city": sales_city,
             "currency": currency.strip().upper() if currency else None}

    def body(slices: list[dict]) -> dict:
        return {"summarizers": SEARCH_SUMMARIZERS,
                "inputs": _inputs(slices, cabin=cabin, stops=stops, adults=adults, children=children, currency=currency,
                                  sales_city=sales_city, sort=sort, limit=limit),
                "summarizerSet": "wholeTrip", "name": "specificDatesSlice"}

    empty_steps = [] if legs else [_calendar_step(o, d, out, back, cabin=cabin, stops=stops, routing=routing,
                                                  extension=extension, adults=adults, children=children,
                                                  currency=currency,
                                                  sales_city=sales_city)]
    if thorough:
        return _thorough(ctx, o, d, trip, body, wanted, about, {**given, "carriers": carriers}, operators,
                         empty_steps)
    page = solution_page(_post(ctx, "search", body(slices), timeout=SEARCH_TIMEOUT))
    if not page.rows:
        _check_codes(ctx, slices)
    elif operators:
        _add_operators(ctx, page.token, page.rows)
    listing = _page_listing(page, about, sort, limit, more=False, empty_steps=empty_steps, with_operators=operators)
    if not legs and page.rows:
        if page.total <= FEW_ROWS:
            listing.next.append(Next("search", {**given, "thorough": True},
                                     f"look for the carriers Matrix left out: it found only {page.total} "
                                     f"itinerar{'y' if page.total == 1 else 'ies'}, and an open search can leave "
                                     f"out whole carriers (several searches, 1-2 minutes)"))
        listing.next += _route_steps(page.rows, given)
    return listing


FEW_ROWS = 24           # an open search pricing under a page was cut short: HNL-BJS priced 4 and JFK-TYO 8,
                        # and both missed cheaper carriers (2026-11-13)
THOROUGH_ROUNDS = 3     # searches per airport pair: the open one, then up to 2 without the carriers found
THOROUGH_PAIRS = 6      # airport pairs a thorough search takes
THOROUGH_CARRIERS = 10  # --carriers codes, one search each
THOROUGH_WORKERS = 8    # searches in flight at once; the site throttle still spaces their starts
_CARRIER = re.compile(r"[A-Z0-9]{2}")


def _carrier_codes(value: str) -> list[str]:
    codes = list(dict.fromkeys(c for c in value.replace(" ", "").upper().split(",") if c))
    if not codes or any(not _CARRIER.fullmatch(c) for c in codes):
        raise ArgumentError(f"--carriers {value!r}: give 2-character airline codes, comma-separated (KE,PR,CX)")
    if len(codes) > THOROUGH_CARRIERS:
        raise ArgumentError(f"--carriers names {len(codes)} carriers; give at most {THOROUGH_CARRIERS} "
                            f"(one search each)")
    return codes


def _calendar_step(o: list[str], d: list[str], out: str, back: str | None, **filters) -> Next:
    """One date found nothing: the calendar shows which dates nearby have fares."""
    around = date.fromisoformat(out)
    # Matrix counts nights from arrival, which a date-line route puts up to two days later
    n = (date.fromisoformat(back) - around).days if back else None
    nights = None if n is None else f"{max(n - 2, 0)}-{n}" if n > 0 else "0"
    return Next("calendar", {"origin": ",".join(o), "dest": ",".join(d),
                             "start": max(around - timedelta(days=7), date.today()).isoformat(),
                             "end": (around + timedelta(days=7)).isoformat(), "nights": nights, **filters},
                "see which dates around it have fares", when_empty=True)


def _route_steps(rows: list[dict], given: dict) -> list[Next]:
    """FlightConnections knows who flies the route; its carriers come back here as --carriers."""
    first = rows[0]["slices"][0]
    pair = {"origin": first["origin"], "dest": first["destination"]}
    steps = [Next("route", pair, f"see which airlines fly {pair['origin']}-{pair['dest']} nonstop",
                  site="flightconnections"),
             Next("connections", pair, "see the one-stop hubs and the airlines flying both legs",
                  site="flightconnections")]
    if not given.get("routing") and not given.get("carriers"):
        steps.append(Next("search", {**given, "thorough": True, "carriers": "<codes>"},
                          "also search each of those airlines on its own (KE,PR)"))
    return steps


def _airports(ctx, code: str) -> list[str]:
    """A city code's airports (BJS: PEK, PKX); an airport code is itself."""
    found = ctx.http.get_json(f"{API}/locationTypes/CITIES_AND_AIRPORTS/partialNames/{code}/locations",
                              params={"pageSize": 10}).get("locations") or []
    city = next((loc for loc in found if loc.get("code") == code and loc.get("type") == "city"), None)
    if not city or not city.get("cityName"):
        return [code]
    named = ctx.http.get_json(f"{API}/locationTypes/CITIES_AND_AIRPORTS/partialNames/"
                              f"{quote(city['cityName'], safe='')}/locations", params={"pageSize": 50})
    return city_airports(named, code) or [code]


def _thorough(ctx, o: list[str], d: list[str], trip, body, wanted: list[str], about: dict, given: dict,
              operators: bool, empty_steps: list[Next]) -> Listing:
    """Several searches whose rows merge: each airport pair, then each pair again without the
    carriers it found so far, plus one search per wanted carrier."""
    started = time.monotonic()
    origins = list(dict.fromkeys(a for c in o for a in _airports(ctx, c)))
    dests = list(dict.fromkeys(a for c in d for a in _airports(ctx, c)))
    pairs = [(a, b) for a in origins for b in dests if a != b]
    if len(pairs) > THOROUGH_PAIRS:
        raise ArgumentError(f"--thorough: {', '.join(origins)} to {', '.join(dests)} is {len(pairs)} airport pairs; "
                            f"it searches at most {THOROUGH_PAIRS} (up to {THOROUGH_ROUNDS} searches each)",
                            help="Give fewer airports in --origin or --dest (an airport code instead of a city code)")
    extension, sort, limit = given["extension"], given["sort"], given["limit"]

    def pair_rounds(a: str, b: str) -> list[tuple[str, SolutionPage | str]]:
        """One pair's searches in turn, each without every carrier the pair found before it."""
        done, seen = [], []
        for _ in range(THOROUGH_ROUNDS):
            left_out = f"-AIRLINES {' '.join(seen)}" if seen else ""
            ext = "; ".join(x for x in (extension, left_out) if x) or None
            found = _try_search(ctx, body(trip([a], [b], extension=ext)))
            done.append((f"{a}-{b} {left_out}".strip(), found))
            new = [] if isinstance(found, str) else [c for c in found.carriers if c not in seen]
            if not new:
                break
            seen += new
        return done

    jobs = [lambda a=a, b=b: pair_rounds(a, b) for a, b in pairs]
    jobs += [lambda c=c: [(f"{c}+", _try_search(ctx, body(trip(o, d, routing=f"{c}+"))))] for c in wanted]
    with ThreadPoolExecutor(max_workers=min(THOROUGH_WORKERS, len(jobs))) as pool:
        ran = [step for steps in pool.map(lambda job: job(), jobs) for step in steps]
    seconds = time.monotonic() - started
    failed = [(label, why) for label, why in ran if isinstance(why, str)]
    pages = [(label, page) for label, page in ran if not isinstance(page, str)]
    if not pages:
        raise SiteRefused(f"Matrix: all {len(ran)} searches failed: "
                          + "; ".join(f"{label}: {why}" for label, why in failed))
    rows, carrier_min, stops_min = _merge(pages)
    rows.sort(key=_ORDER[sort])
    shown = rows[:limit]
    if not shown:
        _check_codes(ctx, trip(o, d))
    elif operators:
        _add_operators(ctx, "", shown)
    about = {**about, "airports": f"{', '.join(origins)} to {', '.join(dests)}",
             "searches": f"{len(ran)} in {seconds:.0f} s"
                         + (f", {len(failed)} failed" if failed else "") + ": " + ", ".join(label for label, _ in ran),
             "failed": "; ".join(f"{label}: {why}" for label, why in failed) or None, "sort": sort,
             "currency": next((p.currency for _, p in pages if p.currency), None) or about.get("currency"),
             "by_carrier": summary(carrier_min, by_price=True) or None, "by_stops": summary(stops_min) or None}
    steps = list(empty_steps)
    if shown:
        top = shown[0]
        steps = [Next("detail", {"search": top["search"], "id": top["id"]},
                      "see the first itinerary's fare basis, booking classes, taxes and segments"),
                 Next("detail", {"search": "<search>", "id": "<id>"},
                      "see another itinerary, with that row's own search and id"),
                 Next("results", {"search": top["search"]},
                      f"page through the search that found the first row ({top['found_by']})"),
                 *_route_steps(shown, given)]
    fields = ("id", "price", "duration", "itinerary") if sort == "duration" else ("id", "price", "itinerary")
    fields += ("operators",) * operators + ("found_by", "search")
    table = [{**{k: v for k, v in r.items() if k not in ("slices", "matrix_id")},
              "stops": " | ".join(map(str, r["stops"])) if isinstance(r["stops"], list) else r["stops"]}
             for r in shown]
    return Listing(name="itineraries", rows=table, fields=fields, about=about, total=len(rows),
                   data={**{k: v for k, v in about.items() if v is not None}, "total": len(rows),
                         "failed": [{"search": label, "error": why} for label, why in failed],
                         "itineraries": shown},
                   empty="Matrix found no itineraries in any of the searches; widen the search (other dates, "
                         "--flex, more stops or another cabin)",
                   next=steps)


def _merge(pages: list[tuple[str, SolutionPage]]) -> tuple[list[dict], dict[str, str], dict[str, str]]:
    """Every search's rows once (the same flights at the same price are one itinerary, kept with the
    first search that found it), and each carrier's and stop count's cheapest price over them all."""
    rows: dict[tuple[str, str], dict] = {}
    carrier_min: dict[str, str] = {}
    stops_min: dict[str, str] = {}
    for label, page in pages:
        for row in page.rows:
            rows.setdefault((row["itinerary"], row["price"]), {**row, "found_by": label, "search": page.token})
        for mins, found in ((carrier_min, page.carrier_min), (stops_min, page.stops_min)):
            for key, price in found.items():
                if key not in mins or amount(price) < amount(mins[key]):
                    mins[key] = price
    by_stops = sorted(stops_min.items(), key=lambda kv: (kv[0] != "nonstop", kv[0]))
    return list(rows.values()), carrier_min, dict(by_stops)


_ORDER = {
    "price": lambda r: amount(r["price"]),
    "duration": lambda r: sum(s.get("duration") or 0 for s in r["slices"]),
    "departure": lambda r: (r["slices"][0]["departure"] or "") if r["slices"] else "",
    "arrival": lambda r: (r["slices"][-1]["arrival"] or "") if r["slices"] else "",
}


LADDER_SEARCHES = 6
LADDER_PAGE = 50      # itineraries a search lists; the ladder reads one per price
LADDER_WIDTH = 6      # itineraries of one search read, a few seconds each, side by side
_SEARCH_ERRORS = (HttpError, SiteRefused, ArgumentError, *TRANSIENT_ERRORS, ValueError)


@action(matrix, tier=Tier.READ)
def ladder(ctx, origin: str | None = None, dest: str | None = None, depart: str | None = None,
           return_: str | None = None, legs: list[str] | None = None, cabin: Cabin = "economy",
           stops: int | None = None, routing: str | None = None, extension: str | None = None,
           adults: int = 1, children: int = 0, currency: str | None = None, sales_city: str | None = None,
           searches: int = 3,
           skip: str | None = None, until: str | None = None):
    """A trip's dearer fares, and the cheapest that meets a condition: one rung per row, cheapest first.

    A search lists each itinerary once, at its cheapest fare, so the dearer fares on the same
    flights (one that isn't basic, a refundable one) stay hidden. The ladder searches, reads the
    first itinerary at each price (up to 6), leaves fares out of the slices they priced, by
    carrier, city pair and fare basis code with its designator (`1:UA.SFO+TYO.KLX07JBH`,
    `2:UA.TYO+SFO.SLW47FBH`), and searches again. A search takes 20-60 seconds; `help` gives
    the command for the next ones.

    `--until` is the condition, a test of each fare:

    - `not-basic`: the fare's rule title doesn't call it basic.
    - `refundable`: it states a cancellation term, and none says non-refundable.
    - any other words (`"CHANGES PERMITTED"`): its cancellation or change statements hold
      them, any case. Take the words from the penalties column.

    A rung fits when none of its fares fails. Only the fares that fail are left out, so a fare
    that passes can pair with another in the next search, and leaving a failing fare out never
    hides a rung that fits. The ladder stops when the cheapest itinerary of a search fits:
    every cheaper one held a failing fare. A rung that fits further down a search is a
    candidate, which the next search confirms or beats. Each slice gets its own filter, so the
    same code flown the other way stays, and a code with a designator (`JX2WC/CH`) is left out
    apart from the same code with another or none. Matrix's filter takes 10 characters of a
    code: a longer one goes out with the code's other designators, and `caution` names it,
    since a cheaper rung can sit behind it. It also says when Matrix lists a fare its filter
    left out. A fare Matrix doesn't place on a slice is printed without one (`UA.SFO+TYO.K…`)
    and left out of every slice. The answer is the cheapest among the fares Matrix lists, not
    a proof.

    Without `--until` every fare is left out once read: a list of the fares on the trip, where
    an itinerary that reuses one of them is not found.

    A row gives the rung's price, booking classes (` | ` between slices, outbound first), fare
    basis codes, kind, notes and penalties. Kind is read from each fare's rule title (basic,
    sell-up, or blank). Notes are Matrix's own ticket notes (non-refundable, change fee); they
    can say change fee where the rules file none. Penalties quotes the first statements each
    fare files under `CANCELLATIONS` and `CHANGES` in category 16 (` | ` between fares that
    differ), cut at 160 characters a section: the charge or the refusal, without the notes
    under it. `in the notes` stands for a time whose terms are all notes, `as above` for a
    rung that files what the row above does. For the whole text, `id` and `search` open the
    rung in `detail` and `rules`.

    Kind also names a slice flown wholly in a higher cabin than asked (`R is premium`): that
    rung is not the asked cabin, it never fits, and the fares that price that slice's flights
    are left out. A business slice with a domestic connection in first is a business slice. When a later
    search fails, the rungs found so far are printed, with `stopped` saying why.

    Args:
        origin: Origin airport or city codes, comma-separated (JFK, NYC, JFK,EWR).
        dest: Destination airport or city codes, comma-separated.
        depart: Outbound date, YYYY-MM-DD.
        return_: Return date for a round trip, YYYY-MM-DD.
        legs: Multi-city slices instead of origin/dest/depart, each ORIGIN-DEST:YYYY-MM-DD.
        cabin: economy, premium (premium economy), business or first.
        stops: Most stops per slice: 0 (nonstop), 1 or 2. Default any.
        routing: Matrix routing language, e.g. `UA+` (United flight numbers only), `O:UA+` (flights United operates).
        extension: Matrix extension codes for every search; the ladder adds its own fare filter after them.
        adults: Adult passengers.
        children: Child passengers (2-11 years old), priced at the child's fare.
        currency: Price currency, ISO code (USD, EUR, JPY).
        sales_city: Point-of-sale city code; changes which fares are offered.
        searches: Most searches to run, 1-6, each 20-60 seconds: 3 fit a 4-minute timeout, 6 need 8 minutes. The ladder stops sooner when a rung fits.
        skip: Fares to leave out from the start, comma-separated, as an earlier ladder printed them (1:UA.SFO+TYO.KLX07JBH, on slice 1), or shorter: on every slice (UA.SFO+TYO.KLX07JBH), in any market (UA.KLX07JBH), on any carrier (KLX07JBH). A code without a designator is that code with none.
        until: The condition to stop at: not-basic, refundable, or words every fare must file under CANCELLATIONS or CHANGES ("CHANGES PERMITTED").
    """
    if not 1 <= searches <= LADDER_SEARCHES:
        raise ArgumentError(f"--searches {searches}: run 1 to {LADDER_SEARCHES} searches a call; continue with --skip")
    skipped = list(dict.fromkeys(c.upper() for c in (skip or "").replace(" ", "").split(",") if c))
    # a code with no carrier needs three characters: `K` and `KS` were booking classes once, and are a slip
    if any((s := Skip.of(c)) is None or (not s.carrier and len(s.basis.split("/")[0]) < 3) for c in skipped):
        raise ArgumentError(f"--skip {skip!r}: give fares as the ladder prints them (1:UA.SFO+TYO.KLX07JBH), or "
                            f"without the slice, market or carrier (UA.KLX07JBH, KLX07JBH), comma-separated")
    trip = _plan(origin, dest, depart, return_, legs, what="ladder")
    count = len(trip.make(None, None))
    if any((s := Skip.of(c)).slice is not None and not 1 <= s.slice <= count for c in skipped):
        raise ArgumentError(f"--skip {skip!r}: this trip has {count} slice{'s' * (count != 1)}; a fare's "
                            f"`N:` names the slice it is left out of")
    try:
        goal = condition(until)
    except ValueError:
        raise ArgumentError("--until needs not-basic, refundable, or the words to look for as the penalties column "
                            "prints them (\"CHANGES PERMITTED\")") from None
    given = {"origin": origin, "dest": dest, "depart": depart, "return_": return_, "legs": legs, "cabin": cabin,
             "stops": stops, "routing": routing, "extension": extension, "adults": adults,
             "children": children or None, "currency": currency,
             "sales_city": sales_city, "searches": searches, "until": until}
    climb = Climb(goal, skipped)
    rules: dict = {}                    # each fare's rules, read once
    failed, top_out, stuck = None, False, False
    searched, started = 0, time.monotonic()
    for n in range(1, searches + 1):
        slices = trip.make(routing, None)
        for number, piece in enumerate(slices, 1):     # each slice leaves out the fares that failed on it
            line = "; ".join(e for e in (extension, without(climb.left_out, number)) if e)
            if line:
                piece["commandLine"] = line
        body = {"summarizers": SEARCH_SUMMARIZERS,
                "inputs": _inputs(slices, cabin=cabin, stops=stops, adults=adults, children=children, currency=currency,
                                  sales_city=sales_city, limit=LADDER_PAGE),
                "summarizerSet": "wholeTrip", "name": "specificDatesSlice"}
        searched += 1
        try:
            page = solution_page(_post(ctx, "search", body, timeout=SEARCH_TIMEOUT))
            if not page.rows:
                if not climb.steps and not skipped:
                    _check_codes(ctx, slices)
                top_out = bool(climb.steps or skipped)
                break
            picks = []      # the first itinerary at each price
            for r in page.rows:
                if len(picks) < LADDER_WIDTH and r["price"] not in {p["price"] for p in picks}:
                    picks.append({**r, "search": page.token})

            def read(row: dict) -> Rung | None:
                try:
                    return _rung(ctx, row, cabin, rules, needed=bool(goal.label))
                except _SEARCH_ERRORS:
                    if row is picks[0]:
                        raise
                    return None     # a rung further down is a bonus
            with ThreadPoolExecutor(max_workers=len(picks)) as pool:
                read_all = list(zip(picks, pool.map(read, picks)))
        except _SEARCH_ERRORS as e:
            if not climb.steps:     # nothing to keep: the error is the answer
                raise
            failed = f"search {n} failed ({type(e).__name__}: {e})"
            break
        if not climb.take(read_all):
            stuck = climb.answer is None
            break
    steps = climb.rungs()
    best = climb.answer or climb.candidate
    about = {"route": trip.route, "dates": trip.dates, "cabin": cabin, "stops": "any" if stops is None else stops,
             "routing": routing, "extension": extension, "adults": adults if adults != 1 else None,
             "children": children or None,
             "sales_city": sales_city, "skipped": ",".join(skipped) or None,
             "searches": f"{searched} in {time.monotonic() - started:.0f} s",
             "stopped": _ended(climb, steps, failed=failed, top_out=top_out, stuck=stuck),
             "caution": _caution(climb)}
    hints = []
    if steps:
        hints = [Next("detail", {"search": "<search>", "id": "<id>"},
                      "see a rung's flights, cabins, fare calculation and taxes, with its own search and id")]
        to_read = best or (None if goal.label else steps[-1])
        if to_read:
            hints.insert(0, Next("rules", {"search": to_read.row["search"], "id": to_read.row["id"],
                                           "category": "0,16,33"},
                                 f"read rung {to_read.number}'s filed title, penalties and refund rules (31 for "
                                 f"changes)"))
        if climb.answer and climb.answer.row.get("google_fares"):
            hints.append(Next("fares", climb.answer.row["google_fares"],
                              f"see what the airline sells on rung {climb.answer.number}'s flights now: each fare "
                              f"brand's price and terms (Google's booking page, a few seconds)", site="gflights"))
        if climb.answer:    # the rung that fits stays in a search, so climbing past it leaves its fares out too
            past = list(dict.fromkeys([*climb.left_out, *climb.answer.rung.skips]))
            hints.append(Next("ladder", {**given, "skip": ",".join(past)},
                              f"climb past rung {climb.answer.number}, when its rules don't bear it out: the next "
                              f"rungs without its fares"))
        elif not top_out and climb.left_out:
            hints.append(Next("ladder", {**given, "skip": ",".join(climb.left_out)},
                              "climb further: the next rungs, without the fares left out so far"))
    table = [{"rung": s.number, "price": s.row["price"], "classes": s.rung.classes,
              "fares": " ".join(dict.fromkeys(f.basis for f in s.rung.fares)), "kind": s.rung.kind,
              "notes": s.rung.notes, "penalties": s.rung.penalties, "itinerary": s.row["itinerary"],
              "id": s.row["id"], "search": s.row["search"]} for s in steps]
    shown = [{**r, "penalties": "as above"} if i and r["penalties"] and r["penalties"] == table[i - 1]["penalties"]
             else r for i, r in enumerate(table)]
    return Listing(name="rungs", rows=shown, about=about, total=None,
                   fields=("rung", "price", "classes", "fares", "kind", "notes", "penalties", "id", "search"),
                   data={**{k: v for k, v in about.items() if v is not None}, "left_out": climb.left_out,
                         "rungs": table},
                   empty=f"Matrix has nothing without {','.join(climb.left_out)}" if skipped else
                         "Matrix found no itineraries; widen the search (other dates, more stops, another cabin "
                         "or nearby airports)",
                   next=hints)


def _caution(climb: Climb) -> str | None:
    """Where the filter fell short of the fares the ladder meant to leave out."""
    short = []
    if climb.leaked:
        short.append(f"Matrix listed {', '.join(climb.leaked)} where the filter left it out, so the filter didn't "
                     f"hold for it")
    if climb.wide():
        hidden = climb.hidden() if climb.answer or climb.candidate else []
        short.append(f"a cheaper rung can exist: the filter can't name {', '.join(climb.wide())} alone (over 10 "
                     f"characters), so the code's other fares on the slice went out "
                     + (f"with it, {', '.join(hidden)} that meets the condition among them" if hidden else "unread"))
    return "; ".join(short) or None


def _ended(climb: Climb, steps: list, *, failed: str | None, top_out: bool, stuck: bool) -> str | None:
    goal = climb.condition
    if climb.answer:
        said = f"rung {climb.answer.number} {goal.fits}: confirm it in its rules, notes included"
        if "non-refundable" in climb.answer.rung.notes and goal is PRESETS["refundable"]:
            said += "; Matrix's note calls it non-refundable"
        return said
    if failed:
        return failed
    if top_out:
        return f"nothing left without {','.join(climb.left_out)}"
    if climb.candidate:
        return (f"rung {climb.candidate.number} fits ({goal.label}) and may not be the cheapest: a fare behind a "
                f"cheaper rung's flights can still fit. Climb further with the last help line")
    if stuck:
        return "the last search names no new fare to leave out, so the ladder can't climb further"
    if goal.words and steps and not climb.some_pass:
        return (f"no fare of {len(steps)} rungs files {goal.label}: check the words against the penalties column "
                f"before you climb further, since the last help line leaves out every fare seen")
    if goal.label and steps:
        return (f"{len(steps)} rungs climbed, none {'with ' * goal.words}{goal.label}: climb further with the last "
                f"help line")
    return None


def _rung(ctx, row: dict, cabin: str, rules: dict, *, needed: bool) -> Rung:
    """One itinerary of a search as a rung. Each fare's kind and category 16 come from its rules, a
    request a fare, kept in `rules` for the fares met again. When a request fails the fare goes
    unread, unless a condition needs it."""
    session, solution_set, _ = split_token(row["search"])
    details = _post(ctx, "summarize", {
        "summarizers": ["bookingDetails"], "inputs": {"solution": f"{solution_set}/{row['matrix_id']}"},
        "summarizerSet": "viewDetails", "solutionSet": solution_set, "session": session},
        search=row["search"], id=row["id"])
    data = booking(details).data
    row["google_fares"] = google_fares(data)
    read = {}
    for f in {f["key"]: f for f in data["fares"] if f["key"]}.values():
        known = (f["basis"], f["carrier"], f["market"])
        if known not in rules:
            try:
                sections = rule_sections(_rules(ctx, session, solution_set, row["matrix_id"], f["key"]))
            except _SEARCH_ERRORS:
                if needed:
                    raise
                continue
            rules[known] = (fare_traits(sections), "\n".join(s["text"] for s in sections if s["category"] == 16))
        read[f["key"]] = rules[known]
    return rung(data, cabin, read)


@action(matrix, tier=Tier.READ)
def results(ctx, search: str, page: int = 1, limit: int = 25, sort: Sort = "price", operators: bool = False):
    """Another page, or another order, of a search Matrix already ran (about a second, no new search).

    Matrix keeps a search for a while after it runs; when it no longer has it, run `search` again.
    `route` and `dates` in the header are read off the listed itineraries, slice by slice.

    Args:
        search: The `search` value a `search` result printed.
        page: Page number, starting at 1.
        limit: Itineraries per page (max 500).
        sort: price (cheapest first), duration, departure or arrival.
        operators: Also show who operates each flight (about a second more per row).
    """
    if limit > 500:
        raise ArgumentError(f"--limit {limit}: Matrix pages at most 500 itineraries")
    session, solution_set, prefix = _token(search)
    body = {"summarizers": SEARCH_SUMMARIZERS,
            "inputs": {"page": {"current": page, "size": limit}, "sorts": SORTS[sort]},
            "summarizerSet": "wholeTrip", "solutionSet": solution_set, "session": session}
    data = _post(ctx, "summarize", body, search=search)
    data.setdefault("session", session)
    data.setdefault("solutionSet", solution_set)
    found = solution_page(data, prefix)
    if found.pages and page > found.pages:
        raise ArgumentError(f"--page {page}: this search has {found.pages} page(s) of {limit}",
                            help=f"Read the last page with `esum matrix results --search {search} --page {found.pages}"
                                 + (f" --limit {limit}" if limit != 25 else "") + "`, or a smaller --limit for more pages")
    if operators and found.rows:
        _add_operators(ctx, found.token, found.rows)
    route, dates = rows_route(found.rows) if found.rows else (None, None)
    return _page_listing(found, {"route": route, "dates": dates}, sort, limit, more=True, with_operators=operators)


@action(matrix, tier=Tier.READ)
def detail(ctx, search: str, id: str):
    """One itinerary from a search: segments, aircraft, booking classes, fare basis codes, the fare
    calculation line, taxes and carrier surcharges, and ticket notes.

    Under each fare, a line says what its rules make it, when they say: an unbundled (basic)
    fare, a sell-up fare, or a dual booking class (the fare sells only while a second class has
    seats too). That reads each fare's rules, about a second per fare; `rules` prints them.

    A help line gives the `esum gflights fares` command for these flights: the fare brands the
    airline sells on them now, with prices (codeshares by their operating flight numbers). It
    is left out for a trip that command can't name: over two slices, children, or slices in
    different cabins.

    Args:
        search: The `search` value the `search` or `results` output printed.
        id: The itinerary's `id` from that list.
    """
    session, solution_set, prefix = _token(search)
    raw = _matrix_id(id, prefix, search)
    body = {"summarizers": ["bookingDetails"], "inputs": {"solution": f"{solution_set}/{raw}"},
            "summarizerSet": "viewDetails", "solutionSet": solution_set, "session": session}
    details = _post(ctx, "summarize", body, search=search, id=id)
    traits = {}
    for key in dict.fromkeys(f["key"] for f in booking(details).data["fares"] if f["key"]):
        try:   # the fare's kind is a bonus: detail stands without it
            traits[key] = fare_traits(rule_sections(_rules(ctx, session, solution_set, raw, key)))
        except (HttpError, SiteRefused, ArgumentError, *TRANSIENT_ERRORS, ValueError):
            pass
    b = booking(details, traits)
    text = "\n".join([f"Itinerary {id.strip()}: {b.title}", *b.lines])
    steps = [Next("rules", {"search": search, "id": id.strip()},
                  "read each fare's rules: booking classes, advance purchase, days, stays, change and cancel penalties")]
    named = google_fares(b.data)
    if named:
        steps.append(Next("fares", named, "see what the airline and the agencies sell on these flights now: each "
                                          "fare brand's price and terms (Google's booking page, a few seconds)",
                          site="gflights"))
    return Result(data={"search": search, "id": id.strip(), "matrix_id": raw, **b.data}, text=text, summary=b.title,
                  next=steps + _seat_steps(b.data["slices"]))


def _rules(ctx, session: str, solution_set: str, raw: str, key: str) -> dict:
    body = {"summarizers": ["fareRules"], "inputs": {"solution": f"{solution_set}/{raw}", "fareKeys": key},
            "summarizerSet": "viewRules", "solutionSet": solution_set, "session": session}
    return _post(ctx, "summarize", body, search=f"{session}.{solution_set}")


RULE_CATEGORIES = (0, 2, 3, 4, 5, 6, 7, 11, 14, 15, 16)   # whether, when and for whom the fare applies; penalties


@action(matrix, tier=Tier.READ)
def rules(ctx, search: str, id: str, fare: str | None = None, category: str | None = None):
    """The fare rules of one itinerary's fares, by ATPCO category, as Matrix's `rules` links show
    them: the filed text, unchanged.

    Each fare starts with what its general notes (category 0) say about its kind: an unbundled
    (basic) fare, a sell-up fare, and a dual booking class, where the fare books one class but
    sells only while another has seats too (a United N fare that also needs K): the fare
    disappears when either class sells out, and a change must find both open again. Several
    passengers on one booking share a fare, so the classes need seats for all of them.

    By default it prints categories 0, 2-7, 11, 14-16: booking classes, days and times, seasons,
    flights, advance purchase, minimum and maximum stay, blackout dates, travel and sales dates,
    and penalties. `also filed` names the fare's other categories and how long each is.

    Args:
        search: The `search` value the `search` or `results` output printed.
        id: The itinerary's `id` from that list.
        fare: Only this fare: its fare basis code (KAU2JKBS) or its number in `detail`'s Fares list (1, 2).
        category: Categories to print: numbers, comma-separated (16,31), or `all`.
    """
    if category is None:
        wanted = set(RULE_CATEGORIES)
    elif category.strip().lower() == "all":
        wanted = None
    else:
        try:
            wanted = {int(c) for c in category.replace(" ", "").split(",") if c}
        except ValueError:
            raise ArgumentError(f"--category {category!r}: give category numbers, comma-separated (16,31), or all") \
                from None
    session, solution_set, prefix = _token(search)
    raw = _matrix_id(id, prefix, search)
    body = {"summarizers": ["bookingDetails"], "inputs": {"solution": f"{solution_set}/{raw}"},
            "summarizerSet": "viewDetails", "solutionSet": solution_set, "session": session}
    fares = list({f["key"]: f for f in booking(_post(ctx, "summarize", body, search=search, id=id)).data["fares"]
                  if f["key"]}.values())
    if fare:
        pick = fare.strip()
        chosen = [fares[int(pick) - 1]] if pick.isdigit() and 0 < int(pick) <= len(fares) \
            else [f for f in fares if str(f["basis"]).upper() == pick.upper()]
        if not chosen:
            raise ArgumentError(f"--fare {fare!r}: this itinerary's fares are "
                                + ", ".join(f"{i} {f['basis']}" for i, f in enumerate(fares, 1)),
                                help="Give a fare basis code or a number from that list")
        fares = chosen
    lines, out = [], []
    for f in fares:
        sections = rule_sections(_rules(ctx, session, solution_set, raw, f["key"]))
        kind = fare_traits(sections)
        filed = sorted({s["category"] for s in sections})
        shown = [s for s in sections if wanted is None or s["category"] in wanted]
        lines += [f"Fare {f['basis']} · {f['carrier']} {f['market']} · booking class {f['classes']} · {f['price']} "
                  f"{str(f['type'] or '').lower()}"]
        if kind:
            lines.append(f"  {kind}")
        rest: dict[int, int] = {}
        for s in sections:
            if s not in shown:
                rest[s["category"]] = rest.get(s["category"], 0) + len(s["text"].splitlines())
        if rest:
            lines.append("  also filed: " + " · ".join(
                f"{c} {CATEGORIES.get(c, 'category')} ({n} lines)" for c, n in sorted(rest.items())))
        for s in shown:
            lines.append(f"  [{s['category']}] {s['name']}" + (f" ({s['kind']})" if s["kind"] else ""))
            lines += [f"    {line}" if line.strip() else "" for line in s["text"].splitlines()]
        missing = sorted(wanted - set(filed)) if wanted is not None and category is not None else []
        if missing:
            lines.append(f"  not filed: {' '.join(map(str, missing))}")
        lines.append("")
        out.append({**f, "kind": kind or None, "categories": filed, "rules": shown})
    steps = [] if category and category.strip().lower() == "all" else \
        [Next("rules", {"search": search, "id": id.strip(), "fare": fare, "category": "all"},
              "read every category the fares file (combinations and voluntary changes run long)")]
    title = f"Fare rules for itinerary {id.strip()}: " + ", ".join(f["basis"] for f in fares)
    return Result(data={"search": search, "id": id.strip(), "fares": out}, text="\n".join([title, "", *lines]).rstrip(),
                  summary=title, next=steps)


LONG_HAUL = 6 * 60          # minutes in the air: the flights whose seat is worth looking up
SEAT_STEPS = 2
AEROLOPA_DAYS = 120         # AeroLOPA's Super Schedule covers today and the next 120 days
AEROLOPA_CABINS = {"COACH": "economy", "PREMIUM-COACH": "premium-economy", "BUSINESS": "business", "FIRST": "first"}


def _today() -> date:
    return date.today()


def _seat_steps(slices: list[dict]) -> list[Next]:
    """AeroLOPA names the seat configuration a flight is scheduled on: one step per long flight (or
    the longest one), by its operating flight number, when its date is in AeroLOPA's window."""
    segments = [seg for s in slices for seg in s.get("segments") or []]
    long = [seg for seg in segments if (seg.get("duration") or 0) >= LONG_HAUL] \
        or sorted(segments, key=lambda seg: -(seg.get("duration") or 0))[:1]
    steps, today = [], _today()
    for seg in long[:SEAT_STEPS]:
        day = (seg.get("departure") or "")[:10]
        try:
            ahead = (date.fromisoformat(day) - today).days
        except ValueError:
            continue
        if not 0 <= ahead <= AEROLOPA_DAYS:
            continue
        op = seg.get("operationalFlight") or {}
        carrier = (op.get("carrier") or seg.get("carrier") or {}).get("code", "")
        number = op.get("number") or (seg.get("flight") or {}).get("number", "")
        cabin = next((AEROLOPA_CABINS.get(str(bi.get("cabin"))) for bi in seg.get("bookingInfos") or []), None)
        route = f"{seg['origin']['code']}-{seg['destination']['code']}"
        steps.append(Next("schedule", {"route": route, "date_": day, "flight": f"{carrier}{number}", "cabin": cabin},
                          f"see which seat configuration {carrier}{number} ({route}) is scheduled on, and its seats",
                          site="aerolopa"))
    return steps


@action(matrix, tier=Tier.READ)
def calendar(ctx, origin: str, dest: str, start: str, end: str | None = None, nights: str | None = None,
             cabin: Cabin = "economy", stops: int | None = None, routing: str | None = None,
             extension: str | None = None, adults: int = 1, children: int = 0, currency: str | None = None,
             sales_city: str | None = None, check: int = 1):
    """Lowest fare for each departure date in a range: Matrix's calendar of lowest fares, with the
    cheapest dates checked by a real search.

    Without `--nights` it prices one-way trips. With `--nights` it prices round trips of that many
    nights (or a range, 5-8), and each row gives the cheapest length, its return date and every
    length's price.

    Calendar prices come from a lighter search and can be far off: a date can show several times
    its real fare, or none at all (`no_fare`), and even `--flex` searches miss fares a search for
    the exact dates finds. So `--check N` (default 1) also runs that exact search for the N
    cheapest dates (with their return): `checked` holds the real cheapest price, and the help
    lines open that search's itineraries with `detail` and `results` straight away. The calendar
    takes 20-60 seconds and the check about as long again, however many dates it searches (they
    run side by side); `--check 0` skips it.
    The calendar also overprices dates now and then, so dates priced over three times the range's
    median are listed under `suspect`, with a help line to search one. `solutions` is how many
    itineraries the calendar priced for a date.

    Args:
        origin: Origin airport or city codes, comma-separated.
        dest: Destination airport or city codes, comma-separated.
        start: First departure date, YYYY-MM-DD.
        end: Last departure date, YYYY-MM-DD (default: 30 days from start).
        nights: Round-trip length in nights: 7, or a range such as 5-8. Leave out for one way.
        cabin: economy, premium (premium economy), business or first.
        stops: Most stops per slice: 0 (nonstop), 1 or 2. Default any.
        routing: Matrix routing language for every slice (see `search`).
        extension: Matrix extension codes for every slice (see `search`).
        adults: Adult passengers.
        children: Child passengers (2-11 years old), priced at the child's fare.
        currency: Price currency, ISO code.
        sales_city: Point-of-sale city code.
        check: Search this many of the cheapest dates for real, 0-5 (together about one search: 20-60 seconds).
    """
    if not 0 <= check <= 5:
        raise ArgumentError(f"--check {check}: give 0 (no check) to 5 dates")
    o, d = _codes(origin, "--origin"), _codes(dest, "--dest")
    first = _date(start, "--start")
    last = _date(end, "--end") if end else (date.fromisoformat(first) + timedelta(days=29)).isoformat()
    if last < first:
        raise ArgumentError(f"--end {last} is before --start {first}")
    opts = {"routing": routing, "extension": extension}
    slices = [_slice(o, d, None, **opts)]
    inputs_extra: dict = {"startDate": first, "endDate": last}
    hi = 0
    if nights:
        m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", nights)
        if not m or int(m[2] or m[1]) < int(m[1]):
            raise ArgumentError(f"--nights {nights!r}: give a number of nights (7) or a range (5-8)")
        lo, hi = int(m[1]), int(m[2] or m[1])
        slices.append(_slice(d, o, None, **opts))
        inputs_extra["layover"] = {"min": lo, "max": hi}
        # Matrix's end date bounds the return as well, so the last departures would get only a few
        # (dear) returns or none; let the trip end past --end (nights count from arrival, up to two
        # days later on date-line routes) and keep departures within the range
        inputs_extra["endDate"] = (date.fromisoformat(last) + timedelta(days=hi + 2)).isoformat()
        names = ("calendar", "calendarRoundTrip")
    else:
        names = ("calendarOneWay", "calendarOneWay")
    inputs = _inputs(slices, cabin=cabin, stops=stops, adults=adults, children=children, currency=currency,
                     sales_city=sales_city)
    inputs["page"] = {"size": 25}
    inputs.update(inputs_extra)
    body = {"summarizers": [names[0], "itineraryCarrierList", "itineraryStopCountList", "currencyNotice"],
            "inputs": inputs, "summarizerSet": names[1], "name": "calendar"}
    rows = [r for r in calendar_rows(_post(ctx, "search", body, timeout=SEARCH_TIMEOUT))
            if first <= r["date"] <= last]
    if not rows:
        _check_codes(ctx, slices)
    best = min(rows, key=lambda r: (amount(r["price"]), r["date"]), default=None)
    checked_about, check_steps = None, []
    picks = sorted((r for r in rows if not nights or r.get("return")),
                   key=lambda r: (amount(r["price"]), r["date"]))[:check]
    if check and rows and not picks:
        checked_about = "no date had a return date to search; prices are calendar estimates, so search a date first"
    if picks:
        def exact(r: dict):
            """A specific-date search is the exhaustive one; the calendar is not."""
            window = [_slice(o, d, r["date"], **opts)]
            if nights:
                window.append(_slice(d, o, r["return"], **opts))
            body = {"summarizers": SEARCH_SUMMARIZERS,
                    "inputs": _inputs(window, cabin=cabin, stops=stops, adults=adults, children=children,
                                      currency=currency,
                                      sales_city=sales_city, limit=5),
                    "summarizerSet": "wholeTrip", "name": "specificDatesSlice"}
            return _try_search(ctx, body)   # a failed check keeps the calendar

        # Matrix spends 20-60 s on each search, so the checks run side by side: the site throttle
        # still spaces their starts, and the whole check takes about as long as one search
        with ThreadPoolExecutor(max_workers=len(picks)) as pool:
            answers = list(pool.map(exact, picks))
        results_ok = []
        for r, found in zip(picks, answers):
            if isinstance(found, str):
                r["checked"] = found
                continue
            r["checked"] = found.rows[0]["price"] if found.rows else "no itinerary"
            if found.rows:
                results_ok.append((r, found))
        if results_ok:
            r, found = min(results_ok, key=lambda x: amount(x[1].rows[0]["price"]))
            ran = sum(not isinstance(a, str) for a in answers)
            checked_about = (f"{ran} date{'s' if ran != 1 else ''} searched for real; cheapest "
                             f"{found.rows[0]['price']} on {r['date']}" + (f" returning {r['return']}" if nights else "")
                             + "; other prices are calendar estimates")
            check_steps = [Next("detail", {"search": found.token, "id": found.rows[0]["id"]},
                                f"see that itinerary ({found.rows[0]['price']})"),
                           Next("results", {"search": found.token}, f"list the itineraries for {r['date']}")]
        elif all(isinstance(a, str) for a in answers):
            checked_about = "the real searches failed; prices are calendar estimates, so search a date first"
        else:
            checked_about = "the real searches found nothing; prices are calendar estimates, so search a date first"
    # the calendar sometimes prices a date several times its real fare (USD6876 for a USD909 trip)
    priced = sorted(amount(r["price"]) for r in rows)
    median = priced[len(priced) // 2] if priced else None
    suspect = [r["date"] for r in rows if median and "checked" not in r
               and amount(r.get("price")) > 3 * median and amount(r.get("price")) != float("inf")]
    have = {r["date"] for r in rows}
    days = (date.fromisoformat(last) - date.fromisoformat(first)).days + 1
    missing = [(date.fromisoformat(first) + timedelta(days=n)).isoformat() for n in range(days)]
    missing = [x for x in missing if x not in have]
    route = f"{','.join(o)}→{','.join(d)}" + (f"→{','.join(o)}" if nights else "")
    common = {"origin": ",".join(o), "dest": ",".join(d), "cabin": cabin, "stops": stops, "routing": routing,
              "extension": extension, "adults": adults, "children": children or None, "currency": currency,
              "sales_city": sales_city}
    steps = list(check_steps)
    if suspect:
        row = next(r for r in rows if r["date"] == suspect[0])
        steps.append(Next("search", {**common, "depart": row["date"], "return_": row.get("return")},
                          f"price a suspect date for real (the calendar says {row['price']})"))
    if best and not check_steps:
        steps.append(Next("search", {**common, "depart": best["date"], "return_": best.get("return")},
                          f"see the itineraries for the cheapest date ({best['price']})"))
    steps.append(Next("search", {**common, "depart": "<date>", "return_": "<return>"},
                      "see the itineraries for another date and its return")
                 if nights else Next("search", {**common, "depart": "<date>"}, "see the itineraries for another date"))
    fields = ("date", "price") if not nights else ("date", "return", "nights", "price") if lo == hi \
        else ("date", "return", "nights", "price", "by_nights")
    if checked_about:
        fields += ("checked",)
        for r in rows:
            r.setdefault("checked", "")   # not searched: the calendar estimate is all there is
    return Listing(name="days", rows=rows, fields=fields,
                   about={"route": route, "dates": f"{first} – {last}", "nights": (lo if lo == hi else nights) if nights else "one way",
                          "cabin": cabin, "stops": "any" if stops is None else stops, "routing": routing,
                          "extension": extension, "currency": currency_of(best["price"]) if best else None,
                          "cheapest": f"{_cheapest(rows, best)} (estimate)" if best else None,
                          "suspect": (f"{_ranges(suspect)}: over 3 times the range's median estimate; often a "
                                      f"calendar error, so search them") if suspect else None,
                          "checked": checked_about, "no_fare": _ranges(missing) or None, "sales_city": sales_city},
                   empty="Matrix found no fares in this range; try other dates, more stops or another cabin",
                   next=steps)


def _cheapest(rows: list[dict], best: dict) -> str:
    tied = [r["date"] for r in rows if r is not best and amount(r["price"]) == amount(best["price"])]
    more = f" (and {len(tied)} more date{'s' if len(tied) != 1 else ''} at this price: {', '.join(tied[:6])}" \
           f"{', …' if len(tied) > 6 else ''})" if tied else ""
    return f"{best['price']} on {best['date']}{more}"


def _ranges(days: list[str]) -> str:
    """`2027-03-28 – 2027-04-01, 2027-04-05` for consecutive ISO dates."""
    out, run = [], []
    for d in days:
        if run and date.fromisoformat(d) - date.fromisoformat(run[-1]) != timedelta(days=1):
            out.append(run)
            run = []
        run.append(d)
    if run:
        out.append(run)
    return ", ".join(r[0] if len(r) == 1 else f"{r[0]} – {r[-1]}" for r in out)