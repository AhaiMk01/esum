"""FlightConnections' public pages into rows. Pure functions, HTML in.

Sources, all public pages its sitemaps list (robots.txt allows them):

- a route page (`/flights-from-jfk-to-lhr`): the nonstop summary (distance, flight time, airlines,
  alliances, classes, aircraft, flights per week), this month's timetable, and for a route without a
  nonstop flight the site's one-stop options with each leg's airlines and weekdays;
- the timetable of another month, the JSON the route page's own calendar asks for
  (`POST /route_schedule.php`), whose `timetable` holds the same rows;
- an airport page (`/flights-from-<city>-<iata>`, `/flights-to-<city>-<iata>`): every nonstop
  destination (or origin) with its flights this month, a list of one-stop destinations, distances,
  and the sentences naming the airlines on each route;
- an airline page (`/route-map-<airline>-<iata>`): the airports it flies to and from and its top
  200 routes.

The map, its filters and the routes-by-airline layer come from `/rt*`, `/airline_routes.php` and
`/airports_url.php`, which robots.txt disallows; nothing here reads them.
"""

from __future__ import annotations

import json
import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from html import unescape

from esum import element_inner, one_line, text_of

BASE = "https://www.flightconnections.com"
CLASSES = {"Y": "economy", "W": "premium", "J": "business", "F": "first"}
DAYS = ("mo", "tu", "we", "th", "fr", "sa", "su")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")


class PageChanged(RuntimeError):
    """A page is not laid out as this parser knows it. Never carries page content."""


# ---------------------------------------------------------------- shared bits


def _attr(tag: str, name: str) -> str:
    found = re.search(rf'\b{name}="([^"]*)"', tag)
    return unescape(found.group(1)) if found else ""


def _code_of(label: str) -> str:
    """`London Heathrow (LHR)` -> `LHR`."""
    found = re.search(r"\(([A-Z0-9]{3})\)\s*$", label.strip())
    return found.group(1) if found else ""


def _name_of(label: str) -> str:
    """`Paris (Orly) (ORY)` -> `Paris (Orly)`."""
    return re.sub(r"\s*\([A-Z0-9]{3}\)\s*$", "", label.strip())


def _int(text: str) -> int | None:
    found = re.search(r"\d[\d,]*", text or "")
    return int(found.group(0).replace(",", "")) if found else None


def airline_catalog(html: str) -> dict[str, list[str]]:
    """Airline name (casefolded) -> IATA codes, from the airline filter every airport, airline and
    one-stop page carries. Route pages with nonstop flights have none."""
    catalog: dict[str, list[str]] = {}
    for tag in re.findall(r'<(?:div|a)\b[^>]*\bairline-option\b[^>]*>', html):
        name, code = text_of(_attr(tag, "data-name")), _attr(tag, "data-iata").strip()
        if name and code and code not in catalog.setdefault(name.casefold(), []):
            catalog[name.casefold()].append(code)
    return catalog


def _code_for(name: str, catalog: dict[str, list[str]]) -> str:
    codes = catalog.get(name.casefold()) or []
    return codes[0] if len(codes) == 1 else ""


def _section(html: str, opening: str) -> str:
    return element_inner(html, opening) or ""


# ---------------------------------------------------------------- route page


@dataclass
class Place:
    code: str
    city: str
    airport: str
    country: str


@dataclass
class LegAirline:
    name: str
    code: str
    days: str          # `mo th fr`, `daily`
    note: str          # `Ends in March`, `From Apr to Oct`


@dataclass
class Leg:
    origin: str
    dest: str
    miles: int | None
    time: str          # `2h 35`
    airlines: list[LegAirline]


@dataclass
class Stopover:
    via: str
    name: str
    time: str          # total, as the site gives it: `8h 10`
    codes: list[str]   # every airline on the two legs
    legs: list[Leg]


@dataclass
class Route:
    origin: Place
    dest: Place
    url: str
    nonstop: bool
    distance: str = ""
    time: str = ""
    weekly: int | None = None
    airlines: list[dict] = field(default_factory=list)      # code, name
    alliances: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    aircraft: list[str] = field(default_factory=list)
    month: date | None = None
    flights: list[dict] = field(default_factory=list)       # this month's timetable rows
    stopovers: list[Stopover] = field(default_factory=list)
    ids: tuple[str, str] = ("", "")                         # the site's airport ids, for other months


def _place(html: str, side: str) -> Place:
    block = _section(html, rf'<div class="route-page-{side}">')
    if not block:
        raise PageChanged(f"the route page has no route-page-{side} block")
    city = text_of((re.search(r"<h3>(.*?)</h3>", block, re.S) or [None, ""])[1])
    code = text_of((re.search(r"<h2>(.*?)</h2>", block, re.S) or [None, ""])[1])
    airport = text_of((re.search(r"</h2>\s*<p>(.*?)</p>", block, re.S) or [None, ""])[1])
    country = text_of((re.search(rf'<div class="route-page-{side}-country">.*?<p>(.*?)</p>', block, re.S)
                       or [None, ""])[1])
    if not re.fullmatch(r"[A-Z0-9]{3}", code):
        raise PageChanged(f"the route page's {side} airport has no code")
    return Place(code, city, airport, country)


def _info_rows(html: str) -> dict[str, tuple[str, list[str]]]:
    """The summary box: title -> (text, the items not struck through)."""
    rows: dict[str, tuple[str, list[str]]] = {}
    for start in re.finditer(r'<div class="route-page-info-row', html):
        chunk = element_inner(html[start.start():], r'<div class="route-page-info-row') or ""
        title = text_of((re.search(r'route-page-info-title">\s*<span>(.*?)</span>', chunk, re.S) or [None, ""])[1])
        items = [text_of(i) for i in re.findall(r'<li(?![^>]*class="strike")[^>]*>(.*?)</li>', chunk, re.S)]
        text = text_of((re.search(r'route-page-info-text[^"]*">\s*<span>(.*?)</span>', chunk, re.S) or [None, ""])[1])
        if title:
            rows[title] = (text, [i for i in items if i])
    return rows


def _month_of(html: str) -> date | None:
    found = re.search(r'datepicker--nav-title">\s*(\w+) (\d{4})\s*<', html)
    if not found or found.group(1) not in MONTHS:
        return None
    return date(int(found.group(2)), MONTHS.index(found.group(1)) + 1, 1)


def weekdays(month: date | None, days: list[int], over: list[int] | None = None) -> str:
    """The weekdays a month's dates fall on: `daily` when they cover every day of `over`, or
    without it when they run without a gap from the first to the last for at least a week (a
    flight on every day it is listed for), else the weekdays."""
    if not month or not days:
        return ""
    last = monthrange(month.year, month.month)[1]
    dated = sorted({d for d in days if 1 <= d <= last})
    if not dated:
        return ""
    if dated == sorted(set(over)) if over else len(dated) >= 7 and dated[-1] - dated[0] + 1 == len(dated):
        return "daily"
    seen = {date(month.year, month.month, d).weekday() for d in dated}
    return " ".join(DAYS[i] for i in sorted(seen))


def timetable(html: str, month: date | None) -> list[dict]:
    """The rows of a timetable block: one per flight number and time, with the days of the month it
    flies (`data-dates`)."""
    rows = []
    for chunk in re.split(r'<ul class="schedule-table-row btn"', html)[1:]:
        head = chunk[:chunk.find(">")]
        days = [int(d) for d in re.findall(r"\d+", _attr(head, "data-dates"))]

        def column(name: str) -> str:
            found = re.search(rf'<li class="schedule-table-column {name}"[^>]*>(.*?)</li>', chunk, re.S)
            return found.group(1) if found else ""

        airline = column("airline")
        name = text_of((re.search(r'<div class="text">\s*<p>(.*?)</p>', airline, re.S) or [None, ""])[1])
        alliance = text_of((re.search(r'<p class="alliance">(.*?)</p>', airline, re.S) or [None, ""])[1])
        offered = [CLASSES.get(c, c) for c in re.findall(r'<p data-content="([A-Z])"', column("class"))]
        rows.append({
            "depart": text_of(column("dep")), "arrive": text_of(column("des")),
            "airline": _attr(head, "data-airline"), "airline_name": name, "alliance": alliance,
            "flight": text_of(column("flightno")), "aircraft": text_of(column("aircraft")),
            "classes": " ".join(offered), "days": weekdays(month, days),
            "dates": _dates_text(days), "month": month.strftime("%Y-%m") if month else "",
            "_days": days,
        })
    return rows


def _dates_text(days: list[int]) -> str:
    """`1-30` or `1,6,8,13`."""
    if not days:
        return ""
    runs: list[list[int]] = []
    for d in sorted(set(days)):
        if runs and d == runs[-1][1] + 1:
            runs[-1][1] = d
        else:
            runs.append([d, d])
    return ",".join(f"{a}-{b}" if b > a else str(a) for a, b in runs)


def _leg_airlines(block: str, catalog: dict[str, list[str]]) -> list[LegAirline]:
    found = []
    for chunk in block.split('<div class="airline-schedule">')[1:]:
        name = text_of((re.search(r'airline-schedule-airline-name">(.*?)</span>', chunk, re.S) or [None, ""])[1])
        note = text_of((re.search(r'validity-per-airline">(.*?)</div>', chunk, re.S) or [None, ""])[1])
        active = re.findall(r'airline-schedule-day active" id="(\w\w)"', chunk)
        days = "daily" if len(set(active)) == 7 else " ".join(d for d in DAYS if d in active)
        if name:
            found.append(LegAirline(name, _code_for(name, catalog), days, note))
    return found


def _stopovers(html: str) -> list[Stopover]:
    catalog = airline_catalog(html)
    options = []
    for tag in re.findall(r'<li class="flight-path-via[^"]*"[^>]*>.*?</li>', html, re.S):
        head = tag[:tag.find(">")]
        number = _attr(head, "data-routenumber")
        via = _attr(head, "data-connections").strip()
        name = text_of((re.search(r'via-destination">(.*?)</span>', tag, re.S) or [None, ""])[1])
        time = text_of(re.sub(r"<div.*", "", (re.search(r'via-duration">(.*?)</span>', tag, re.S) or [None, ""])[1],
                              flags=re.S))
        legs = []
        for leg in (1, 2):
            opening = rf'<div class="route-info leg-{leg} route{re.escape(number)}"'
            start = re.search(opening, html)
            if not start:
                continue
            block = element_inner(html, opening) or ""
            tag_open = html[start.start():html.find(">", start.start())]
            legs.append(Leg(_attr(tag_open, "data-dep"), _attr(tag_open, "data-des"),
                            _int(text_of((re.search(r'route-info-distance">(.*?)</span>', block, re.S)
                                          or [None, ""])[1])),
                            text_of((re.search(r'route-info-duration">(.*?)</span>', block, re.S)
                                     or [None, ""])[1]),
                            _leg_airlines(block, catalog)))
        options.append(Stopover(via, re.sub(r"^via\s+", "", name), time,
                                [c for c in _attr(head, "data-airlines").split(",") if c], legs))
    return options


def route(html: str, url: str) -> Route:
    origin, dest = _place(html, "dep"), _place(html, "des")
    stopovers = _stopovers(html)
    nonstop = 'class="schedule-table-row btn"' in html or 'airline-schedule-button' in html
    found = Route(origin, dest, url, nonstop, stopovers=stopovers)
    if not nonstop:
        if not stopovers and "no direct flights" not in html:
            raise PageChanged("the route page has neither a timetable nor stopover options")
        return found
    info = _info_rows(html)
    found.distance = info.get("Distance", ("", []))[0]
    found.time = info.get("Flight time", ("", []))[0]
    found.alliances = info.get("Alliances", ("", []))[1]
    found.classes = info.get("Classes", ("", []))[1]
    found.aircraft = info.get("Aircrafts", ("", []))[1]
    weekly = re.search(r"operated ([\d,]+) times a week", html)
    found.weekly = int(weekly.group(1).replace(",", "")) if weekly else None
    for label in re.findall(r'<a class="airline-schedule-button btn"[^>]*>.*?</div>(.*?)</a>', html, re.S):
        label = text_of(label)
        code = re.search(r"\s*\(([A-Z0-9]{2,3})\)\s*$", label)
        found.airlines.append({"code": code.group(1) if code else "",
                               "name": label[:code.start()] if code else label})
    window = _section(html, r'<div class="schedule-window" id="schedule-window">')
    found.month = _month_of(html)
    found.flights = timetable(window, found.month)
    dep = re.search(r'var route_dep_id\s*=\s*"(\d+)"', html)
    des = re.search(r'var route_des_id\s*=\s*"(\d+)"', html)
    found.ids = (dep.group(1) if dep else "", des.group(1) if des else "")
    if not found.flights and not found.airlines:
        raise PageChanged("the route page has no airlines and no timetable")
    return found


def schedule_answer(text: str) -> tuple[date | None, list[dict]]:
    """The calendar's answer for another month: its month and its timetable rows."""
    try:
        data = json.loads(text)
        calendar, rows = data["schedule"], data["timetable"]
    except (ValueError, KeyError, TypeError):
        raise PageChanged("the schedule answer is not the calendar's JSON") from None
    month = _month_of(calendar)
    return month, timetable(rows, month)


# ---------------------------------------------------------------- airport pages


@dataclass
class Airport:
    code: str
    city: str
    name: str            # `John F. Kennedy International Airport`
    direction: str       # from | to
    url: str
    summary: str         # the site's own count sentence
    nonstop: list[dict]
    onestop: list[dict]


_SENTENCE_END = re.compile(r"\.\s+(?=(?:This|It|The route)\b)")
# the last of these words starts the airline list: `the only airline with direct flights is X`,
# `Airlines that operate direct flights to <X> are A and B`
_AIRLINES_AFTER = re.compile(r"^.*(?:offered by|operated by|\bare|\bwith|\bis)\s+(.+)$", re.S)


def _airline_list(text: str, catalog: dict[str, list[str]]) -> list[dict]:
    """`American Airlines (Oneworld), Delta (SkyTeam) and JetBlue` -> [{name, code, alliance}]."""
    pieces: list[str] = []
    for part in re.split(r",\s+", text.strip().rstrip(".")):
        words = re.split(r"\s+(?:and|or)\s+", part)
        if len(words) > 1 and re.sub(r"\s*\([^()]+\)\s*$", "", part).casefold() in catalog:
            words = [part]                         # a name with `and` in it
        pieces.extend(w for w in words if w)
    found = []
    for piece in pieces:
        alliance = re.search(r"\(([^()]+)\)\s*$", piece)
        name = piece[:alliance.start()].strip() if alliance else piece.strip()
        found.append({"name": name, "code": _code_for(name, catalog), "alliance": alliance.group(1) if alliance else ""})
    return found


def route_airlines(html: str, catalog: dict[str, list[str]]) -> list[tuple[list[str], list[dict], str, str]]:
    """The sentences under International and Domestic flights: (city names, airlines, note, country)
    each, the country from the flag of the section's heading.

    One sentence names one or more places in <strong> and the airlines that fly to (or from) all of
    them, e.g. `Non-stop flights to <strong>Cancún</strong> are offered by American Airlines
    (Oneworld), Delta (SkyTeam) and JetBlue.`; a following sentence may say the route is seasonal."""
    found = []
    for block_class in ("international-flights", "domestic-flights"):
        block = _section(html, rf'<div class="{block_class}">')
        country = ""
        for heading, paragraph in re.findall(r"<h3>(.*?)</h3>|<p>(.*?)</p>", block, re.S):
            if heading:
                country = _attr((re.search(r"<img[^>]*>", heading) or [""])[0], "title")
                continue
            for sentence in re.split(r"<br\s*/?>", paragraph):
                places = [text_of(p) for p in re.findall(r"<strong>(.*?)</strong>", sentence, re.S)]
                if not places:
                    continue
                tail = text_of(sentence[sentence.rfind("</strong>") + len("</strong>"):])
                parts = _SENTENCE_END.split(tail, maxsplit=1)
                note = parts[1].strip() if len(parts) > 1 else ""
                listed = _AIRLINES_AFTER.search(parts[0])
                if listed:
                    found.append((places, _airline_list(listed.group(1), catalog), note.rstrip("."), country))
    return found


def _destination_rows(block: str) -> list[dict]:
    rows = []
    for tag, body in re.findall(r'(<a class="popular-destination btn[^"]*"[^>]*>)(.*?)</a>', block, re.S):
        label = _attr(tag, "data-a")
        country = _attr((re.search(r"<img[^>]*>", body) or [""])[0], "title")
        right = text_of((re.search(r'<span class="float-right">(.*?)</span>', body, re.S) or [None, ""])[1])
        code = _code_of(label)
        if code:
            rows.append({"code": code, "name": _name_of(label), "country": country,
                         "flights": _int(right) if "flight" in right else None,
                         "url": BASE + _attr(tag, "href")})
    return rows


def _miles(html: str) -> dict[str, int]:
    found: dict[str, int] = {}
    opening = r'<div class="popular-destinations-list" id="flights-within-\d+-miles">'
    for start in re.finditer(opening, html):
        block = element_inner(html, opening, start=start.start()) or ""
        for name, miles in re.findall(r'popular-destination-airport-name">(.*?)</div>\s*'
                                      r'<span class="float-right">\s*([\d,]+) miles', block, re.S):
            code = re.search(r"\(([A-Z0-9]{3})\)", text_of(name))
            if code:
                found.setdefault(code.group(1), int(miles.replace(",", "")))
    return found


def _idle(note: str) -> int:
    """How surely a sentence's note means no flights this month: not flying yet, else a season or
    pause, else nothing said."""
    return 2 if "not operational" in note else 1 if note else 0


def _assign(rows: list[dict], sentences: list[tuple[list[dict], str]]) -> None:
    """Give one place's sentences to its rows. Same-named airports in one country (Orlando MCO and
    SFB) are told apart only by the sentence's note: the idler a sentence, the fewer flights its
    airport has this month. Sentences no note tells apart give each of their airports every
    airline they name, and the note says so; `tie` keeps the airports' codes and their airline
    lists, one an airport, so a filter can count how many of them an airline flies to."""
    pairs = list(zip(sorted(rows, key=lambda r: -(r["flights"] or 0)), sorted(sentences, key=lambda s: _idle(s[1]))))
    for row, (airlines, note) in pairs:
        row["airlines"], row["note"] = airlines, note
    for level in {_idle(note) for _, (_, note) in pairs}:
        tied = [row for row, (_, note) in pairs if _idle(note) == level]
        if len({tuple(a["name"] for a in row["airlines"]) for row in tied}) > 1:
            union = list({a["name"]: a for row in tied for a in row["airlines"]}.values())
            said = f"airlines of {' and '.join(r['code'] for r in tied)} together: the page doesn't say which airport each flies"
            tie = {"codes": [r["code"] for r in tied], "lists": [r["airlines"] for r in tied]}
            for row in tied:
                row["airlines"], row["note"] = union, "; ".join(n for n in (row["note"], said) if n)
                row["tie"] = tie


def airport(html: str, url: str, direction: str) -> Airport:
    heading = text_of((re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S) or [None, ""])[1])
    found = re.match(r"Flights (?:from|to) (.*) \(([A-Z0-9]{3})\)$", heading)
    if not found:
        raise PageChanged("the airport page has no `Flights from <city> (<code>)` heading")
    city, code = found.group(1), found.group(2)
    name = text_of((re.search(r"<h1[^>]*>.*?</h1>\s*<h2>(.*?) - Airport code", html, re.S) or [None, ""])[1])
    summary = text_of((re.search(r"has non-stop passenger flights scheduled to [^.<]*|"
                                 r"there are \d+ airports around the world that have direct flights to [^<]*?"
                                 r"countries", html) or [""])[0])
    nonstop_block = _section(html, r'<div class="popular-destinations-list" id="popular-destinations">')
    if 'id="popular-destinations"' not in html:
        raise PageChanged("the airport page has no popular-destinations list")
    nonstop = _destination_rows(nonstop_block)
    onestop = _destination_rows(_section(html, r'<div class="popular-destinations-list" id="one-stop-destinations">'))
    miles = _miles(html)
    catalog = airline_catalog(html)
    for row in nonstop:
        row.update(miles=miles.get(row["code"]), airlines=[], note="")
    said: dict[tuple[str, str], list[tuple[list[dict], str]]] = {}
    for places, airlines, note, country in route_airlines(html, catalog):
        for place in places:
            said.setdefault((place.casefold(), country), []).append((airlines, note))
    for (place, country), sentences in said.items():
        named = [r for r in nonstop if r["name"].casefold() == place]
        _assign([r for r in named if r["country"] == country] or named, sentences)
    for row in onestop:
        row.pop("flights", None)
    return Airport(code, city, name, direction, url, summary, nonstop, onestop)


# ---------------------------------------------------------------- airline pages


@dataclass
class Airline:
    code: str
    name: str
    url: str
    serves: str          # the site's sentences: `serves 68 domestic destinations and …`
    departs: str
    destinations: list[dict]
    origins: list[dict]
    routes: list[dict]


def _airport_list(block: str) -> list[dict]:
    rows, country = [], ""
    for kind, tag, body in re.findall(r'<a (class="airline-(?:country|destination)[^"]*")([^>]*)>(.*?)</a>', block, re.S):
        if "airline-country" in kind:
            country = text_of(body)
            continue
        label = _attr(tag, "data-a")
        if _code_of(label):
            rows.append({"code": _code_of(label), "name": _name_of(label), "country": country})
    return rows


def airline(html: str, url: str, code: str) -> Airline:
    heading = text_of((re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S) or [None, ""])[1])
    name = re.sub(r"\s+Flights$", "", heading)
    if not name or 'id="destinations-toc"' not in html:
        raise PageChanged("the airline page has no destinations list")

    def after(anchor: str) -> str:
        start = html.find(anchor)
        if start < 0:
            return ""
        return element_inner(html[start:], r'<div class="airline-destinations-list">') or ""

    serves = text_of((re.search(r"(serves [^<]*?)(?:, as of [^<]*)?\.?</p>", html) or [None, ""])[1])
    departs = text_of((re.search(r"(departs from [^<]*?)(?:, as of [^<]*)?\.?</p>", html) or [None, ""])[1])
    routes = []
    for href, label in re.findall(r'<a class="airline-destination airline-top-route btn" href="([^"]+)"[^>]*>'
                                  r'.*?<span class="float-right">(.*?)</span>', after('id="top-routes-toc"'), re.S):
        pair = re.fullmatch(r"([A-Z0-9]{3}) to ([A-Z0-9]{3})", text_of(label))
        if pair:
            routes.append({"origin": pair.group(1), "dest": pair.group(2), "url": BASE + href})
    title_of = {m.group(1): text_of(m.group(2)) for m in
                re.finditer(r'href="(/flights-from-[a-z0-9]+-to-[a-z0-9]+)"\s*title="Flights from (.*?)"', html)}
    for r in routes:
        r["title"] = one_line(title_of.get(r["url"][len(BASE):], ""))
    return Airline(code.upper(), name, url, serves, departs, _airport_list(after('id="destinations-toc"')),
                   _airport_list(after('id="departures-toc"')), routes)
