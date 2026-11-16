"""FlightConnections (flightconnections.com): which airlines fly a route nonstop and how often, its
timetable, one-stop connections, every nonstop destination from an airport, and an airline's network.

Read-only and public: a guest sees everything here. Every page is one the site's sitemaps list and
robots.txt allows (route, airport and airline pages), plus the JSON its route page's calendar asks
for another month (`POST /route_schedule.php`, a read). The map and its filters (`/rt*`,
`/airline_routes.php`, `/airports_url.php`, `/filter*`) are disallowed and never read, so
connections with two stops and the full route list of a large airline are out of reach (D-91).
Plain HTTP gets a 202 bot check page (AWS WAF); Chrome's TLS fingerprint (T2) gets the pages until
a burst, and a tab (T3) clears the check when it comes.
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date
from typing import Literal

from esum import ArgumentError, Listing, Next, NoSuchItem, Tier, Transport, action, one_line, other_site_command, site

from .parse import (BASE, MONTHS, PageChanged, Stopover, airline as parse_airline, airline_catalog, airport as parse_airport,
                    route as parse_route, schedule_answer, weekdays)

FORMAT = 1
EXTRAS = ()

flightconnections = site(
    name="flightconnections",
    base_url=BASE,
    transport=Transport.BROWSER_HTTP,
    fallback=Transport.IN_PAGE,     # a real tab solves the AWS WAF check T2 gets after a burst (D-91)
    label={"en": "FlightConnections: which airlines fly a route nonstop, how often and on which aircraft, its "
                 "timetable, one-stop connections, every nonstop destination from an airport, an airline's network",
           "zh": "FlightConnections：航线有哪些航司直飞、班次与机型、时刻表、一次中转方案、机场全部直飞航点、航司航线网络"},
    # the AWS WAF check page's script; T2 borrows the tab's `aws-waf-token` cookie afterwards
    # `/` is never checked, so a tab opens on a page that is and solves the check there
    extras={"challenge_text": "window.gokuProps", "browser_home": "/airlines", "min_interval": 3.0},
)

Alliance = Literal["oneworld", "skyteam", "star"]
ALLIANCES = {"oneworld": "Oneworld", "skyteam": "SkyTeam", "star": "Star Alliance"}
_AIRPORT = re.compile(r"[A-Za-z]{3}")
_AIRLINE = re.compile(r"[A-Za-z0-9]{2,3}")


# ---------------------------------------------------------------- inputs and fetching


def _find_code() -> str:
    """Where to look an airport code up: Matrix's lookup, when that site is installed."""
    lookup = other_site_command("matrix", "airports", {"query": "<city>"})
    return f"Find a code with `{lookup}`" if lookup else ""


def _airport(value: str, flag: str) -> str:
    text = value.strip()
    if not _AIRPORT.fullmatch(text):
        raise ArgumentError(f"{flag} {value!r}: give a 3-letter IATA airport code, e.g. JFK", help=_find_code())
    return text.upper()


def _month(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}", value.strip()):
        raise ArgumentError(f"--month {value!r}: give a month as YYYY-MM, e.g. 2026-12")
    year, month = map(int, value.strip().split("-"))
    if not 1 <= month <= 12:
        raise ArgumentError(f"--month {value!r}: give a month as YYYY-MM, e.g. 2026-12")
    return date(year, month, 1)


def _day(value: str) -> date:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
            raise ValueError
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ArgumentError(f"--date {value!r}: give a date as YYYY-MM-DD") from None


def _missing(message: str, help: str) -> NoSuchItem:
    error = NoSuchItem(message)
    error.help = help
    return error


def _page(ctx, path: str, missing: NoSuchItem) -> tuple[str, str]:
    """A page's HTML and final URL. A 404 is `missing`; the bot check's empty 202 is a refusal."""
    resp = _send(ctx, "GET", path)
    if resp.status == 404:
        raise missing
    if not resp.ok:
        raise resp.http_error()
    return resp.text(), resp.url


def _send(ctx, method: str, path: str, **kw):
    """One request, its status left to the caller. The bot check (AWS WAF) is the transport's: it
    lets T2 through until a burst, then its 202 moves the call to a tab, which solves the check and
    keeps the `aws-waf-token` cookie that T2 borrows on later calls (D-91)."""
    return ctx.http.request(method, path, check=False, **kw)


def _route(ctx, origin: str, dest: str, *, missing_ok: bool = False):
    path = f"/flights-from-{origin.lower()}-to-{dest.lower()}"
    try:
        html, url = _page(ctx, path, _missing(
            f"FlightConnections has no page for {origin} to {dest}: no nonstop or one-stop route it knows",
            f"Check both codes: they are airports, so a city code (TYO, NYC, LON) has no page; give each of its "
            f"airports (NRT, HND). `esum flightconnections connections --origin {origin} --dest {dest}` still "
            f"looks for one-stop hubs from the two airport pages"))
    except NoSuchItem:
        if missing_ok:
            return None
        raise
    return parse_route(html, url)


def _airport_page(ctx, code: str, direction: str):
    html, url = _page(ctx, f"/flights-{direction}-airport-{code.lower()}", _missing(
        f"FlightConnections has no airport {code}",
        f"Check the code (a 3-letter IATA airport code). {_find_code()}".rstrip()))
    return parse_airport(html, url, direction)


# ---------------------------------------------------------------- shared row helpers


def _codes(airlines: list[dict]) -> str:
    return " ".join(dict.fromkeys(a["code"] or a["name"] for a in airlines))


def _names(airlines: list[dict]) -> str:
    return ", ".join(a["name"] + (f" ({a['alliance']})" if a.get("alliance") else "") for a in airlines)


def _check_airline(ctx, wanted: str | None, matched: bool) -> None:
    """When an `--airline` filter matched no airline on the page, a name the site doesn't know is an
    error (NoSuchItem), not an empty list; a known airline just doesn't fly here."""
    if wanted and not matched:
        _airline_code(ctx, wanted, "--airline")


def _alliances(airlines: list[dict]) -> list[str]:
    return sorted({a["alliance"] for a in airlines if a.get("alliance")})


def _is_airline(wanted: str, code: str, name: str) -> bool:
    """`wanted` is the airline's code, the site's name for it, or a longer name that starts with the
    site's (`Alaska Airlines` for the site's `Alaska`)."""
    key, name = wanted.strip().casefold(), name.casefold()
    return key == code.casefold() or bool(name) and (key == name or key.startswith(name + " "))


def _flies(airlines: list[dict], wanted: str | None, alliance: str | None) -> list[dict]:
    """The airlines that match an `--airline` (code or name) and `--alliance` filter."""
    found = airlines
    if wanted:
        found = [a for a in found if _is_airline(wanted, a["code"], a["name"])]
    if alliance:
        found = [a for a in found if a.get("alliance") == ALLIANCES[alliance]]
    return found


def _share(tie: dict, wanted: str | None, alliance: str | None) -> tuple[int, int]:
    """Same-named airports a page gives one airline list each without saying which is which
    (`tie`): the filter's airlines fly to k of the n, k being the lists that name one."""
    return sum(1 for listed in tie["lists"] if _flies(listed, wanted, alliance)), len(tie["codes"])


def _flies_to(who: str, k: int, n: int) -> str:
    return f"{who} flies to " + ("both" if k == n == 2 else f"all {n}" if k == n else f"{k} of them")


def _window(rows: list[dict], offset: int, limit: int) -> tuple[list[dict], dict | None]:
    shown = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows[offset:offset + limit]]
    return shown, ({"offset": offset + limit} if offset + limit < len(rows) else None)


def _where(place) -> str:
    return one_line(f"{place.code} {place.city}, {place.country}")


def _today() -> date:
    return date.today()


def _ahead(month: date | None) -> list[int]:
    """The days of a timetable's month still to come (today included): the site's timetable keeps
    only some of the flights of days already past."""
    if not month:
        return []
    today = _today()
    last = monthrange(month.year, month.month)[1]
    if (month.year, month.month) < (today.year, today.month):
        return []
    first = today.day if (month.year, month.month) == (today.year, today.month) else 1
    return list(range(first, last + 1))


# ---------------------------------------------------------------- actions


@action(flightconnections, tier=Tier.READ)
def route(ctx, origin: str, dest: str):
    """Which airlines fly a route nonstop, how often and on which aircraft.

    One row per airline flying it nonstop, from the route page's timetable for the rest of this
    month (`counted` above the rows): code, name, alliance (from its flights in the timetable, so
    empty for an airline with none this month), per_week (its average flights per week over those
    days, to one decimal), days (`daily` when it flies every day of `counted`, else the weekdays
    it flies) and aircraft;
    `--fields` adds departures (each flight number with its local departure times over those
    days, e.g. `B6 1107 08:45/09:26`: one time per date range). Above the rows:
    the distance, flight time, the site's flights per week (`per_week`), classes, and every
    aircraft type the site lists for the route (some may fly in other months), and `counts` on
    reading the two per_week figures. A route that doesn't fly this month shows the site's first month with
    flights, and `note` says so. A route without a nonstop flight has no rows and points to
    `connections`, which lists the one-stop options.

    Args:
        origin: Departure airport, a 3-letter IATA code (JFK).
        dest: Arrival airport, a 3-letter IATA code (LHR); a city code such as TYO has no page.
    """
    origin, dest = _airport(origin, "--origin"), _airport(dest, "--dest")
    found = _route(ctx, origin, dest)
    about = {"route": f"{_where(found.origin)} -> {_where(found.dest)}", "url": found.url}
    rows = []
    if found.nonstop:
        about.update(distance=found.distance, time=found.time, per_week=found.weekly,
                     classes=", ".join(found.classes), aircraft=", ".join(found.aircraft),
                     month=found.month.strftime("%Y-%m") if found.month else "")
        window = _ahead(found.month)
        if found.month and found.month > _today().replace(day=1):
            about["note"] = (f"no flights this month: the site shows {found.month:%Y-%m}, its first month "
                             f"with flights on this route")
            first = min((d for f in found.flights for d in f["_days"]), default=None)
            window = [d for d in window if first is None or d >= first]    # counted from its first flight
        if window:
            about["counted"] = f"{found.month:%Y-%m}-{window[0]:02d} to {found.month:%Y-%m}-{window[-1]:02d}"
        for airline in found.airlines or [{"code": c, "name": ""} for c in dict.fromkeys(f["airline"] for f in found.flights)]:
            mine = [f for f in found.flights if f["airline"] == airline["code"]]
            dated = [d for f in mine for d in f["_days"] if d in window]
            departures: dict[str, dict[str, None]] = {}     # flight number -> its departure times
            for f in [f for f in mine if set(f["_days"]) & set(window)] or mine:
                departures.setdefault(f["flight"], {})[f["depart"]] = None
            rows.append({
                "code": airline["code"], "name": one_line(airline["name"] or (mine[0]["airline_name"] if mine else "")),
                "alliance": next((f["alliance"] for f in mine if f["alliance"]), ""),
                "per_week": round(len(dated) * 7 / len(window), 1) if window else None,
                "days": weekdays(found.month, dated, window),
                "aircraft": ", ".join(dict.fromkeys(f["aircraft"] for f in mine if f["aircraft"])),
                "departures": [f"{n} {'/'.join(times)}" for n, times in departures.items()],
            })
        about["counts"] = ("per_week above is the site's own figure and can differ; a row's per_week is that "
                           "airline's average flights a week over `counted`, and `daily` means every day of it; days lists each weekday flown at least once, so per_week can be lower "
                           "than their number")
    stops = len(found.stopovers)
    return Listing(
        name="airlines", rows=rows, fields=("code", "name", "alliance", "per_week", "days", "aircraft"), about=about,
        total=len(rows),
        empty=(f"no nonstop flights from {origin} to {dest}"
               + (f"; the site lists {stops} one-stop option{'s' * (stops != 1)}" if stops else "")),
        next=[Next("schedule", {"origin": origin, "dest": dest, "airline": "<code>"},
                   "see this route's timetable: flight numbers, days, times"),
              Next("connections", {"origin": origin, "dest": dest},
                   "list one-stop connections between the two airports", when_empty=True),
              Next("destinations", {"airport": origin},
                   f"list every nonstop destination from {origin}"),
              *_fares_step(origin, dest, [r["code"] for r in rows], "these airlines")])


@action(flightconnections, tier=Tier.READ)
def schedule(ctx, origin: str, dest: str, airline: str | None = None, month: str | None = None,
             date_: str | None = None, offset: int = 0, limit: int = 50):
    """A nonstop route's timetable: flight numbers, days and times, one month at a time.

    One row per flight number, time, aircraft and period, as the site splits them: depart and
    arrive (local times; a later arrival day isn't marked), airline, flight, aircraft, days (the
    weekdays of its dates, or `daily` when it flies every day from its first date to its last),
    dates (the days of the month it flies, e.g. `13-30`, past days included) and classes (economy, premium, business, first). `--fields` adds
    airline_name, alliance and month (another month's timetable has an airline's alliance only
    when the airline also flies the route this month). The same flight often has several rows, one per aircraft or
    period: read `dates`, or pass `--date` for one day's flights. Without `--month` or `--date` it
    is this month's timetable from today on, from the route page; another month costs one more
    request.

    Args:
        origin: Departure airport, a 3-letter IATA code (JFK).
        dest: Arrival airport, a 3-letter IATA code (LHR).
        airline: Only this airline's flights: its code (BA) or name (British Airways).
        month: The month to show, YYYY-MM (the site has about 12 months ahead).
        date_: Only the flights on this day, YYYY-MM-DD (sets the month).
        offset: Rows to skip.
        limit: Rows to show.
    """
    origin, dest = _airport(origin, "--origin"), _airport(dest, "--dest")
    day = _day(date_) if date_ else None
    wanted = _month(month) if month else None
    if day and wanted and (day.year, day.month) != (wanted.year, wanted.month):
        raise ArgumentError(f"--date {date_} is not in --month {month}", help="Give only --date")
    wanted = wanted or (day.replace(day=1) if day else None)
    this_month = _today().replace(day=1)
    if wanted and wanted < this_month:
        raise ArgumentError(f"{'--date' if day and not month else '--month'} {date_ if day and not month else month}"
                            f" is in a past month; the site's timetables start with this one ({this_month:%Y-%m})",
                            help="Give this month or a later one")
    found = _route(ctx, origin, dest)
    if not found.nonstop:
        return Listing(name="flights", rows=[], fields=("depart", "arrive", "flight", "aircraft", "days"),
                       about={"route": f"{_where(found.origin)} -> {_where(found.dest)}"}, total=0,
                       empty=f"no nonstop flights from {origin} to {dest}, so no timetable",
                       next=[Next("connections", {"origin": origin, "dest": dest},
                                  "list one-stop connections and each leg's airlines", when_empty=True)])
    shown_month, rows = found.month, found.flights
    if wanted and found.month and wanted != found.month:
        if not all(found.ids):
            raise PageChanged("the route page has no airport ids for another month's timetable")
        resp = _send(ctx, "POST", "/route_schedule.php",
                     data={"lang": "en", "dep": found.ids[0], "des": found.ids[1], "ym": wanted.strftime("%Y-%m")},
                     headers={"X-Requested-With": "XMLHttpRequest", "Referer": found.url})
        if not resp.ok:
            raise resp.http_error()
        shown_month, rows = schedule_answer(resp.text())
        if shown_month and shown_month != wanted:
            rows = []
        known = {f["airline"]: f["alliance"] for f in found.flights if f["alliance"]}
        for r in rows:              # the calendar's rows leave the alliance empty
            r["alliance"] = r["alliance"] or known.get(r["airline"], "")
    about = {"route": f"{_where(found.origin)} -> {_where(found.dest)}",
             "month": shown_month.strftime("%Y-%m") if shown_month else (month or ""),
             "note": "local times, and an arrival a day or two later isn't marked; one flight can have several rows "
                     "(by aircraft or period): `dates` gives the days of the month each row flies"}
    if airline:
        about["airline"] = airline
        rows = [r for r in rows if _is_airline(airline, r["airline"], r["airline_name"])]
        _check_airline(ctx, airline, bool(rows) or not found.flights)
    if day:
        about["date"] = day.isoformat()
        rows = [r for r in rows if day.day in r["_days"]]
    else:
        ahead = set(_ahead(shown_month))
        if shown_month and len(ahead) < monthrange(shown_month.year, shown_month.month)[1]:
            about["from"] = f"{shown_month.strftime('%Y-%m')}-{min(ahead) if ahead else 0:02d} (rows flying only on past days left out; `dates` still lists past days)"
            rows = [r for r in rows if ahead & set(r["_days"])]
    shown, more = _window(rows, offset, limit)
    return Listing(name="flights", rows=shown,
                   fields=("depart", "arrive", "airline", "flight", "aircraft", "days", "dates", "classes"),
                   about=about,
                   total=len(rows), more=more,
                   empty="no flights match" + (f" ({airline})" if airline else "") + (f" on {day}" if day else ""),
                   next=[Next("route", {"origin": origin, "dest": dest},
                              "see the airlines on this route, their weekly days and aircraft", when_empty=True)])


_MONTH = {m[:3].casefold(): i for i, m in enumerate(MONTHS, 1)}


def _out_of_season(note: str, today: date) -> bool:
    """A leg airline's season (`From Jun to Aug`, `Starts in March`) leaves out today's month.
    `Ends in March` flies until then."""
    months = [_MONTH[w[:3].casefold()] for w in re.findall(r"[A-Za-z]+", note) if w[:3].casefold() in _MONTH]
    if note.startswith("Starts") and months:
        return months[0] != today.month
    if note.startswith("From") and len(months) == 2:
        first, last, now = *months, today.month
        return not (first <= now <= last if first <= last else now >= first or now <= last)
    return False


def _season_note(legs: list) -> str:
    notes = []
    for n, leg in enumerate(legs, 1):
        out = [a for a in leg.airlines if a.note and _out_of_season(a.note, _today())]
        names = ", ".join(f"{a.code or a.name} {a.note}" for a in out)
        if out and len(out) == len(leg.airlines):
            notes.append(f"leg {n}: no airline flies it this month ({names})")
        elif out:
            notes.append(f"leg {n}: out of season this month: {names}")
    return "; ".join(notes)


def _site_rows(stopovers: list[Stopover]) -> list[dict]:
    rows = []
    for s in stopovers:
        legs = []
        for leg in s.legs:
            parts = [f"{a.code or a.name} ({a.days}{', ' + a.note if a.note else ''})" for a in leg.airlines]
            legs.append(f"{leg.origin}-{leg.dest} {leg.miles or '?'} mi {leg.time}: " + "; ".join(parts))
        airlines = [[{"code": a.code, "name": a.name, "alliance": ""} for a in leg.airlines] for leg in s.legs]
        rows.append({"via": s.via, "name": one_line(s.name), "time": s.time,
                     "leg1": one_line(legs[0] if legs else ""), "leg2": one_line(legs[1] if len(legs) > 1 else ""),
                     "airlines": " ".join(s.codes), "note": _season_note(s.legs), "_legs": airlines})
    return rows


FEW = 4         # flights in a month: under one a week, so the route starts, ends or pauses this month


def _why_idle(row: dict) -> str:
    """A destination row's note: the site's or, when the site says nothing, that the route doesn't
    fly this month or flies under one a week."""
    flights = row["flights"]
    if row["note"] or flights is None or flights >= FEW:
        return row["note"]
    if flights == 0:
        return "no flights this month (see `route` for its first month)"
    return f"only {flights} flight{'s' * (flights != 1)} this month"


def _flying(rows: list[dict], pairs: dict[tuple[str, ...], int]) -> str:
    """`N of them in C countries` with flights this month. Of same-named airports a filter's airline
    flies to only k of (`pairs`), which k is unknown: a range when it changes the count."""
    sure = [r for r in rows if r["flights"] and not r.get("_unsure")]
    low, high = len(sure), len(sure)
    some, maybe = {r["country"] for r in sure if r["country"]}, set()
    for codes, k in pairs.items():
        group = [r for r in rows if r.get("_unsure") and r["_unsure"][0] == codes]
        busy = sum(1 for r in group if r["flights"])
        least, most = max(0, k - (len(group) - busy)), min(k, busy)
        low, high = low + least, high + most
        (some if least else maybe if most else set()).add(group[0]["country"])
    countries = some | maybe
    return (f"{low}{f' to {high}' if high != low else ''} of them in "
            f"{len(some)}{f' to {len(countries)}' if len(countries) != len(some) else ''} countries")


def _derived_rows(start, end, back, origin: str, dest: str, airline: str | None, alliance: str | None) -> list[dict]:
    """Hubs both airport pages share: leg 1 from the origin's page, leg 2 from the destination's
    `to` page, the hub's distance to the destination from the destination's `from` page. An
    alliance counts only where both legs fly this month. With a filter, a leg to one of
    same-named airports (`tie`) says how many of them the filter flies to; `_unsure` keeps each
    leg where that is some but not all: (leg, the airports' codes, k)."""
    who = airline or ALLIANCES.get(alliance or "", "")
    arriving = {r["code"]: r for r in end.nonstop}
    miles_back = {r["code"]: r["miles"] for r in back.nonstop}
    rows = []
    for first in start.nonstop:
        second = arriving.get(first["code"])
        if not second or first["code"] in (origin, dest):
            continue
        one, two = first["airlines"], second["airlines"]
        both = sorted({a["code"] for a in one if a["code"]} & {a["code"] for a in two if a["code"]})
        leg2 = miles_back.get(first["code"])
        total = first["miles"] + leg2 if first["miles"] is not None and leg2 is not None else None
        flying = first["flights"] != 0 and second["flights"] != 0
        notes, unsure, tied = [], [], []
        for n, side in enumerate((first, second), 1):
            note = _why_idle(side)
            if side.get("tie"):
                tied.append((n, tuple(side["tie"]["codes"])))
            if side.get("tie") and who:
                k, of = _share(side["tie"], airline, alliance)
                note += f"; {_flies_to(who, k, of)}"
                if k < of:
                    unsure.append((n, tuple(side["tie"]["codes"]), k))
            if note:
                notes.append(f"leg {n}: {note}")
        rows.append({
            "via": first["code"], "name": one_line(first["name"]), "country": one_line(first["country"]),
            "miles": total, "detour": "",
            "flights": f"{first['flights'] if first['flights'] is not None else '?'}/"
                       f"{second['flights'] if second['flights'] is not None else '?'}",
            "leg1": _codes(one), "leg2": _codes(two),
            "alliances": ", ".join(sorted(set(_alliances(one)) & set(_alliances(two)))) if flying else "",
            "same": " ".join(both), "miles1": first["miles"], "miles2": leg2,
            "leg1_airlines": _names(one), "leg2_airlines": _names(two),
            "note": one_line("; ".join(notes)),
            "_legs": [one, two], "_busy": min(first["flights"] or 0, second["flights"] or 0), "_flying": flying,
            "_unsure": unsure, "_tied": tied,
        })
    return rows


def _miles_of(text: str) -> int | None:
    found = re.match(r"\s*([\d,]+) miles", text or "")
    return int(found.group(1).replace(",", "")) if found else None


FAR = 1.5       # a hub whose two legs add up to more than this times the baseline is a detour


def _detours(rows: list[dict], direct: int | None) -> tuple[int | None, str]:
    """Set each paired row's detour over a baseline: the nonstop distance, or without one the
    shortest pair (the pages give no straight-line distance). Returns the baseline and what it is."""
    if direct:
        baseline, what = direct, f"over the nonstop distance, {direct:,} mi"
    else:
        best = min((r for r in rows if r["miles"]), key=lambda r: r["miles"], default=None)
        if not best:
            return None, ""
        baseline = best["miles"]
        what = (f"over the shortest pair, via {best['via']}, {baseline:,} mi (no nonstop; the pages give no "
                f"straight-line distance)")
    for r in rows:
        r["detour"] = f"{round(100 * r['miles'] / baseline) - 100}%" if r["miles"] else ""
    return baseline, what


@action(flightconnections, tier=Tier.READ)
def connections(ctx, origin: str, dest: str, airline: str | None = None, alliance: Alliance | None = None,
                derive: bool = False, sort: Literal["miles", "flights"] = "miles", detours: bool = False,
                offset: int = 0, limit: int = 20):
    """One-stop connections from A to B: each hub with the airlines on both legs.

    When the route has no nonstop flight, the site's route page lists its own one-stop options,
    fastest first: via, name, time (the two flight times added, without the connection), leg1 / leg2 (each leg's
    airlines with their weekdays and season) and note (a leg with an airline out of season this
    month). Otherwise (a route with a nonstop, no route page,
    or `--derive` / `--alliance`), ESUM pairs airport pages instead: every hub the origin flies to
    nonstop that also flies nonstop to the destination, shortest first: via, name, miles (both
    legs), detour (over the nonstop distance, or without a nonstop over the shortest pair; `detour`
    above the rows says which), flights (leg 1 / leg 2 flights
    this month, all airlines on each leg, even with a filter), leg1 / leg2 airline codes (with
    `--airline` or `--alliance`, only the matching ones), alliances (each alliance with an airline on
    both legs, only where both legs fly this month) and note (a seasonal leg, one with no flights
    this month, or one with under 4; a column only when some row has
    one); `--fields` adds same (airlines flying both legs), country, miles1, miles2 and the
    airline names. With `--airline` or `--alliance`, hubs with a leg not flying this month are left
    out, and hubs more than 50% longer than the baseline always are (`dropped` above the rows counts
    both; `--detours` keeps the long ones). Where an airport page names a city with two airports
    (Washington DCA and IAD), it gives each airport's airlines but not which is which: with a
    filter, a leg's `note` says how many of the pair the filter flies to, and when that is not all
    of them, `hubs` above the rows counts the sure hubs and names the unsure ones; without a
    filter, a help line names the `route` pages that settle which airline flies to which. These
    are pairs of nonstop routes, not checked itineraries: confirm a leg's timetable with
    `schedule`. Two-stop options aren't on the pages ESUM reads.

    Args:
        origin: Departure airport, a 3-letter IATA code (JFK).
        dest: Arrival airport, a 3-letter IATA code (ABZ).
        airline: Only hubs where this airline (code or name) flies both legs, both flying this month.
        alliance: Only hubs where this alliance flies both legs, both flying this month: oneworld,
            skyteam or star.
        derive: Pair the airport pages even when the route page lists its own options.
        sort: For paired hubs: miles (shortest first) or flights (busiest leg pair first).
        detours: For paired hubs: keep those more than 50% longer than the baseline.
        offset: Rows to skip.
        limit: Rows to show.
    """
    origin, dest = _airport(origin, "--origin"), _airport(dest, "--dest")
    if origin == dest:
        raise ArgumentError("--origin and --dest are the same airport")
    found = _route(ctx, origin, dest, missing_ok=True)
    about = {"route": (f"{_where(found.origin)} -> {_where(found.dest)}" if found else f"{origin} -> {dest}")}
    if found and found.nonstop:
        about["nonstop"] = f"yes, {len(found.airlines)} airline(s), {found.distance}: see `route`"
    if found and found.stopovers and not derive and not alliance:
        about["source"] = "the site's one-stop options (route page)"
        about["note"] = ("time is the two flight times added, without the connection; each leg's airlines, weekdays "
                         "and season (in brackets) as the site lists them; times, same ticket and the connection are "
                         "not checked: confirm each leg with `schedule`")
        rows = _site_rows(found.stopovers)
        fields: tuple[str, ...] = ("via", "name", "time", "leg1", "leg2")
        paired = False
    else:
        paired = True
        start = _airport_page(ctx, origin, "from")
        end, back = _airport_page(ctx, dest, "to"), _airport_page(ctx, dest, "from")
        about["source"] = f"paired: {origin}'s nonstop destinations x {dest}'s nonstop origins"
        about["note"] = ("two nonstop routes meeting at a hub, not checked itineraries: no times, same ticket or "
                         "minimum connection time; flights is leg 1/leg 2 flights this month, all airlines; with a "
                         "filter, leg1/leg2 show only the matching airlines")
        about["alliances"] = "each alliance with an airline on both legs, only where both legs fly this month"
        rows = _derived_rows(start, end, back, origin, dest, airline, alliance)
        baseline, about["detour"] = _detours(rows, _miles_of(found.distance) if found and found.nonstop else None)
        if sort == "flights":
            rows.sort(key=lambda r: -r["_busy"])
        else:
            rows.sort(key=lambda r: (r["miles"] is None, r["miles"] or 0))
        fields = ("via", "name", "miles", "detour", "flights", "leg1", "leg2", "alliances")
    if airline or alliance:
        if airline:
            about["airline"] = airline
        if alliance:
            about["alliance"] = ALLIANCES[alliance]
        _check_airline(ctx, airline, any(_flies(leg, airline, None) for r in rows for leg in r["_legs"]))
        rows = [r for r in rows if all(_flies(leg, airline, alliance) for leg in r["_legs"])]
        if paired:          # name only the airlines that match on each leg
            for r in rows:
                r["leg1"], r["leg2"] = (_codes(_flies(leg, airline, alliance)) for leg in r["_legs"])
    steps, dropped = [], []
    if paired and (airline or alliance):        # the filter answers "this month", as `alliances` does
        idle = [r["via"] for r in rows if not r["_flying"]]
        if idle:
            rows = [r for r in rows if r["_flying"]]
            hubs = f"hub{'s' * (len(idle) != 1)}"
            listed = "them, with their seasons" if len(idle) > 1 else "it, with its season"
            dropped.append(f"{len(idle)} {hubs} with a leg not flying this month ({', '.join(idle)}): the paired "
                           f"view without the filter lists {listed}")
            steps.append(Next("connections", {"origin": origin, "dest": dest, "derive": True, "airline": None,
                                              "alliance": None},
                              f"see the {hubs} with a leg not flying this month ({', '.join(idle)}): every airline "
                              f"and each leg's season"))
    if not paired:
        steps.append(Next("connections", {"origin": origin, "dest": dest, "derive": True},
                          "pair the airport pages instead: each hub's alliances and flights this month; "
                          "`--alliance oneworld|skyteam|star` keeps one alliance's hubs"))
    if paired and baseline and not detours:
        far = [r for r in rows if r["miles"] and r["miles"] > FAR * baseline]
        if far:
            rows = [r for r in rows if r not in far]
            dropped.append(f"{len(far)} hub{'s' * (len(far) != 1)} more than {round(100 * FAR) - 100}% longer "
                           f"than the baseline; `--detours` keeps them")
            given = {k: v for k, v in (("airline", airline), ("alliance", alliance), ("derive", derive),
                                       ("sort", sort)) if v}
            steps.append(Next("connections", {"origin": origin, "dest": dest, **given, "detours": True},
                              f"keep the {len(far)} hub{'s' * (len(far) != 1)} past the detour limit"))
    if dropped:
        about["dropped"] = "; ".join(dropped)
    unsure = [r for r in rows if r["_unsure"]] if paired else []
    if unsure:      # an airport page names a city, not an airport: count only the hubs it names for sure
        who = airline or ALLIANCES.get(alliance or "", "")
        legs: dict[tuple[int, tuple[str, ...], int], list[str]] = {}
        for r in unsure:
            for key in r["_unsure"]:
                legs.setdefault(key, []).append(r["via"])
        about["hubs"] = (f"{who} flies both legs via {len(rows) - len(unsure)} of these {len(rows)} for sure; unsure: "
                         + "; ".join(f"{', '.join(vias)} (leg {n} goes to {k} of {' and '.join(codes)})"
                                     for (n, codes, k), vias in legs.items())
                         + "; the airport page doesn't say which airport of each pair (`route` settles it)")
        n, codes, _ = next(iter(legs))
        steps.append(Next("route", {"origin": origin, "dest": codes[0]} if n == 1 else {"origin": codes[0], "dest": dest},
                          f"settle which airport of each pair {who} flies to: one `route` a pair ("
                          + ", ".join(f"{origin}-{'/'.join(c)}" if leg == 1 else f"{'/'.join(c)}-{dest}"
                                      for leg, c, _ in legs) + ")"))
    tied = list(dict.fromkeys(t for r in rows for t in r["_tied"])) if paired and not (airline or alliance) else []
    if tied:        # the airport page names a city, not an airport: each airport's own route page says
        n, codes = tied[0]
        steps.append(Next("route", {"origin": origin, "dest": codes[0]} if n == 1 else {"origin": codes[0], "dest": dest},
                          "settle which airlines fly to which airport of "
                          + ", ".join(" and ".join(c) for c in dict.fromkeys(c for _, c in tied))
                          + " (the airport page names the city, not the airport): one `route` a pair ("
                          + ", ".join(f"{origin}-{c}" if leg == 1 else f"{c}-{dest}" for leg, cs in tied for c in cs)
                          + ")"))
    if any(r["note"] for r in rows):
        fields = (*fields, "note")
    if (airline or alliance) and not rows:
        steps.append(Next("connections", {"origin": origin, "dest": dest, "derive": derive, "airline": None,
                                          "alliance": None},
                          "list the hubs without the airline or alliance filter", when_empty=True))
    shown, more = _window(rows, offset, limit)
    return Listing(name="connections", rows=shown, fields=fields, about=about, total=len(rows), more=more,
                   empty=f"no one-stop hub found between {origin} and {dest}"
                         + (" with that airline" if airline else "") + (" with that alliance" if alliance else "")
                         + "; two-stop options aren't on the pages ESUM reads",
                   next=[Next("schedule", {"origin": origin, "dest": "<via>"},
                              "see the first leg's timetable (then the second, from <via> to the destination)"),
                         *steps,
                         *_fares_step(origin, dest, [c for r in rows for c in _both_legs(r)],
                                      "the airlines flying both legs of these hubs"),
                         Next("destinations", {"airport": origin},
                              f"list every nonstop destination from {origin}", when_empty=True)])


FARE_CARRIERS = 10      # `matrix search --carriers` takes at most 10


def _both_legs(row: dict) -> list[str]:
    """The airline codes on both legs of a hub, in leg-1 order: one airline's own one-stop trip."""
    one, two = row["_legs"] if len(row["_legs"]) == 2 else ([], [])
    second = {a["code"] for a in two}
    return [a["code"] for a in one if a["code"] and a["code"] in second]


def _fares_step(origin: str, dest: str, codes: list[str], who: str) -> list[Next]:
    """ITA Matrix prices the route; an open search there can leave out carriers, so name them."""
    codes = [c for c in dict.fromkeys(codes) if _AIRLINE.fullmatch(c) and len(c) == 2][:FARE_CARRIERS]
    if not codes:
        return []
    return [Next("search", {"origin": origin, "dest": dest, "depart": "<date>", "thorough": True,
                            "carriers": ",".join(codes)},
                 f"price the route on ITA Matrix, searching {who} one by one as well", site="matrix")]


Sort = Literal["flights", "distance", "name"]


@action(flightconnections, tier=Tier.READ)
def destinations(ctx, airport: str, direction: Literal["from", "to"] = "from", stops: Literal[0, 1] = 0,
                 airline: str | None = None, alliance: Alliance | None = None, country: str | None = None,
                 sort: Sort = "flights", offset: int = 0, limit: int = 50):
    """Every nonstop destination from an airport (or origin into it), with the airlines on each route.

    One row per airport: code, name, country, flights (the whole month's, all airlines on the
    route together, even with `--airline`), miles (from the airport; not on `--direction to`), airlines
    (codes, every airline on the route, even with a filter) and note. A route with 0 flights doesn't fly this month: seasonal, announced and not
    flying yet, or paused; `note` gives the site's reason, or `no flights this month` when the
    site gives none (`route` shows its first month with flights); a route with 1 to 3 flights
    (under one a week) gets `only N flights this month` when the site gives no reason. `--fields` adds alliances, airline_names and url. With `--airline` this is
    that airline's network from the airport, every route of it; with `--alliance`, the
    alliance's. A big airport has hundreds of rows: `total` counts them, and `--limit 300` shows
    them in one call. `--stops 1` lists instead the site's popular destinations reachable only
    with one stop (code, name, country; up to 200, no airlines), not every one. Above the rows,
    `countries` counts the countries of the airports listed (after the filters, the airport's own
    country included, which the site's `site` sentence leaves out), and `flying` those with
    flights this month. A row's season note is the route's: the airport page gives no airline's
    own season, so with `--airline` check that airline's flights on one route with `route`. Where
    one country has two airports of the same city name (Washington DCA and IAD), the page gives
    each airport's airlines but not which airport is which: both rows list all of them, flagged in
    `note`; with a filter, `note` says how many of the pair the filter flies to and `airports`
    above the rows gives the sure total. An airline name the site doesn't know is an error.

    Args:
        airport: The airport, a 3-letter IATA code (JFK).
        direction: from (destinations from the airport) or to (origins with nonstop flights into it).
        stops: 0 for nonstop routes, 1 for the site's list of one-stop destinations.
        airline: Only routes this airline flies: its code (B6) or name (JetBlue).
        alliance: Only routes an airline of this alliance flies: oneworld, skyteam or star.
        country: Only airports in this country (its English name, e.g. Germany).
        sort: flights (the site's order, busiest first), distance (shortest first) or name.
        offset: Rows to skip.
        limit: Rows to show.
    """
    code = _airport(airport, "--airport")
    if stops == 1 and (airline or alliance):
        raise ArgumentError("--airline and --alliance work with nonstop routes only (--stops 0)",
                            help=f"For one airline's one-stop options, use `esum flightconnections connections "
                                 f"--origin {code} --dest <code> --airline <code>`")
    page = _airport_page(ctx, code, direction)
    about = {"airport": one_line(f"{page.code} {page.city}, {page.name}"), "direction": direction,
             "stops": stops, "site": one_line(page.summary), "url": page.url}
    if stops == 1:
        about["note"] = ("the site's list of popular one-stop destinations (at most 200), not every airport one "
                         "stop away; `connections` finds the hubs for one of them")
        rows = [dict(r) for r in page.onestop]
        fields: tuple[str, ...] = ("code", "name", "country")
    else:
        _check_airline(ctx, airline, any(_flies(r["airlines"], airline, None) for r in page.nonstop))
        who = airline or ALLIANCES.get(alliance or "", "")
        rows = []
        for r in page.nonstop:
            flying = _flies(r["airlines"], airline, alliance)
            if (airline or alliance) and not flying:
                continue
            note, tie, unsure = _why_idle(r), r.get("tie"), None
            if tie and (airline or alliance):
                k, n = _share(tie, airline, alliance)
                note += f"; {_flies_to(who, k, n)}"
                unsure = (tuple(tie["codes"]), k) if k < n else None
            rows.append({"code": r["code"], "name": one_line(r["name"]), "country": one_line(r["country"]),
                         "flights": r["flights"], "miles": r["miles"], "airlines": _codes(r["airlines"]),
                         "note": one_line(note), "alliances": " ".join(_alliances(r["airlines"])),
                         "airline_names": _names(r["airlines"]), "url": r["url"],
                         "_unsure": unsure})
        fields = ("code", "name", "country", "flights", "miles", "airlines", "note")
        about["note"] = ("flights and airlines: the whole route, all airlines, even with a filter; flights: the site's "
                         "count for the whole month (0: not flying this month; under 4: noted)")
        about["counts"] = ("countries: the rows' countries, 0-flight routes and the airport's own country included "
                           "(the site's sentence leaves its own country out); flying: those with flights")
        if airline or alliance:
            about["note"] += ("; a row's note is the route's season, not the airline's: `route` shows whether "
                              "the airline flies it this month")
        if direction == "to":
            fields = tuple(f for f in fields if f != "miles")
    if airline:
        about["airline"] = airline
    if alliance:
        about["alliance"] = ALLIANCES[alliance]
    if country:
        about["country"] = country
        rows = [r for r in rows if r["country"].casefold() == country.strip().casefold()]
    about["countries"] = len({r["country"] for r in rows if r["country"]})
    pairs: dict[tuple[str, ...], int] = dict(r["_unsure"] for r in rows if r.get("_unsure"))
    if stops == 0:
        about["flying"] = _flying(rows, pairs)
    if pairs:
        about["counts"] += "; of a pair in `airports`, flying counts only the airports the filter flies to (a range if unsure)"
        sure = sum(1 for r in rows if not r.get("_unsure"))
        about["airports"] = (f"{who} flies to {sure + sum(pairs.values())} of these {len(rows)}: "
                             + ", ".join(f"{k} of {' and '.join(codes)}" for codes, k in pairs.items())
                             + "; the page doesn't say which (`route` settles it); every other row for sure")
    if sort == "distance" and stops == 0:
        rows.sort(key=lambda r: (r["miles"] is None, r["miles"] or 0))
    elif sort == "name":
        rows.sort(key=lambda r: r["name"].casefold())
    shown, more = _window(rows, offset, limit)
    pair = {"origin": code, "dest": "<code>"} if direction == "from" else {"origin": "<code>", "dest": code}
    steps = [Next("route" if stops == 0 else "connections", pair,
                  "see who flies one route, how often and on which aircraft" if stops == 0
                  else "see the one-stop options to one of them")]
    if pairs:
        first = next(iter(pairs))[0]
        steps.append(Next("route", {**pair, ("dest" if direction == "from" else "origin"): first},
                          f"settle which airport of each pair {who} flies to: one `route` a pair, to either "
                          f"airport ({', '.join('/'.join(codes) for codes in pairs)})"))
    if airline and not rows:
        steps.append(Next("airline", {"code": airline}, "check the airline's code and the site's name for it",
                          when_empty=True))
    return Listing(name="destinations", rows=shown, fields=fields, about=about, total=len(rows), more=more,
                   empty=f"no {'nonstop' if stops == 0 else 'one-stop'} airports match", next=steps)


Show = Literal["destinations", "origins", "routes"]


def _airline_code(ctx, wanted: str, flag: str = "--code") -> str:
    text = wanted.strip()
    if _AIRLINE.fullmatch(text):
        return text.upper()
    html, _ = _page(ctx, "/airlines", _missing("FlightConnections has no airline list", "Give the airline's IATA code"))
    catalog = airline_catalog(html)
    if not catalog:
        raise PageChanged("the airline list has no airlines")
    key = text.casefold()
    codes = catalog.get(key) or sorted({c for name, cs in catalog.items() if key in name for c in cs})
    if not codes:       # the site's shorter name: `Alaska Airlines` is `Alaska`
        heads = [name for name in catalog if key.startswith(name + " ")]
        codes = catalog[max(heads, key=len)] if heads else []
    if len(codes) == 1:
        return codes[0]
    if not codes:
        raise _missing(f"FlightConnections has no airline named {wanted!r}",
                       "Give the airline's IATA code (e.g. B6), or a part of its name as the site writes it")
    names = sorted(f"{name} ({c})" for name, cs in catalog.items() if key in name for c in cs)
    raise ArgumentError(f"{flag} {wanted!r} matches several airlines: {', '.join(names[:12])}",
                        help="Give the airline's IATA code")


@action(flightconnections, tier=Tier.READ)
def airline(ctx, code: str, show: Show = "destinations", origin: str | None = None, offset: int = 0,
            limit: int = 50):
    """An airline's network: the airports it flies to, the airports it flies from, or its top routes.

    `destinations` (default) and `origins` list airports: code, name, country. `routes` lists the
    site's 200 most popular routes of the airline this month: origin, dest and title; `--origin`
    keeps those from one airport. The airline's every route from one airport (a hub) is
    `destinations --airport <hub> --airline <code>`: the site has no full route list for an
    airline outside its map, which ESUM doesn't read.

    Args:
        code: The airline: its IATA code (B6) or name (JetBlue).
        show: destinations, origins or routes.
        origin: With `--show routes`, only routes from this airport (3-letter code).
        offset: Rows to skip.
        limit: Rows to show.
    """
    wanted = _airline_code(ctx, code)
    html, url = _page(ctx, f"/route-map-{wanted.lower()}", _missing(
        f"FlightConnections has no airline with code {wanted}",
        "Check the code, or give the airline's name: `esum flightconnections airline --code \"<name>\"`"))
    found = parse_airline(html, url, wanted)
    about = {"airline": one_line(f"{found.name} ({found.code})"), "show": show,
             "site": one_line("; ".join(s for s in (found.serves, found.departs) if s)), "url": found.url}
    if show == "routes":
        rows = [dict(r) for r in found.routes]
        if origin:
            start = _airport(origin, "--origin")
            about["origin"] = start
            rows = [r for r in rows if r["origin"] == start]
        fields: tuple[str, ...] = ("origin", "dest", "title")
        about["note"] = "the site's 200 most popular routes of the airline this month, not its full network"
        steps = [Next("route", {"origin": "<origin>", "dest": "<dest>"}, "see who flies one route, how often and on which aircraft"),
                 Next("destinations", {"airport": about.get("origin", "<code>"), "airline": found.code},
                      f"list every route {found.code} flies from one airport, not only its top ones")]
    else:
        if origin:
            raise ArgumentError("--origin works with --show routes only",
                                help=f"For {found.code}'s routes from one airport: `esum flightconnections "
                                     f"destinations --airport {_airport(origin, '--origin')} --airline {found.code}`")
        rows = [dict(r) for r in (found.destinations if show == "destinations" else found.origins)]
        fields = ("code", "name", "country")
        steps = [Next("destinations", {"airport": "<code>", "airline": found.code},
                      f"list every route {found.code} flies from one of these airports"),
                 Next("airline", {"code": found.code, "show": "routes"}, f"see {found.code}'s 200 most popular routes"),
                 Next("airline", {"code": found.code, "show": "origins" if show == "destinations" else "destinations"},
                      f"list the airports {found.code} flies "
                      + ("from" if show == "destinations" else "to"))]
    shown, more = _window(rows, offset, limit)
    return Listing(name=show, rows=shown, fields=fields, about=about, total=len(rows), more=more,
                   empty=f"no {show} listed" + (f" from {origin}" if origin else ""), next=steps)
