"""AeroLOPA page parsers: pure functions, page HTML in, rows out.

AeroLOPA is a Next.js app. Its public pages carry their data in the React Server Components
payload: `<script>self.__next_f.push([1,"…"])</script>` chunks that join into rows of
`<hex id>:<JSON>`. The parsers decode those rows and pick the objects the page renders from.

A configuration page's payload holds every seat with AeroLOPA's rating and notes. The site shows
them only after its free sign-in, but sends them to every visitor; ESUM reads them (D-92). The
payload has no seat width, bed length or per-cabin pitch, and no recommendation lists.
"""

from __future__ import annotations

import html as htmllib
import json
import re
from collections.abc import Iterable, Iterator
from typing import Any

from esum import one_line

BASE = "https://www.aerolopa.com"

_PUSH = re.compile(r"self\.__next_f\.push\((\[.*?\])\)</script>", re.S)
_TITLE = re.compile(r"<title>([^<]*)</title>")
_AIRLINE_LINK = re.compile(r'<a [^>]*href="/([a-z0-9]{2})"[^>]*>([^<]+)</a>')
_TYPE_LINK = re.compile(r'href="/aircraft/([a-z0-9-]+)"')
DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
CABINS = {"first": "F", "business": "J", "premium_economy": "W", "economy": "Y"}


class PageChanged(RuntimeError):
    """A page is not laid out as this parser knows it. Never carries page content."""


def rsc_objects(html: str) -> list[Any]:
    """Every JSON row of the page's server-components payload, decoded."""
    chunks = []
    for raw in _PUSH.findall(html):
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if len(item) > 1 and isinstance(item[1], str):
            chunks.append(item[1])
    stream = "".join(chunks).encode("utf-8")
    objects, pos = [], 0
    while pos < len(stream):
        colon = stream.find(b":", pos)
        if colon < 0:
            break
        if stream[colon + 1:colon + 2] == b"T":
            # A text row: `<id>:T<hex byte length>,<text>`, with no newline after the text.
            comma = stream.find(b",", colon)
            try:
                size = int(stream[colon + 2:comma], 16)
            except ValueError:
                raise PageChanged("the page payload has a malformed text row") from None
            pos = comma + 1 + size
            continue
        end = stream.find(b"\n", colon)
        end = len(stream) if end < 0 else end
        body = stream[colon + 1:end]
        pos = end + 1
        if body[:1] in (b"[", b"{"):
            try:
                objects.append(json.loads(body))
            except ValueError:
                continue
    return objects


def find(objects: Iterable[Any], *keys: str) -> Iterator[dict]:
    """Every dict, at any depth, that has all these keys."""
    stack = list(objects)[::-1]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if all(k in item for k in keys):
                yield item
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            stack.extend(reversed(item))


def first(objects: Iterable[Any], *keys: str, where: str) -> dict:
    for found in find(objects, *keys):
        return found
    raise PageChanged(f"{where} has no object with {', '.join(keys)}")


def title(html: str) -> str:
    """The page's <title> without the ` | AeroLOPA` suffix."""
    found = _TITLE.search(html)
    return one_line(htmllib.unescape(found.group(1))).removesuffix(" | AeroLOPA") if found else ""


def _text(value: Any) -> str:
    return one_line(str(value)) if value not in (None, "") else ""


def _hhmm(raw: Any) -> str:
    raw = str(raw or "")
    return f"{raw[:-2]}:{raw[-2:]}" if re.fullmatch(r"\d{3,4}", raw) else raw


def days_label(days: Iterable[int]) -> str:
    """ISO weekdays (1 = Monday) as `Daily` or `Mo We Fr`."""
    wanted = sorted({d for d in days if isinstance(d, int) and 1 <= d <= 7})
    return "Daily" if len(wanted) == 7 else " ".join(DAYS[d - 1] for d in wanted)


# --- airlines ---------------------------------------------------------------------------------

def airlines(html: str) -> list[dict]:
    """The `/airlines` index: one row per airline, its code (the fleet page slug) and name."""
    start = html.find('id="letter-')
    if start < 0:
        raise PageChanged("the airlines page has no letter sections")
    rows, seen = [], set()
    for code, name in _AIRLINE_LINK.findall(html[start:]):
        if code not in seen:
            seen.add(code)
            rows.append({"code": code, "name": one_line(htmllib.unescape(name)), "url": f"{BASE}/{code}"})
    if not rows:
        raise PageChanged("the airlines page lists no airline")
    return rows


def type_slugs(html: str) -> list[str]:
    """The `/aircraft` index: every aircraft type's slug, e.g. `boeing-787-9`."""
    slugs = list(dict.fromkeys(_TYPE_LINK.findall(html)))
    if not slugs:
        raise PageChanged("the aircraft types page lists no type")
    return slugs


def match_type(text: str, slugs: list[str]) -> list[str]:
    """The type slugs a user's text names: the slug itself, a slug ending in it (`787-9`), one
    containing it, or, failing those, the same without a sub-variant suffix (AeroLOPA files the
    `777-200ER` under `boeing-777-200`)."""
    squash = lambda s: re.sub(r"[^a-z0-9]", "", s)  # noqa: E731
    for candidate in dict.fromkeys([text, re.sub(r"-?(er|lr|ulr|xlr)$", "", text)]):
        if not candidate:
            continue
        if candidate in slugs:
            return [candidate]
        found = ([s for s in slugs if s.endswith("-" + candidate)] or [s for s in slugs if candidate in s]
                 or [s for s in slugs if squash(candidate) in squash(s)])
        if found:
            return found
    return []


# --- fleet (an airline's configurations, or one type across airlines) -------------------------

def _wifi(wifi: Any) -> str:
    """The first Wi-Fi system's rating (Good, Better, Best), as the fleet tables show it."""
    if isinstance(wifi, list):
        wifi = wifi[0] if wifi else None
    if isinstance(wifi, dict):
        return _text(wifi.get("rating"))
    return ""


def config_row(item: dict, airline: str = "") -> dict:
    """One seat-map configuration, from an airline's fleet page or an aircraft-type page."""
    counts = item.get("seat_counts") or {}
    code = _text(item.get("aircraft_code"))
    row = {"code": code,
           "airline": _text(item.get("airline_name")) or airline,
           "type": _text(item.get("aircraft_type_displayed") or item.get("aircraft_type")),
           "variant": _text(item.get("optional_text")),
           **{letter: counts.get(name) or 0 for name, letter in CABINS.items()},
           "seats": item.get("total_seats") or counts.get("total") or sum(counts.get(n) or 0 for n in CABINS),
           "fleet": item.get("fleet_count"),
           "haul": _text(item.get("haul_type")),
           "wifi": _wifi(item.get("wifi")),
           "updated": _text(item.get("last_updated")),
           "status": _text(item.get("lifecycle")) or "active",
           "url": f"{BASE}/{code}"}
    return row


def fleet(html: str) -> tuple[str, list[dict]]:
    """An airline's fleet page: the airline's name and one row per seat-map configuration."""
    name = title(html).removesuffix(" Seat Maps")
    rows, seen = [], set()
    for item in find(rsc_objects(html), "aircraft_code", "seat_counts", "fleet_count"):
        if "seats" in item or item["aircraft_code"] in seen:
            continue
        seen.add(item["aircraft_code"])
        rows.append(config_row(item, name))
    if not rows:
        raise PageChanged("the airline page lists no aircraft")
    return name, rows


def type_fleet(html: str) -> tuple[str, list[dict]]:
    """An aircraft-type page: the type's name and one row per airline configuration."""
    found = first(rsc_objects(html), "variants", where="the aircraft type page")
    rows = [config_row(v) for v in found["variants"] if isinstance(v, dict) and v.get("aircraft_code")]
    if not rows:
        raise PageChanged("the aircraft type page lists no configuration")
    return title(html).removesuffix(" Seat Maps"), rows


# --- seat map ---------------------------------------------------------------------------------

# AeroLOPA's legend: green "Good: above average, a great choice", yellow "Be Aware: some
# trade-offs to consider", red "Avoid: below average, known issues"; other seats are unrated.
RATINGS = {"green": "good", "yellow": "aware", "red": "avoid"}
_SEVERITY = {"major": 3, "moderate": 2, "minor": 1}


def _config_page(html: str) -> tuple[dict, dict]:
    page = first(rsc_objects(html), "aircraft", "topRoutes", where="the seat map page")
    a = page["aircraft"]
    if not isinstance(a, dict) or "cabins" not in a:
        raise PageChanged("the seat map page has no aircraft object")
    return page, a


def _seat_name(raw: str) -> str:
    """`01A` → `1A`."""
    found = re.fullmatch(r"0*(\d+)([A-Z]+)", raw)
    return f"{found.group(1)}{found.group(2)}" if found else raw


def seat_row(seat_id: str, s: dict) -> dict:
    """One seat: its rating, AeroLOPA's notes (+ good, - bad with severity, ~ neutral) and flags.

    Notes run bad first (worst severity first), then good, then neutral, so a cut cell keeps the
    reasons a seat is rated down."""
    notes, good, bad = [], 0, 0
    for c in s.get("comments") or []:
        if not isinstance(c, dict) or not c.get("comment"):
            continue
        sentiment = c.get("sentiment")
        severity = c.get("severity")
        mark = {"good": "+", "bad": "-"}.get(sentiment, "~")
        weight = _SEVERITY.get(severity, 1) if sentiment == "bad" else 0
        rank = (0 if sentiment == "bad" else 1 if sentiment == "good" else 2, -weight)
        notes.append((rank, f"{mark} {_text(c['comment'])}" + (f" ({severity})" if weight and severity else "")))
        if sentiment == "good":
            good += 1
        elif sentiment == "bad":
            bad += weight
    notes = [text for _, text in sorted(notes, key=lambda n: n[0])]
    window = s.get("window") if isinstance(s.get("window"), dict) else None
    found = re.match(r"0*(\d+)", seat_id)
    return {"seat": _seat_name(seat_id),
            "row": int(found.group(1)) if found else 0,
            "cabin": _text(s.get("cabin")).replace("_", "-"),     # as the `--cabin` flag spells it
            "rating": RATINGS.get(s.get("rating") or "", ""),
            "notes": " · ".join(notes),
            "window": f"{_text(window.get('status'))} ({window.get('count')})" if window else "",
            "product": _text(s.get("seat_product_name")),
            "lieflat": bool(s.get("lieflat")),
            "bookable": bool(s.get("bookable", True)) and not s.get("blocked") and not s.get("crew_rest"),
            "_good": good, "_bad": bad}


def seats(html: str) -> tuple[dict, list[dict]]:
    """A configuration page's seats, in seat-map order (row, then letter)."""
    _, a = _config_page(html)
    raw = a.get("seats")
    if not isinstance(raw, dict) or not raw:
        raise PageChanged("the seat map page has no seats")
    rows = [seat_row(k, v) for k, v in raw.items() if isinstance(v, dict)]
    rows.sort(key=lambda r: (r["row"], r["seat"]))
    return {"code": _text(a.get("aircraft_code")), "title": title(html).removesuffix(" Seat Map")}, rows


def rating_counts(a: dict) -> dict:
    meta = a.get("seats_meta") or {}
    rated = {name: meta.get(f"{color}_count") or 0 for color, name in RATINGS.items()}
    total = meta.get("seat_count") or a.get("total_seats") or 0
    return {**rated, "unrated": max(total - sum(rated.values()), 0)}


def seatmap(html: str) -> dict:
    """One configuration's page: the facts beside the seat-map picture, seat-rating counts, aisle
    access per cabin and any extra-legroom seat product (its pitch and recline)."""
    page, a = _config_page(html)
    wifi = [w for w in (a.get("wifi") or []) if isinstance(w, dict)]
    cabins = [{"cabin": {"M": "Y"}.get(_text(c.get("cabin_type")), _text(c.get("cabin_type"))), "name": _text(c.get("cabin_class")),
               "seats": c.get("num_seats"), "rows": _text(c.get("rows"))}
              for c in a.get("cabins") or [] if isinstance(c, dict)]
    routes = []
    for r in page.get("topRoutes") or []:
        if not isinstance(r, dict):
            continue
        share = r.get("observed_share")
        routes.append({"route": f"{_text(r.get('origin'))}-{_text(r.get('destination'))}",
                       "from": _text(r.get("origin_name")), "to": _text(r.get("destination_name")),
                       "share": round(share * 100) if isinstance(share, (int, float)) else None,
                       "schedule": (r.get("guideHref") or "").removeprefix("/super-schedule/") or None})
    code = _text(a.get("aircraft_code"))
    return {"code": code,
            "title": title(html).removesuffix(" Seat Map"),
            "airline": _text(page.get("airlineName")),
            "airline_code": _text(page.get("airlineCode") or a.get("airline_iata")).lower(),
            "type": _text(a.get("aircraft_type_displayed")),
            "type_full": _text(a.get("aircraft_type")),
            "type_slug": _text(page.get("typeSlug")),
            "iata": _text(a.get("iata_code")),
            "variant": _text(a.get("optional_text")),
            "description": _text(a.get("description")),
            "notes": _text(a.get("notes")),
            "haul": _text(a.get("haul_type")),
            "seats": a.get("total_seats"),
            "fleet": a.get("fleet_count"),
            "updated": _text(a.get("last_updated")),
            "status": _text(a.get("lifecycle")) or "active",
            "cabins": cabins,
            "entertainment": _text(a.get("ife_description")),
            "wifi": [{"system": _text(w.get("technology")), "rating": _text(w.get("rating")),
                      "speed": _text(w.get("speed_range"))} for w in wifi],
            "top_routes": routes,
            "ratings": rating_counts(a),
            "aisle_access": {_text(k): v.get("direct_pct") for k, v in
                             ((a.get("seats_meta") or {}).get("aisle_access") or {}).items()
                             if isinstance(v, dict) and v.get("direct_pct") is not None},
            "products": [_product(p) for p in a.get("seat_products") or [] if isinstance(p, dict)],
            "image": _text(a.get("image_url_cdn")),
            "url": f"{BASE}/{code}"}


def _product(p: dict) -> str:
    """`Economy Plus (economy, rows 16-26, 39-40, 102 seats): pitch 35" (standard 31"), recline 4" (standard 3")`."""
    where = ", ".join(x for x in (_text(p.get("cabin")), f"rows {_text(p.get('rows'))}" if p.get("rows") else "",
                                  f"{p['seats']} seats" if p.get("seats") else "") if x)
    sizes = []
    for key, label in (("pitch", "pitch"), ("recline", "recline")):
        value, standard = p.get(f"{key}_in"), p.get(f"standard_{key}_in")
        if value is not None:
            sizes.append(f'{label} {value}"' + (f' (standard {standard}")' if standard is not None else ""))
    return f"{_text(p.get('name'))} ({where})" + (f": {', '.join(sizes)}" if sizes else "")


# --- super schedule (one route, one cabin) ----------------------------------------------------

def schedule(html: str) -> dict:
    """A Super Schedule page: the route, its cabin, the date (if any) and one row per flight."""
    objects = rsc_objects(html)
    table = first(objects, "rows", "cabinKey", where="the route schedule page")
    header = next(find(objects, "rows", "originCode", "dated"), {})
    rows = []
    for r in table["rows"]:
        if not isinstance(r, dict):
            continue
        shown, gated = {}, []
        for cell in r.get("publicRows") or []:
            if not isinstance(cell, dict):
                continue
            if cell.get("gated"):
                gated.append(_text(cell.get("label")))
            else:
                shown[_text(cell.get("label"))] = _text(cell.get("value"))
        flight = _text(r.get("flightNo"))
        rows.append({"flight": flight,
                     "airline": _text(r.get("airlineName")),
                     "time": _text(r.get("time")),
                     "days": _text(r.get("days")),
                     "season": _text(r.get("season")),
                     "code": _text(r.get("aircraftCode")),
                     "aircraft": _text(r.get("aircraftType")),
                     "tier": _text(r.get("bandWord")),
                     "seat": _text(r.get("seatWord")) + (f" ({_text(r.get('seatQualifier'))})"
                                                         if r.get("seatQualifier") else ""),
                     "layout": shown.get("Seat layout", ""),
                     "direct": shown.get("Direct access", ""),
                     "wifi": shown.get("Wi-Fi", ""),
                     "config": _text(r.get("configLabel")),
                     "_gated": gated})
    heading = title(html)
    places = heading.partition(": ")[2]
    return {"cabin": _text(table.get("cabinKey")),
            "class": _text(header.get("classWord")),
            "date": _text(header.get("dateLabel")) if header.get("dated") else "",
            "places": places,
            "heading": heading,
            "rows": rows}


# --- routes (where an airline flies one aircraft type) ----------------------------------------

def _layouts(flight: dict) -> list[tuple[str, int, int, int]]:
    """(code, percent, dates seen, dates observed in all) per layout, most seen first."""
    found = []
    total = flight.get("observed_total") or 0
    for lay in flight.get("layouts") or []:
        if isinstance(lay, dict) and lay.get("aircraft_code"):
            share = lay.get("share")
            pct = round(share * 100) if isinstance(share, (int, float)) else 0
            found.append((_text(lay["aircraft_code"]), pct, lay.get("observed") or 0, total))
    return sorted(found, key=lambda x: (-x[2], x[0]))


def _flew_instead(flight: dict) -> list[tuple[str, int, int, int]]:
    """(type name, percent, dates, dates observed in all) per other type that operated the flight
    on seen dates: the share the site leaves unfilled and names on hover."""
    found = []
    total = flight.get("observed_total") or 0
    for t in flight.get("other_types") or []:
        if isinstance(t, dict) and (t.get("type_name") or t.get("type")):
            share = t.get("share")
            pct = round(share * 100) if isinstance(share, (int, float)) else 0
            found.append((_text(t.get("type_name") or t.get("type")), pct, t.get("observed") or 0, total))
    return sorted(found, key=lambda x: (-x[2], x[0]))


def _scheduled(flight: dict) -> str:
    """Other aircraft types scheduled for this flight number in the window: type, dates, departures."""
    parts = []
    filed = set(flight.get("filed_as") or [])
    for t in flight.get("scheduled_types") or []:
        if not isinstance(t, dict) or t.get("type") in filed:
            continue
        name = _text(t.get("type_name") or t.get("type"))
        start, end = _text(t.get("first_date")), _text(t.get("last_date"))
        when = start if start == end else f"{start} to {end}"
        count = t.get("departures")
        if isinstance(count, int) and start != end:
            when += f", {count} flight" + ("" if count == 1 else "s")
        parts.append(f"{name} ({when})" if when else name)
    return "; ".join(parts)


def routes(html: str) -> dict:
    """A `/routes/<airline>/<type>` page: the window and one row per flight number."""
    page = first(rsc_objects(html), "routes", "carrier", "layouts", where="the routes page")
    carrier = _text(page.get("carrier")).upper()
    rows = []
    for route in page["routes"]:
        if not isinstance(route, dict):
            continue
        pair = f"{_text(route.get('origin'))}-{_text(route.get('destination'))}"
        places = f"{_text(route.get('origin_name'))} to {_text(route.get('destination_name'))}"
        for f in route.get("flights") or []:
            if not isinstance(f, dict):
                continue
            lays = _layouts(f)
            seen = lays + [(f"{name} (other type)", p, n, t) for name, p, n, t in _flew_instead(f)]
            arr = _hhmm(f.get("arr_time"))
            if f.get("arr_day"):
                arr += f" +{f['arr_day']}"
            rows.append({"flight": f"{carrier} {_text(f.get('flight_no'))}",
                         "route": pair,
                         "places": places,
                         "departs": " · ".join(_hhmm(t) for t in f.get("dep_times") or []),
                         "arrives": arr,
                         "days": days_label(f.get("days_of_op") or []),
                         "flying": f"{f.get('departures') or 0} of {f.get('window_days') or 0}",
                         # the site: "Not yet seen as a <type> — scheduled ahead" (fewer than 5
                         # sighted dates in the last 30 days)
                         "layouts": " · ".join(f"{c} {p}%" for c, p, _, _ in seen) or "not seen yet",
                         "seen": " · ".join(f"{c} {n} of {t}" for c, _, n, t in seen)
                                 or f"{f.get('observed_total') or 0} sighted dates",
                         "code": lays[0][0] if lays else "",
                         "also": _scheduled(f)})
    window = page.get("window") or {}
    return {"carrier": carrier, "title": title(html).removesuffix(" routes"),
            "from": _text(window.get("from")), "to": _text(window.get("to")),
            "layouts": [_text(x.get("aircraft_code")) for x in page.get("layouts") or [] if isinstance(x, dict)],
            "rows": rows}
