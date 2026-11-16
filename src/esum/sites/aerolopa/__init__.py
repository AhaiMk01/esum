"""AeroLOPA (aerolopa.com): aircraft seat maps by airline and configuration, which configuration
flies a route or a flight number, and how one cabin compares across the airlines on a route.

Read-only and logged out. Every action reads a public page (robots.txt allows them) and decodes the
data the page embeds for its own rendering (D-92). That data includes each seat's rating and
notes, which the site shows only after its free sign-in; at the user's request ESUM returns them
(`seats`). Not used: `/api/`, the partner API, and the flight-number lookup (it calls `/api/` with
`lookup=` parameters, which robots.txt disallows). Not in the page data, so not returned: seat
width, bed length, per-cabin pitch, AeroLOPA's recommendation lists, and the Super Schedule's
locked columns.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from esum import ArgumentError, Listing, Next, NoSuchItem, Result, Tier, Transport, action, command_line, site

from .parse import airlines, fleet as parse_fleet, routes as parse_routes, schedule as parse_schedule
from .parse import match_type, seatmap as parse_seatmap, seats as parse_seats, type_fleet, type_slugs

FORMAT = 1
EXTRAS = ()

NOT_IN_DATA = ("seat width, bed length and per-cabin pitch, and AeroLOPA's seat recommendation lists "
               "(the page data doesn't carry them; the site shows them only after sign-in)")

aerolopa = site(
    name="aerolopa",
    base_url="https://www.aerolopa.com",
    transport=Transport.DIRECT,
    label={"en": "AeroLOPA: aircraft seat maps by airline and configuration (cabins, rows, seat counts, "
                 "Wi-Fi), the best and worst seats with AeroLOPA's rating and notes (windows, exits, "
                 "bassinets, lavatories), which configuration flies a route or flight, and business or "
                 "other cabins compared across airlines on a route",
           "zh": "AeroLOPA：按航司和机型配置查座位图（舱位、排号、座位数、Wi-Fi），最好和最差的座位"
                 "（评级与说明：窗户、出口、婴儿摇篮、洗手间），某航线或航班飞哪种配置，以及同一航线上"
                 "各航司的公务舱等舱位对比"},
)

Cabin = Literal["first", "business", "premium-economy", "economy"]


def _page(ctx, path: str) -> str | None:
    """A page's HTML, or None when the site answers 404."""
    resp = ctx.http.get(path, check=False)
    if resp.status == 404:
        return None
    if not resp.ok:
        raise resp.http_error()
    return resp.text()


def _window(rows: list[dict], offset: int, limit: int) -> tuple[list[dict], dict | None]:
    shown = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows[offset:offset + limit]]
    return shown, ({"offset": offset + limit} if offset + limit < len(rows) else None)


def _empty(rows: list[dict], offset: int, why: str) -> str:
    """The empty-list message: past the last row is not the same as nothing found."""
    return f"no rows past offset {offset}; {len(rows)} in all" if rows and offset >= len(rows) else why


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _airline(ctx, wanted: str) -> str:
    """The airline's code (its fleet page slug) from a code or a name."""
    text = wanted.strip()
    if not text:
        raise ArgumentError("--airline is empty")
    if re.fullmatch(r"[A-Za-z0-9]{2}", text):
        return text.lower()
    rows = airlines(_page(ctx, "/airlines") or "")
    exact = [r for r in rows if r["name"].casefold() == text.casefold()]
    found = exact or [r for r in rows if _squash(text) in _squash(r["name"])]
    if len(found) == 1:
        return found[0]["code"]
    if found:
        names = ", ".join(f"{r['name']} ({r['code']})" for r in found[:10])
        raise ArgumentError(f"--airline {wanted!r} matches several airlines: {names}",
                            help="Pass one of the codes shown, e.g. `--airline " + found[0]["code"] + "`")
    missing = NoSuchItem(f"AeroLOPA has no airline named {wanted!r}")
    missing.help = "Pass the airline's two-character IATA code (e.g. `--airline ba`), or another spelling of its name"
    raise missing


def _type(ctx, wanted: str) -> str:
    """An aircraft type's slug (`boeing-787-9`) from a slug or a name such as `787-9` or `A350-1000`."""
    text = re.sub(r"[\s_]+", "-", wanted.strip().casefold())
    if not text:
        raise ArgumentError("--type is empty")
    found = match_type(text, type_slugs(_page(ctx, "/aircraft") or ""))
    if len(found) == 1:
        return found[0]
    if found:
        raise ArgumentError(f"--type {wanted!r} matches several aircraft types: {', '.join(found[:12])}",
                            help=f"Pass one of them, e.g. `--type {found[0]}`")
    missing = NoSuchItem(f"AeroLOPA has no aircraft type {wanted!r}")
    missing.help = "Pass a type such as `boeing-787-9`, `787-9`, `a350-1000` or `a321neo`"
    raise missing


@action(aerolopa, tier=Tier.READ)
def fleet(ctx, airline: str | None = None, type: str | None = None, offset: int = 0, limit: int = 50):
    """Seat-map configurations: an airline's fleet, or one aircraft type across every airline.

    One row per configuration (`code`, e.g. `ua-77m`, is what `seatmap` takes) with its seats per
    cabin: F first, J business, W premium economy, Y economy. `--airline` lists that airline's
    configurations (with `--type`, only that type's); `--type` alone compares the type across
    airlines, in AeroLOPA's order (by airline). `--fields` adds haul, wifi (AeroLOPA's rating:
    Good, Better, Best), updated (when AeroLOPA last checked the map), status and url.

    Args:
        airline: Airline code (`ua`, `ba`) or name (`United`, `Singapore Airlines`).
        type: Aircraft type: a slug (`boeing-787-9`) or name (`787-9`, `A350-1000`, `a321neo`).
        offset: Rows to skip.
        limit: Rows to show.
    """
    if not airline and not type:
        raise ArgumentError("give --airline, --type or both", help="e.g. `esum aerolopa fleet --airline ua`")
    if airline:
        code = _airline(ctx, airline)
        page = _page(ctx, f"/{code}")
        if page is None:
            missing = NoSuchItem(f"AeroLOPA has no airline with code {code!r}")
            missing.help = "Pass the airline's name instead, e.g. `--airline \"Air France\"`"
            raise missing
        name, rows = parse_fleet(page)
        about = {"airline": f"{name} ({code})"}
        if type:
            wanted = _squash(re.sub(r"^(boeing|airbus|embraer|b(?=\d)|a(?=\d))[-\s]*", "", type.strip(), flags=re.I))
            rows = [r for r in rows if wanted in _squash(r["type"])]
            about["type"] = type
    else:
        slug = _type(ctx, type)
        page = _page(ctx, f"/aircraft/{slug}")
        if page is None:
            raise NoSuchItem(f"AeroLOPA has no page for aircraft type {slug!r}")
        name, rows = type_fleet(page)
        about = {"type": f"{name} ({slug})"}
    shown, more = _window(rows, offset, limit)
    fields = ("code", "type", "variant", "F", "J", "W", "Y", "seats", "fleet") if airline else \
             ("code", "airline", "variant", "F", "J", "W", "Y", "seats", "fleet")
    return Listing(name="configs", rows=shown, fields=fields, about=about, total=len(rows), more=more,
                   empty=_empty(rows, offset, f"no {type!r} configuration in this fleet" if airline else "no configuration listed"),
                   next=[Next("seatmap", {"code": "<code>"}, "read one configuration's cabins, rows and top routes"),
                         *([Next("fleet", {"type": None, "offset": 0}, "list the whole fleet", when_empty=True)]
                           if airline and type else [])])


def _config(ctx, code: str) -> tuple[str, str]:
    """A configuration's code and page HTML, from a code or its page URL."""
    wanted = code.strip().lower().rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"[a-z0-9]{2}-[a-z0-9-]+", wanted):
        raise ArgumentError(f"not a seat-map code: {code!r}",
                            help="Pass a code from `esum aerolopa fleet --airline <airline>`, e.g. `ua-77m`")
    page = _page(ctx, f"/{wanted}")
    if page is None:
        missing = NoSuchItem(f"AeroLOPA has no seat map {wanted!r}")
        missing.help = f"List the airline's codes: `esum aerolopa fleet --airline {wanted[:2]}`"
        raise missing
    return wanted, page


@action(aerolopa, tier=Tier.READ)
def seatmap(ctx, code: str):
    """One configuration's seat map: its cabins with row ranges and seat counts, how many seats
    AeroLOPA rates good, be-aware and avoid, direct aisle access per cabin, any extra-legroom seat
    product with its pitch, entertainment, Wi-Fi, fleet size, the routes it flies most, and a link
    to the seat-map picture.

    Each seat's rating and notes are one call away: `seats`. The picture shows every seat, exit,
    galley, lavatory, bassinet position and window.

    Args:
        code: Configuration code from `fleet`, e.g. `ua-77m`, or its page URL.
    """
    wanted, page = _config(ctx, code)
    m = parse_seatmap(page)
    lines = [m["title"], f"url: {m['url']}"]
    kind = m["type_full"] + (f" (shown as {m['type']})" if m["type"] and m["type"] != m["type_full"] else "")
    facts = [kind, f"IATA {m['iata']}" if m["iata"] else "", m["haul"].lower(), m["variant"],
             f"{m['seats']} seats", f"fleet {m['fleet']}" if m["fleet"] is not None else "",
             f"updated {m['updated']}" if m["updated"] else "", "" if m["status"] == "active" else m["status"]]
    lines.append("aircraft: " + " · ".join(f for f in facts if f))
    if m["description"]:
        lines.append(f"about: {m['description']}")
    if m["notes"]:
        lines.append(f"notes: {m['notes']}")
    lines.append("cabins:")
    lines += [f"  {c['cabin']} {c['name']}: {c['seats']}, rows {c['rows']}" for c in m["cabins"]]
    r = m["ratings"]
    lines.append(f"seat ratings: {r['good']} good, {r['aware']} be aware, {r['avoid']} avoid, {r['unrated']} unrated")
    if m["aisle_access"]:
        lines.append("direct aisle access: " + ", ".join(f"{k.replace('_', ' ')} {v}%" for k, v in m["aisle_access"].items()))
    for p in m["products"]:
        lines.append(f"seat product: {p}")
    if m["entertainment"]:
        lines.append(f"entertainment: {m['entertainment']}")
    for w in m["wifi"]:
        lines.append("wifi: " + w["system"] + "".join(f" · {x}" for x in (w["rating"], w["speed"]) if x))
    if m["top_routes"]:
        lines.append("top routes (this seat map on N% of the route's flights, aircraft seen in the last 30 days):")
        for t in m["top_routes"]:
            where = ("no Super Schedule" if not t["schedule"]
                     else f"schedule --route {t['schedule']}" if t["schedule"] != t["route"] else "")
            lines.append(f"  {t['route']} {t['from']} to {t['to']}: {t['share']}%" + (f" ({where})" if where else ""))
    if m["image"]:
        lines.append(f"seat-map picture: {m['image']}")
    lines.append(f"not in the data: {NOT_IN_DATA}")
    steps = [Next("seats", {"code": wanted, "rating": "good", "sort": "best"}, "list the best seats with AeroLOPA's notes"),
             Next("seats", {"code": wanted, "rating": "avoid", "sort": "worst"}, "list the seats to avoid and why")]
    routed = next((t["schedule"] for t in m["top_routes"] if t["schedule"]), None)
    if routed:
        steps.append(Next("schedule", {"route": routed}, "compare every flight's cabin on its top route"))
    kind = m["type_slug"] or m["type"]     # some pages carry no type slug; `--type` resolves the name
    if kind and m["airline_code"]:
        steps.append(Next("routes", {"airline": m["airline_code"], "type": kind},
                          f"see which flights get which {m['type']} layout at {m['airline'] or 'this airline'}"))
        steps.append(Next("fleet", {"type": kind}, f"compare the {m['type']} across airlines"))
    data = {**m, "not_in_data": NOT_IN_DATA}
    return Result(data=data, text="\n".join(lines), next=steps)


SeatCabin = Literal["first", "business", "premium-economy", "economy"]
SeatRating = Literal["rated", "good", "aware", "avoid", "unrated", "all"]
SeatSort = Literal["row", "best", "worst"]
SeatView = Literal["great", "average", "poor", "none"]
_RANK = {"good": 0, "": 1, "aware": 2, "avoid": 3}
_VIEW = {"great": 0, "average": 1, "": 1, "poor": 2, "none": 3}     # "": no window (aisle, middle)


def _view(row: dict) -> str:
    return row["window"].split(" ", 1)[0]


@action(aerolopa, tier=Tier.READ)
def seats(ctx, code: str, cabin: SeatCabin | None = None, rating: SeatRating = "rated", note: str | None = None,
          window: SeatView | None = None, sort: SeatSort = "row", offset: int = 0, limit: int = 50):
    """Each seat of a configuration with AeroLOPA's rating and notes: the best and worst seats.

    `rating` is AeroLOPA's colour: good (green: above average, a great choice), aware (yellow: some
    trade-offs), avoid (red: below average, known issues); unrated seats are ordinary ones. `notes`
    are AeroLOPA's, `+` good, `-` bad with its severity (minor, moderate, major), `~` neutral: extra
    legroom, exit doors, bassinets, missing or misaligned windows, lavatory and galley traffic,
    no underseat storage, over the wing, extra-legroom products with their pitch. Bad notes come
    first, worst first. `window` is AeroLOPA's view from a window seat (great, average, poor, none)
    with the window count, empty for seats away from the windows; a rating can rest on it alone.
    `--sort best` puts good seats first (fewest bad and most good notes, then the better view),
    `--sort worst` avoid seats first (most and worst bad notes, then the worse view). `--window`
    keeps one view rating (`--window none` for no usable view). `--note` keeps seats whose notes
    contain some text (`bassinet`, `exit`, `lavatory`, `wing`; `window` also matches good notes
    such as "Faces the windows"). `--fields` adds row, product (e.g. Economy Plus), lieflat and
    bookable. The site shows ratings and notes only after sign-in; its page data carries them for
    everyone.

    Args:
        code: Configuration code from `fleet`, e.g. `ua-77m`, or its page URL.
        cabin: Only this cabin's seats.
        rating: Which seats: rated (good, aware and avoid), one rating, unrated, or all.
        note: Only seats whose notes contain this text, e.g. `bassinet`, `exit`, `lavatory`.
        window: Only window seats with this view.
        sort: row (seat-map order), best or worst.
        offset: Rows to skip.
        limit: Rows to show.
    """
    wanted, page = _config(ctx, code)
    info, rows = parse_seats(page)
    if cabin:
        rows = [r for r in rows if r["cabin"] == cabin]
    if rating == "rated":
        rows = [r for r in rows if r["rating"]]
    elif rating == "unrated":
        rows = [r for r in rows if not r["rating"]]
    elif rating != "all":
        rows = [r for r in rows if r["rating"] == rating]
    if note:
        text = note.casefold().strip()
        rows = [r for r in rows if text in r["notes"].casefold()]
    if window:
        rows = [r for r in rows if _view(r) == window]
    if sort == "best":
        rows = sorted(rows, key=lambda r: (_RANK[r["rating"]], r["_bad"] - r["_good"], _VIEW.get(_view(r), 1), r["row"]))
    elif sort == "worst":
        rows = sorted(rows, key=lambda r: (-_RANK[r["rating"]], r["_good"] - r["_bad"], -_VIEW.get(_view(r), 1),
                                           r["row"]))
    shown, more = _window(rows, offset, limit)
    about = {"config": f"{info['title']} ({wanted})", "cabin": cabin or "all", "rating": rating, "sort": sort}
    if note:
        about["note"] = note
    if window:
        about["window"] = window
    filtered = bool(cabin or note or window) or rating not in ("rated", "all")
    return Listing(name="seats", rows=shown, fields=("seat", "cabin", "rating", "window", "notes"), about=about,
                   total=len(rows), more=more,
                   empty=_empty(rows, offset, "no seat matches" if filtered else "AeroLOPA rates no seat on this map"),
                   next=[Next("seatmap", {"code": wanted}, "read the cabins, rows and rating counts"),
                         *([Next("seats", {"cabin": None, "note": None, "window": None, "rating": "all", "offset": 0},
                                 "list every seat", when_empty=True)] if filtered and not rows else [])])


def _flight_key(text: str) -> str:
    return re.sub(r"\s+", "", text).upper()


@action(aerolopa, tier=Tier.READ)
def schedule(ctx, route: str, cabin: Cabin = "business", date_: str | None = None, airline: str | None = None,
             flight: str | None = None, offset: int = 0, limit: int = 50):
    """Every flight on a route over the next 120 days (or on one date) with the configuration it is
    scheduled on and how good its seat is in one cabin: AeroLOPA's Super Schedule.

    Rows come in AeroLOPA's order, best seat tier first. `tier` is AeroLOPA's ranking of the seat
    (Top: among the best sold in this class on any airline; Middle; Bottom), `seat` its kind (Private
    suite, Open suite, Recliner, Economy seat…), `layout` the seat pattern (1-2-1), `direct` the
    share of seats with direct aisle access, `code` the configuration (for `seatmap`). `--fields`
    adds airline, season, aircraft (type as filed), wifi and config (the cabin's seat count).
    Routes use airport or city codes (`LON-NYC`, `SFO-HOU`), one direction. Bed length, seat width,
    recline, privacy doors, screen size, lavatory ratio and whether the scheduled aircraft really
    turns up need AeroLOPA's sign-in and are not shown.

    Args:
        route: Origin and destination codes, e.g. `MUC-SFO` or `LON-NYC`.
        cabin: Cabin to rank the seats in.
        date_: Only flights on this date (YYYY-MM-DD, within the next 120 days).
        airline: Only this airline's flights (its two-character code, e.g. `UA`).
        flight: Only this flight number, e.g. `UA195` or `UA 195`.
        offset: Rows to skip.
        limit: Rows to show.
    """
    pair = route.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3}-[A-Z0-9]{3}", pair):
        raise ArgumentError(f"not a route: {route!r}", help="Give origin and destination codes, e.g. `--route MUC-SFO`")
    path = f"/super-schedule/{pair}" + ("" if cabin == "business" else f"/{cabin}")
    if date_:
        try:
            day = date.fromisoformat(date_.strip())
        except ValueError:
            raise ArgumentError(f"not a date: {date_!r}", help="Use YYYY-MM-DD, e.g. `--date 2026-12-01`") from None
        path += f"/{day.isoformat()}"
    page = _page(ctx, path)
    if page is None:
        if date_:
            raise ArgumentError(f"AeroLOPA has no {pair} schedule for {date_}: it covers today and the next 120 days",
                                help="Drop --date to list every date: `"
                                     + command_line(ctx.site, ctx.action, {"route": pair, "cabin": cabin},
                                                    placeholders=False) + "`")
        missing = NoSuchItem(f"AeroLOPA has no Super Schedule for {pair}")
        missing.help = ("Check the codes; a city code (LON, NYC, TYO) covers all its airports, "
                        "and a route reads one direction")
        raise missing
    s = parse_schedule(page)
    rows = s["rows"]
    if airline:
        rows = [r for r in rows if _flight_key(r["flight"]).startswith(_flight_key(airline))]
    if flight:
        key = _flight_key(flight)
        rows = [r for r in rows if _flight_key(r["flight"]) == key
                or (key.isdigit() and _flight_key(r["flight"])[2:] == key)]
    gated = sorted({g for r in s["rows"] for g in r["_gated"]})
    shown, more = _window(rows, offset, limit)
    about = {"route": f"{pair} ({s['places']})" if s["places"] else pair, "cabin": s["class"] or cabin,
             "date": f"{day.isoformat()} ({s['date']})" if date_ and s["date"] else
                     s["date"] or "any day in the next 120 days"}
    if gated:
        about["needs_sign_in"] = ", ".join(gated)
    return Listing(name="flights", rows=shown,
                   fields=("flight", "code", "tier", "seat", "layout", "direct", "time", "days"),
                   about=about, total=len(rows), more=more,
                   empty=_empty(rows, offset, "no flight matches" if (airline or flight) else f"no {cabin} flights listed"),
                   next=[Next("seatmap", {"code": "<code>"}, "read a configuration's cabins, rows and top routes"),
                         Next("seats", {"code": (shown[0].get("code") if shown else None) or "<code>", "cabin": cabin,
                                       "sort": "best"},
                              f"list the configuration's best {cabin} seats with AeroLOPA's notes"),
                         *([Next("schedule", {"airline": None, "flight": None, "offset": 0},
                                 "list every flight on the route", when_empty=True)] if (airline or flight) else [])])


@action(aerolopa, tier=Tier.READ)
def routes(ctx, airline: str, type: str, flight: str | None = None, airport: str | None = None,
           offset: int = 0, limit: int = 50):
    """Where an airline flies one aircraft type over the next 30 days, flight by flight, and which of
    its configurations (seat maps) each flight number actually got in the last 30 days.

    Two windows. `flying` looks ahead: on how many days of the next 30 the flight is scheduled with
    this type (`30 of 30`). `layouts` looks back: of the dates in the last 30 days the flight was
    scheduled with this type and AeroLOPA saw the aircraft that operated it, the share each
    configuration flew (`ua-77m 52%`), plus any other type that turned up instead (`Boeing
    777-300ER (other type) 6%`); `seen` gives those as date counts (`ua-77m 15 of 29`), which can
    exceed `flying`. `not seen yet` means fewer than 5 sighted dates, so AeroLOPA can't say which
    configuration flies. `code` is the configuration seen most (for `seatmap`). What a flight
    really gets can differ from the filed configuration `schedule` gives. `--fields` adds places,
    arrives, seen and also (other types scheduled for this flight number in the next 30 days, with
    dates and number of flights; shown only when some flight has one). A type includes its
    sub-variants: AeroLOPA files the 777-200ER under `boeing-777-200`.

    Args:
        airline: Airline code (`ua`) or name.
        type: Aircraft type: a slug (`boeing-777-200`) or name (`777-200`).
        flight: Only this flight number, e.g. `UA384` or `384`.
        airport: Only flights from or to this airport code, e.g. `DEN`.
        offset: Rows to skip.
        limit: Rows to show.
    """
    code = _airline(ctx, airline)
    slug = _type(ctx, type)
    page = _page(ctx, f"/routes/{code}/{slug}")
    if page is None:
        missing = NoSuchItem(f"AeroLOPA lists no {slug} routes for airline {code!r}")
        missing.help = f"See the types this airline flies: `esum aerolopa fleet --airline {code}`"
        raise missing
    r = parse_routes(page)
    rows = r["rows"]
    if flight:
        key = _flight_key(flight)
        rows = [x for x in rows if _flight_key(x["flight"]) == key
                or (key.isdigit() and _flight_key(x["flight"])[2:] == key)]
    if airport:
        port = airport.strip().upper()
        rows = [x for x in rows if port in x["route"].split("-")]
    shown, more = _window(rows, offset, limit)
    about = {"aircraft": f"{r['title']} ({code}, {slug})",
             "flying": f"days scheduled, {r['from']} to {r['to']}",
             "layouts": "what operated on seen dates in the last 30 days",
             "configurations": ", ".join(r["layouts"])}
    return Listing(name="flights", rows=shown,
                   fields=("flight", "route", "departs", "days", "flying", "layouts", "code"),
                   about=about, total=len(rows), more=more,
                   empty=_empty(rows, offset, "no flight matches" if (flight or airport) else "no flights in the window"),
                   next=[Next("seatmap", {"code": "<code>"}, "read the configuration a flight gets"),
                         Next("schedule", {"route": "<route>"}, "compare every airline's flights and seats on a route"),
                         *([Next("routes", {"flight": None, "airport": None, "offset": 0},
                                 "list every flight of this type", when_empty=True)] if (flight or airport) else []),
                         *([Next("fleet", {"airline": code, "type": None, "offset": 0},
                                 "see the airline's other types and try the flight under each: a flight is listed "
                                 "under the type that operates it, which can differ from the type `schedule` "
                                 "shows as filed", when_empty=True)] if flight and not rows else [])])
