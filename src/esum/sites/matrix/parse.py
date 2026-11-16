"""Pure parsers for ITA Matrix API responses: JSON in, rows and text out (no ctx, no network)."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import NamedTuple

from esum import one_line

CABIN_NAMES = {"COACH": "economy", "PREMIUM-COACH": "premium", "BUSINESS": "business", "FIRST": "first"}


def api_error(data: dict) -> tuple[str, str] | None:
    """`(type, message)` when Matrix answered with an error object instead of results."""
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return None
    return str(err.get("type") or err.get("status") or ""), one_line(str(err.get("message") or "unknown error"))


def minutes(total: int | None) -> str:
    """1170 → `19h30m`."""
    if total is None:
        return ""
    h, m = divmod(int(total), 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def local(stamp: str | None) -> str:
    """`2027-02-16T06:30-05:00` → `02-16 06:30` (the airport's local time, as Matrix gives it)."""
    if not stamp or len(stamp) < 16:
        return stamp or ""
    return f"{stamp[5:10]} {stamp[11:16]}"


# Matrix itinerary ids are case-sensitive: a 22-character prefix shared by the whole search, then
# a 3-character index (…E009, …E00c, …E00C). Rows show the index as a number, so case can't matter;
# the prefix travels in the `search` value.
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"
PREFIX, INDEX = 22, 3


def id_prefix(ids: list[str]) -> str | None:
    """The prefix every id shares, when they all have Matrix's usual shape."""
    if not ids or any(len(i) != PREFIX + INDEX or any(ch not in ALPHABET for ch in i[PREFIX:]) for i in ids):
        return None
    prefix = ids[0][:PREFIX]
    return prefix if all(i.startswith(prefix) for i in ids) else None


def short_id(raw: str, prefix: str | None) -> str:
    """`GVaDLw2ZdHCLfxaQ1oVkxE00c` → `38` under its search's prefix; other ids stay as they are."""
    if not prefix or not raw.startswith(prefix) or len(raw) != PREFIX + INDEX:
        return raw
    value = 0
    for ch in raw[PREFIX:]:
        if ch not in ALPHABET:
            return raw
        value = value * 64 + ALPHABET.index(ch)
    return str(value)


def full_id(value: str, prefix: str | None) -> str | None:
    """A row number back to Matrix's id; None when `value` is not a row number for this prefix."""
    text = value.strip()
    if not prefix or not text.isdigit() or int(text) >= 64 ** INDEX:
        return None
    n, out = int(text), ""
    for _ in range(INDEX):
        n, r = divmod(n, 64)
        out = ALPHABET[r] + out
    return prefix + out


def search_token(data: dict, prefix: str | None = None) -> str:
    """The value `results` and `detail` take: Matrix's session and solution set, plus the id prefix."""
    session, solution_set = data.get("session"), data.get("solutionSet")
    if not session or not solution_set:
        raise ValueError("Matrix answered without a session or solution set")
    return ".".join(p for p in (session, solution_set, prefix) if p)


def split_token(token: str) -> tuple[str, str, str | None] | None:
    parts = token.strip().split(".")
    if len(parts) not in (2, 3) or not all(parts):
        return None
    return parts[0], parts[1], parts[2] if len(parts) == 3 else None


# --- airports ---------------------------------------------------------------------------------

def location_rows(data: dict) -> list[dict]:
    return [{"code": loc.get("code"), "name": one_line(loc.get("displayName") or ""),
             "type": loc.get("type"), "city": loc.get("cityCode"),
             "timezone": loc.get("timezone")} for loc in data.get("locations") or []]


def city_airports(data: dict, city: str) -> list[str]:
    """The airports a lookup by the city's name lists under `city` (rail stations left out)."""
    return [loc["code"] for loc in data.get("locations") or []
            if loc.get("type") == "airport" and loc.get("cityCode") == city and loc.get("code")]


# --- search and results -----------------------------------------------------------------------

def slice_text(s: dict) -> str:
    """One direction on one line: `JFK 02-16 06:30 → NRT 02-17 16:00 · 19h30m · via BOS · AA4645 AA8475`."""
    stops = [st.get("code") for st in s.get("stops") or [] if st.get("code")]
    parts = [f"{s['origin']['code']} {local(s.get('departure'))} → {s['destination']['code']} {local(s.get('arrival'))}",
             minutes(s.get("duration")),
             f"via {' '.join(stops)}" if stops else "nonstop",
             " ".join(s.get("flights") or [])]
    return " · ".join(p for p in parts if p)


def solution_row(sol: dict, prefix: str | None = None) -> dict:
    itin = sol.get("itinerary") or {}
    slices = itin.get("slices") or []
    warnings = sorted({w for s in slices for w in ((s.get("ext") or {}).get("warnings") or {}).get("types") or []})
    return {
        "id": int(s) if (s := short_id(str(sol.get("id")), prefix)).isdigit() else s,
        "matrix_id": sol.get("id"),
        "price": sol.get("displayTotal") or (sol.get("ext") or {}).get("totalPrice"),
        "itinerary": " | ".join(slice_text(s) for s in slices),
        "carriers": " ".join(c.get("code", "") for c in itin.get("carriers") or []),
        "stops": stops[0] if len(stops := [len(s.get("stops") or []) for s in slices]) == 1 else stops,
        "duration": minutes(sum(s.get("duration") or 0 for s in slices)),
        "warnings": [w.lower().replace("_", " ") for w in warnings],
        "slices": [{"origin": s["origin"]["code"], "destination": s["destination"]["code"],
                    "departure": s.get("departure"), "arrival": s.get("arrival"),
                    "duration": s.get("duration"), "flights": s.get("flights") or [],
                    "stops": [st.get("code") for st in s.get("stops") or []],
                    "cabins": [CABIN_NAMES.get(c, c.lower()) for c in s.get("cabins") or []]} for s in slices],
    }


@dataclass
class SolutionPage:
    token: str
    rows: list[dict]
    total: int
    page: int
    pages: int
    currency: str | None = None
    carrier_min: dict[str, str] = field(default_factory=dict)   # carrier code (`multiple`) -> cheapest price
    stops_min: dict[str, str] = field(default_factory=dict)     # `nonstop`, `1 stop`, ... -> cheapest price

    @property
    def by_carrier(self) -> str:
        return summary(self.carrier_min, by_price=True)

    @property
    def by_stops(self) -> str:
        return summary(self.stops_min)

    @property
    def carriers(self) -> list[str]:
        """Every carrier this search priced, its summary's and its rows' (mixed itineraries name theirs)."""
        found = [c for c in self.carrier_min if c != "multiple"]
        found += [c for r in self.rows for c in r["carriers"].split()]
        return list(dict.fromkeys(found))


def summary(prices: dict[str, str], *, by_price: bool = False) -> str:
    """`KE USD568.00 · JL USD795.00` from label -> cheapest price."""
    items = sorted(prices.items(), key=lambda kv: amount(kv[1])) if by_price else list(prices.items())
    return " · ".join(f"{label} {price}" for label, price in items)


def _groups(summary: dict | None, label) -> dict[str, str]:
    return {label(g.get("label")): g["minPrice"] for g in (summary or {}).get("groups") or [] if g.get("minPrice")}


def _carrier_label(label) -> str:
    if isinstance(label, dict):
        return "multiple" if label.get("code") == "*" else str(label.get("code") or label.get("shortName") or "?")
    return str(label)


def _stops_label(label) -> str:
    return "nonstop" if label == 0 else f"{label} stop" + ("s" if label != 1 else "")


def solution_page(data: dict, prefix: str | None = None) -> SolutionPage:
    """One page of itineraries. `prefix` is the search's id prefix when known (from its `search`
    value); otherwise it is read off this page's ids."""
    sl = data.get("solutionList")
    if not isinstance(sl, dict):
        raise ValueError("Matrix answered without a solution list")
    pages = sl.get("pages") or {}
    solutions = sl.get("solutions") or []
    prefix = prefix or id_prefix([str(s.get("id")) for s in solutions])
    return SolutionPage(
        token=search_token(data, prefix),
        rows=[solution_row(s, prefix) for s in solutions],
        total=int(sl.get("solutionCount") or data.get("solutionCount") or 0),
        page=int(pages.get("current") or 1), pages=int(pages.get("count") or 0),
        currency=currency_of(solutions[0].get("displayTotal")) if solutions else None,
        carrier_min=_groups(data.get("itineraryCarrierList"), _carrier_label),
        stops_min=_groups(data.get("itineraryStopCountList"), _stops_label),
    )


def rows_route(rows: list[dict]) -> tuple[str, str]:
    """Route and dates as the listed itineraries show them, slice by slice:
    `JFK→HND,NRT | HND,NRT→JFK` and `2027-02-16 | 2027-02-23`."""
    count = max((len(r["slices"]) for r in rows), default=0)
    route, dates = [], []
    for i in range(count):
        legs = [r["slices"][i] for r in rows if len(r["slices"]) > i]
        o = sorted({s["origin"] for s in legs})
        d = sorted({s["destination"] for s in legs})
        days = sorted({(s["departure"] or "")[:10] for s in legs} - {""})
        route.append(f"{','.join(o)}→{','.join(d)}")
        dates.append(days[0] if len(days) == 1 else f"{days[0]} – {days[-1]}" if days else "?")
    return " | ".join(route), " | ".join(dates)


# --- calendar ---------------------------------------------------------------------------------

def _day(stamp: str | None) -> str | None:
    return stamp[:10] if stamp and len(stamp) >= 10 else None


def calendar_rows(data: dict) -> list[dict]:
    """One row per departure date that has a fare, in date order. Round trips also give the best
    trip length (`nights`), its return date, and the price of every length searched (`by_nights`)."""
    cal = data.get("calendar") or data.get("calendarOneWay")
    if not isinstance(cal, dict):
        raise ValueError("Matrix answered without a calendar")
    rows = []
    for month in cal.get("months") or []:
        for wi, week in enumerate(month.get("weeks") or []):
            for day in week.get("days") or []:
                if day.get("disabled") or not day.get("minPrice"):
                    continue
                # the first and last weeks are padded with the neighbouring months' days, which
                # carry only a day number; those days also come under their own month
                if (wi == 0 and int(day["date"]) > 20) or (wi >= 3 and int(day["date"]) < 10):
                    continue
                d = date(int(month["year"]), int(month["month"]), int(day["date"]))
                row = {"date": d.isoformat(), "price": day["minPrice"], "solutions": day.get("solutionCount")}
                options = ((day.get("tripDuration") or {}).get("options")) or []
                if options:
                    best = min((o for o in options if o.get("minPrice")),
                               key=lambda o: (amount(o["minPrice"]), o.get("tripLength", 0)), default=None)
                    if best:
                        legs = (best.get("solution") or {}).get("slices") or []
                        row["nights"] = best.get("tripLength")
                        row["return"] = _day(legs[1].get("departure")) if len(legs) > 1 else None
                    row["by_nights"] = " · ".join(f"{o.get('tripLength')}n {o['minPrice']}"
                                                  for o in options if o.get("minPrice"))
                rows.append(row)
    return sorted(rows, key=lambda r: r["date"])


def amount(price: str | None) -> float:
    """`USD7212.99` → 7212.99 (inf when there is no number)."""
    digits = "".join(ch for ch in price or "" if ch.isdigit() or ch == ".")
    return float(digits) if digits else float("inf")


def currency_of(price: str | None) -> str | None:
    """`JPY27470` → `JPY`."""
    code = "".join(ch for ch in (price or "")[:3] if ch.isalpha())
    return code if len(code) == 3 else None


# --- detail -----------------------------------------------------------------------------------

@dataclass
class Booking:
    title: str
    price: str | None
    lines: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)


def _segment_line(seg: dict) -> list[str]:
    carrier = (seg.get("carrier") or {}).get("code", "")
    flight = f"{carrier}{(seg.get('flight') or {}).get('number', '')}"
    op = seg.get("operationalFlight") or {}
    if op:
        flight += f" (operated as {(op.get('carrier') or {}).get('code', '')}{op.get('number', '')})"
    booking = " ".join(f"{b.get('bookingCode')} {CABIN_NAMES.get(str(b.get('cabin')), str(b.get('cabin', '')).lower())}"
                       for b in seg.get("bookingInfos") or [])
    legs = seg.get("legs") or []
    aircraft = ", ".join(sorted({(leg.get("aircraft") or {}).get("shortName", "") for leg in legs} - {""}))
    head = (f"{flight}  {seg['origin']['code']} {local(seg.get('departure'))} → "
            f"{seg['destination']['code']} {local(seg.get('arrival'))} · {minutes(seg.get('duration'))}")
    extra = [p for p in (aircraft, f"class {booking}" if booking else "") if p]
    out = [f"  {head}" + (f" · {' · '.join(extra)}" if extra else "")]
    disclosure = (seg.get("ext") or {}).get("operationalDisclosure")
    marketed = str((seg.get("carrier") or {}).get("shortName") or "").lower()
    # an operating flight number already says who flies it; "operated by <the same airline>" says nothing
    if disclosure and not op and one_line(disclosure).lower().removeprefix("operated by ").strip() != marketed:
        out.append(f"      {_disclosure(disclosure)}")
    if len(legs) > 1:   # a direct flight with a stop on the way: one flight number, several legs
        out.append("      Stops on the way: " + ", ".join(leg["destination"]["code"] for leg in legs[:-1]))
    conn = seg.get("connection") or {}
    if conn:
        bits = [f"Connection at {seg['destination']['code']}: {minutes(conn.get('duration'))}"]
        if conn.get("changeOfTerminal"):
            bits.append("change of terminal")
        if conn.get("changeOfAirport"):
            bits.append("change of airport")
        out.append("      " + ", ".join(bits))
    return out


def _disclosure(text: str) -> str:
    """`OPERATED BY REPUBLIC AIRWAYS AS AMERICAN EAGLE` → `Operated by Republic Airways as American Eagle`."""
    words = one_line(text).title().split()
    small = {"By", "As", "For", "Of", "And", "Dba"}
    return " ".join(w.lower() if i and w in small else w for i, w in enumerate(words))


def operators(data: dict) -> str:
    """Who flies each segment of one itinerary's booking details: `AA8475 by JL7 · AA4645 by
    Republic Airways as American Eagle`, or `as marketed` when every flight is its own airline's."""
    out = []
    for s in ((data.get("bookingDetails") or {}).get("itinerary") or {}).get("slices") or []:
        for seg in s.get("segments") or []:
            carrier = seg.get("carrier") or {}
            flight = f"{carrier.get('code', '')}{(seg.get('flight') or {}).get('number', '')}"
            op = seg.get("operationalFlight") or {}
            disclosure = (seg.get("ext") or {}).get("operationalDisclosure")
            if op:
                out.append(f"{flight} by {(op.get('carrier') or {}).get('code', '')}{op.get('number', '')}")
            elif disclosure and one_line(disclosure).lower().removeprefix("operated by ").strip() != \
                    str(carrier.get("shortName") or "").lower():
                out.append(f"{flight} by {_disclosure(disclosure).removeprefix('Operated by ')}")
    return " · ".join(out) or "as marketed"


_ONE = {"adults": "adult", "children": "child"}      # one passenger of a kind, as Matrix names the kinds


def booking(data: dict, traits: dict[str, str] | None = None) -> Booking:
    """One itinerary's booking details; `traits` maps a fare's key to a line about its kind
    (`fare_traits`), printed under that fare."""
    b = data.get("bookingDetails")
    if not isinstance(b, dict):
        raise ValueError("Matrix answered without booking details")
    slices = (b.get("itinerary") or {}).get("slices") or []
    route = "→".join([slices[0]["origin"]["code"], *(s["destination"]["code"] for s in slices)]) if slices else "?"
    price = b.get("displayTotal") or (b.get("ext") or {}).get("totalPrice")
    pax = b.get("passengerCount")
    lines = [f"Total: {price}" + (f" for {pax} passenger{'s' if pax != 1 else ''}" if pax else "")]
    for i, s in enumerate(slices, 1):
        stops = s.get("stopCount", 0)
        warn = (s.get("ext") or {}).get("warnings")
        lines.append("")
        lines.append(f"Slice {i}: {s['origin']['code']} → {s['destination']['code']}, "
                     f"{local(s.get('departure'))} → {local(s.get('arrival'))}, "
                     + ("nonstop" if not stops else f"{stops} stop{'s' if stops != 1 else ''}")
                     + (f" ({warn.lower().replace('_', ' ').replace(',', ', ')})" if warn else ""))
        for seg in s.get("segments") or []:
            lines += _segment_line(seg)

    fares, calcs, taxes, notes = [], [], [], []
    for ticket in b.get("tickets") or []:
        for pricing in ticket.get("pricings") or []:
            pax_types = " ".join(f"{n} {k}" for k, n in ((pricing.get("ext") or {}).get("pax") or {}).items())
            for fare in pricing.get("fares") or []:
                classes = " ".join(bi.get("bookingCode", "") for bi in fare.get("bookingInfos") or [])
                fares.append({"key": fare.get("key"), "basis": fare.get("code"), "carrier": fare.get("carrier"),
                              "market": f"{fare.get('originCity')}-{fare.get('destinationCity')}",
                              "price": fare.get("displayAdjustedPrice"), "type": fare.get("tag"),
                              "classes": classes, "pax": fare.get("ptcs") or [], "for": pax_types,
                              "flights": [((bi.get("segment") or {}).get("origin"),
                                           (bi.get("segment") or {}).get("destination"))
                                          for bi in fare.get("bookingInfos") or []]})
            for calc in pricing.get("fareCalculations") or []:
                calcs += calc.get("lines") or []
            travellers = sum(n for n in ((pricing.get("ext") or {}).get("pax") or {}).values()
                             if isinstance(n, int)) or 1
            for tax in (pricing.get("ext") or {}).get("taxTotals") or []:
                taxes.append({"passengers": travellers,     # Matrix gives a tax per passenger, the total for all
                              "for": " ".join(f"{n} {_ONE.get(k, k) if n == 1 else k}" for k, n in
                                              ((pricing.get("ext") or {}).get("pax") or {}).items()),
                              "code": tax.get("code"), "name": (tax.get("tax") or {}).get("name"),
                              "amount": tax.get("totalDisplayPrice")})
            notes += pricing.get("notes") or []

    if fares:
        lines += ["", "Fares:"]
        for f in fares:
            lines.append(f"  {f['basis']}  {f['carrier']} {f['market']} {f['price']} {str(f['type'] or '').lower()}"
                         + (" a passenger" if pax and pax > 1 else "")
                         + f" · booking class {f['classes']}" + (f" · {' '.join(f['pax'])}" if f["pax"] else ""))
            if (traits or {}).get(f["key"]):
                f["kind"] = traits[f["key"]]
                lines.append(f"      {traits[f['key']]}")
    if calcs:
        lines += ["", "Fare calculation:"] + [f"  {one_line(c)}" for c in calcs]
    if taxes:
        cur = currency_of(price) or ""
        carrier = sum(amount(t["amount"]) * t["passengers"] for t in taxes if t["code"] in ("YQ", "YR"))
        government = sum(amount(t["amount"]) * t["passengers"] for t in taxes if t["code"] not in ("YQ", "YR"))
        each = ", per passenger" if any(t["passengers"] > 1 for t in taxes) else ""
        total = amount(price)
        split = [f"fares {cur}{total - carrier - government:.2f} (total less the charges below)"] if total != float("inf") else []
        split += [f"carrier-imposed (YQ/YR) {cur}{carrier:.2f}", f"government taxes and fees {cur}{government:.2f}"]
        lines += ["", f"Price split{', all passengers' if each else ''}: " + " · ".join(split)]
        lines += ["", f"Taxes and carrier charges (carrier-imposed first{each}):"]
        groups = list(dict.fromkeys(t["for"] for t in taxes))      # one per kind of passenger (adults, children)
        for group in groups:
            pad = "  "
            if len(groups) > 1:
                lines.append(f"  {group}:")
                pad = "    "
            ordered = sorted((t for t in taxes if t["for"] == group), key=lambda t: t["code"] not in ("YQ", "YR"))
            lines += [f"{pad}{t['code']} {t['amount']}  {one_line(t['name'] or '')}"
                      + ("  [carrier-imposed]" if t["code"] in ("YQ", "YR") else "") for t in ordered]
    if notes:
        lines += ["", "Notes:"] + [f"  {one_line(n)}" for n in dict.fromkeys(notes)]
    return Booking(title=f"{route} · {price}", price=price, lines=lines,
                   data={"price": price, "passengers": pax, "slices": slices, "fares": fares,
                         "fare_calculation": calcs, "taxes": taxes, "notes": list(dict.fromkeys(notes))})


GOOGLE_CABINS = {"COACH": "economy", "PREMIUM-COACH": "premium", "BUSINESS": "business", "FIRST": "first"}


def google_fares(data: dict) -> dict | None:
    """The `esum gflights fares` arguments naming one itinerary's flights (`booking().data`), or
    None when that command can't name the trip:

    - more than two slices: it takes a one way or a round trip;
    - passengers other than adults: its booking token carries adults only;
    - the slices fly different cabins: Google prices one cabin for the whole trip. Within a slice
      the cabin is the longest flight's, so a domestic first feeder to a business flight is business;
    - a flight number with a stop on the way (several legs): Google lists each leg as a flight.

    A codeshare goes by its operating flight number (NH7 for UA7949): Google's booking page lists
    a flight under that one and shows nothing for the codeshare's number. A flight a regional
    partner flies under the airline's own number has no other number and keeps it."""
    slices = data.get("slices") or []
    kinds = {f.get("for") for f in data.get("fares") or []}
    party = re.fullmatch(r"(\d+) adults", next(iter(kinds)) or "") if len(kinds) == 1 else None
    if not 1 <= len(slices) <= 2 or not party:
        return None
    flights, cabins = [], set()
    for s in slices:
        segments = s.get("segments") or []
        parts = []
        for seg in segments:
            day = str(seg.get("departure") or "")[:10]
            op = seg.get("operationalFlight") or {}
            carrier = (op.get("carrier") or seg.get("carrier") or {}).get("code")
            number = op.get("number") or (seg.get("flight") or {}).get("number")
            origin, dest = (seg.get("origin") or {}).get("code"), (seg.get("destination") or {}).get("code")
            if not (carrier and number and origin and dest and len(day) == 10) or len(seg.get("legs") or []) > 1:
                return None
            parts.append(f"{origin}-{dest}:{day}:{carrier}{number}")
        if not parts:
            return None
        longest = max(segments, key=lambda seg: seg.get("duration") or 0)
        cabins.add(next((GOOGLE_CABINS.get(str(b.get("cabin"))) for b in longest.get("bookingInfos") or []), None))
        flights.append("+".join(parts))
    if len(cabins) != 1 or None in cabins:
        return None
    currency = currency_of(data.get("price"))
    cabin, adults = cabins.pop(), int(party.group(1))      # the command's defaults stay out of it
    return {"flights": flights, "cabin": cabin if cabin != "economy" else None,
            "adults": adults if adults != 1 else None, "currency": currency if currency != "USD" else None}


_NOTE_WORDS = (("this ticket is ", ""), (" to this ticket", ""), (" of this ticket", ""),
               ("will incur a penalty fee", "fee"), ("no changes may be made", "no changes"),
               ("changes fee", "change fee"), ("cancellation fee", "cancel fee"))


def short_note(note: str) -> str:
    text = one_line(note).rstrip(".").lower()
    for long, short in _NOTE_WORDS:
        text = text.replace(long, short)
    return text


CABIN_ORDER = ("economy", "premium", "business", "first")


_PENALTY_HEADINGS = {"CANCELLATIONS": "cancel", "CHANGES": "change"}
_PENALTY_TIMES = ("ANY TIME", "BEFORE DEPARTURE", "AFTER DEPARTURE")
PENALTY_WIDTH = 160     # characters a section keeps: its first statements, the ones that name the charge


def penalty_sections(text: str) -> dict[str, str]:
    """Category 16's statements under `CANCELLATIONS` and `CHANGES`, quoted as filed, by section
    (`cancel`, `change`): `any time: TICKET IS NON-REFUNDABLE.` The notes below a statement
    (waivers, service fees, how a reissue is priced) are left to `rules`; a time whose terms
    are all notes reads `after departure: in the notes.`"""
    sections: dict[str, list[list[str]]] = {}      # per section, blocks of [time or "", statements…]
    name, depth, in_note = None, 0, False
    for line in text.splitlines():
        words = line.strip()
        if not words:
            continue
        indent = len(line) - len(line.lstrip())
        if words in _PENALTY_HEADINGS:
            name, depth, in_note = _PENALTY_HEADINGS[words], indent, False
            sections.setdefault(name, [])
            continue
        if name is None or indent <= depth:
            name = None
            continue
        blocks = sections[name]
        if words.startswith("NOTE"):
            in_note = True
        elif indent == depth + 2 and words in _PENALTY_TIMES:
            in_note = False
            blocks.append([words.lower() + ":"])
        elif indent == depth + 2 or (indent == depth + 4 and not in_note
                                     and (not blocks or len(blocks[-1]) == 1 or blocks[-1][-1].endswith("."))):
            in_note = False
            if not blocks or indent == depth + 2 and blocks[-1][0]:
                blocks.append([""])     # statements filed without a time
            blocks[-1].append(words)
        elif not in_note and blocks and len(blocks[-1]) > 1:
            blocks[-1][-1] += " " + words       # a statement wrapped onto the next line
    out = {}
    for name, blocks in sections.items():
        said = []
        for time, *statements in blocks:
            statements = list(dict.fromkeys(statements)) or [IN_NOTES]
            said.append(" ".join([time, *statements]).strip())
        out[name] = " ".join(said)
    return out


def cut(text: str, width: int | None = PENALTY_WIDTH) -> str:
    return text if width is None or len(text) <= width else text[:width].rstrip() + "…"


# --- fare ladder -------------------------------------------------------------------------------
# A fare is the unit: a condition judges fares, an itinerary fits when none of its fares fails, and
# a failing fare is left out of the next search. Leaving out a failing fare never hides an
# itinerary that fits.

IN_NOTES = "in the notes."      # what a time's statements read when its terms are all notes
_IN_NOTES = re.compile(r"(?:any time|before departure|after departure): " + re.escape(IN_NOTES))


@dataclass(frozen=True)
class Fare:
    name: str       # carrier, city pair and basis code, designator included (`UA.SFO+TYO.KLX07JBH`, `UA.EWR+TYO.JX2WC/CH`)
    basis: str      # the fare basis code as Matrix prints it
    market: str     # the cities it prices between, in the direction flown (`SFO-TYO`)
    kind: str       # `basic`, `sell-up` or blank, from its rule title
    cancel: str     # category 16's statements under CANCELLATIONS; blank when its rules weren't read
    change: str     # … under CHANGES
    above: bool     # it prices a flight of a slice flown wholly in a higher cabin than asked
    slices: tuple[int, ...] = ()    # the slices whose flights it prices, 1 the first; () when Matrix doesn't say

    @property
    def skips(self) -> tuple[str, ...]:
        """How a search leaves it out: on each slice it prices (`2:UA.TYO+SFO.SLW47FBH`). A slice
        is flown one way, so its city pair names one direction. On every slice when Matrix
        doesn't say which flights it prices."""
        return tuple(f"{n}:{self.name}" for n in self.slices) or (self.name,)

    @property
    def stated(self) -> str:
        """Its cancellation statements, without the times whose terms are all notes."""
        return _IN_NOTES.sub("", self.cancel).strip()

    def quote(self, width: int | None = PENALTY_WIDTH) -> str:
        """`cancel: any time: TICKET IS NON-REFUNDABLE. · change: any time: CHARGE GBP 150.00 FOR REISSUE.`"""
        return " · ".join(f"{name}: {cut(said, width)}" for name, said in (("cancel", self.cancel),
                                                                           ("change", self.change)) if said)


@dataclass
class Rung:
    """One itinerary as a ladder rung."""
    classes: str            # booking classes, slice by slice (`K | S`)
    fares: list[Fare]
    notes: str              # Matrix's ticket notes, shortened
    higher: dict[str, str]  # booking class -> cabin, of the slices flown wholly in a higher cabin than asked

    @property
    def skips(self) -> tuple[str, ...]:
        """How a search leaves out all its fares."""
        return tuple(dict.fromkeys(s for f in self.fares for s in f.skips))

    @property
    def key(self) -> tuple:
        """What makes two itineraries the same rung: the same fares on the same slices, in any order,
        in the same cabins."""
        return tuple(sorted((f.name, f.basis, f.market, f.slices) for f in self.fares)), bool(self.higher)

    @property
    def kind(self) -> str:
        return ", ".join(dict.fromkeys([f.kind for f in self.fares if f.kind]
                                       + [f"{code} is {cabin}" for code, cabin in self.higher.items()]))

    @property
    def penalties(self) -> str:
        return " | ".join(dict.fromkeys(q for q in (f.quote() for f in self.fares) if q))


def rung(data: dict, cabin: str, rules: Mapping[str, tuple[str, str]] = {}) -> Rung:
    """A rung from an itinerary's booking details (`Booking.data`), for a search in `cabin`.
    `rules` holds, by fare key, what `fare_traits` says of the fare and its category 16 text.

    A slice is in a higher cabin when every flight of it is: premium economy out on an economy
    search. A business slice with a domestic connection in first is a business slice. A fare is
    above when it prices a flight of such a slice."""
    upper_cabins = CABIN_ORDER[CABIN_ORDER.index(cabin) + 1:]
    per_slice, higher, upper_flights, slice_of = [], {}, set(), {}
    for n, s in enumerate(data["slices"], 1):
        segments = s.get("segments") or []
        for seg in segments:
            flight = ((seg.get("origin") or {}).get("code"), (seg.get("destination") or {}).get("code"))
            slice_of.setdefault(flight, set()).add(n)
        flown = [(str(b["bookingCode"]), CABIN_NAMES.get(str(b.get("cabin")), ""))
                 for seg in segments for b in seg.get("bookingInfos") or [] if b.get("bookingCode")]
        per_slice.append(" ".join(dict.fromkeys(code for code, _ in flown)))
        if flown and all(name in upper_cabins for _, name in flown):
            higher.update(flown)
            upper_flights |= {((seg.get("origin") or {}).get("code"), (seg.get("destination") or {}).get("code"))
                              for seg in segments}
    fares = []
    for f in data["fares"]:
        traits, sixteen = rules.get(f.get("key"), ("", ""))
        filed = penalty_sections(sixteen)
        basis, market, carrier = str(f["basis"]), str(f.get("market") or ""), str(f.get("carrier") or "")
        pair = market.replace("-", "+") if re.fullmatch(r"[A-Z]{3}-[A-Z]{3}", market) else ""
        flights = f.get("flights") or []
        fares.append(Fare(
            name=".".join(p for p in (carrier, pair if carrier else "", basis) if p), basis=basis, market=market,
            kind="basic" if traits.startswith("unbundled (basic)") else "sell-up" if traits.startswith("sell-up") else "",
            cancel=filed.get("cancel", ""), change=filed.get("change", ""),
            above=any(flight in upper_flights for flight in flights),
            # a flight on two slices (the same airports twice) leaves the fare on both; one Matrix
            # doesn't place, on every slice
            slices=() if not flights or any(fl not in slice_of for fl in flights)
            else tuple(sorted({n for fl in flights for n in slice_of[fl]}))))
    return Rung(classes=" | ".join(per_slice), fares=fares,
                notes="; ".join(short_note(n) for n in data["notes"]), higher=higher)


@dataclass(frozen=True)
class Condition:
    """What the ladder climbs to: a test a fare fails, in words for the output."""
    label: str                      # `not basic`, `refundable`, `"CHANGES PERMITTED"`; blank for none
    fits: str                       # what `stopped` says of the rung that meets it
    fails: Callable[[Fare], bool]
    words: bool = False             # the test is words to find in the fare's statements

    def meets(self, rung: Rung) -> bool:
        return bool(self.label) and not rung.higher and not any(self.fails(f) for f in rung.fares)

    def leaves_out(self, rung: Rung) -> list[str]:
        """The fares of a rung that doesn't meet the condition which no later rung may hold: those
        of a slice in a higher cabin, else those that fail. All of them when Matrix doesn't say
        which flights a fare prices and none fails."""
        named = [s for f in rung.fares if f.above for s in f.skips] if rung.higher else []
        return list(dict.fromkeys(named or [s for f in rung.fares if self.fails(f) for s in f.skips] or rung.skips))


_NON_REFUNDABLE = re.compile(r"NON[\s-]*REFUNDABLE")
PRESETS = {
    "not-basic": Condition("not basic", "has no basic fare by its rule titles", lambda f: f.kind == "basic"),
    # a fare with no cancellation statement can't be called refundable: its terms are unread, or all notes
    "refundable": Condition("refundable", "files no non-refundable cancellation statement",
                            lambda f: not f.stated or bool(_NON_REFUNDABLE.search(f.stated))),
}
NO_CONDITION = Condition("", "", lambda f: True)   # every fare is left out once seen: a list of fares


def condition(until: str | None) -> Condition:
    """`--until`: a preset, or words every fare's cancellation or change statements must hold."""
    if until is None:
        return NO_CONDITION
    if until.strip().lower() in PRESETS:
        return PRESETS[until.strip().lower()]
    wanted = " ".join(until.upper().split())
    if not wanted:
        raise ValueError("no words")
    return Condition(f'"{wanted}"', f'files "{wanted}" on every fare',
                     lambda f: wanted not in " ".join(f"{f.cancel} {f.change}".upper().split()), words=True)


FILTER_CODE = 10    # characters a fare filter takes for a basis code, a designator and a wildcard included
_SKIP_TEXT = re.compile(r"(?:(\d+):)?(?:([A-Z0-9]{2})\.(?:([A-Z]{3})\+([A-Z]{3})\.)?)?([A-Z0-9]+(?:/[A-Z0-9]+)?)")


class Skip(NamedTuple):
    """A fare to leave out, as the ladder prints it: `[slice:][carrier.[city+city.]]basis`. A basis
    without a designator is that code with none; blanks are any."""
    slice: int | None
    carrier: str
    pair: str       # `SFO+TYO`; either way round names the same fares
    basis: str

    @classmethod
    def of(cls, text: str) -> Skip | None:
        m = _SKIP_TEXT.fullmatch(text)
        if not m:
            return None
        return cls(int(m[1]) if m[1] else None, m[2] or "", f"{m[3]}+{m[4]}" if m[3] else "", m[5])

    @property
    def head(self) -> str:
        return f"{self.carrier}.{self.pair}."

    @property
    def term(self) -> str:
        """The filter term. `X/CH` names that designator; `X` is the code without one, which
        Matrix's `X` alone isn't (it takes every designator of X along), so the term subtracts
        them: `(UA..X&~UA..X/-)`. A code too long for that, or for its designator, goes wide."""
        base, _, designator = self.basis.partition("/")
        if designator:
            if len(self.basis) <= FILTER_CODE:
                return self.head + self.basis
            return f"{self.head}{base}/-" if len(base) + 2 <= FILTER_CODE else self.head + base
        return f"({self.head}{base}&~{self.head}{base}/-)" if len(base) + 2 <= FILTER_CODE else self.head + base

    @property
    def exact(self) -> bool:
        """The term leaves out the fares this names and no others."""
        return len(self.basis) <= FILTER_CODE if "/" in self.basis else len(self.basis) + 2 <= FILTER_CODE

    def covers(self, fare: Fare, *, strict: bool = False) -> bool:
        """Whether the term leaves `fare` out: on a slice it prices (any, when Matrix doesn't say
        and not `strict`), of its carrier, city pair and code."""
        if self.slice is not None and self.slice not in fare.slices and (strict or fare.slices):
            return False
        own = Skip.of(fare.name)
        if own is None or (self.carrier and self.carrier != own.carrier) or \
                (self.pair and sorted(self.pair.split("+")) != sorted(own.pair.split("+"))):
            return False
        if self.exact:
            return fare.basis == self.basis
        base, fare_base = self.basis.partition("/")[0], fare.basis.partition("/")[0]
        # wide: `X/-` takes every designator of X, a bare `X` the code with and without one
        return base == fare_base and ("/" in fare.basis or not self.term.endswith("/-"))


def without(left_out: list[str], slice_: int) -> str | None:
    """The fare filter for one slice: the fares left out on it or on every slice."""
    terms = [s.term for s in map(Skip.of, left_out) if s and s.slice in (None, slice_)]
    return "f ~(" + "|".join(dict.fromkeys(terms)) + ")" if terms else None


@dataclass
class Step:
    """One rung with where it was read: the search row (`price`, `itinerary`, `id`, `search`)."""
    row: dict
    rung: Rung
    number: int = 0


class Climb:
    """The ladder between searches: the rungs met, the fares to leave out, and the rung that fits.

    A rung that fits at the top of a search is the answer: every cheaper itinerary held a fare
    that fails. One that fits further down is a candidate: a dearer fare on a cheaper rung's
    flights may still fit, and the next search says."""

    def __init__(self, condition: Condition, left_out: list[str]):
        self.condition = condition
        self.left_out = list(left_out)
        self.steps: dict[tuple, Step] = {}      # one per rung (`Rung.key`), at its cheapest itinerary
        self.answer: Step | None = None
        self.candidate: Step | None = None
        self.passing: dict[tuple, Fare] = {}    # the fares read that meet the condition
        self.leaked: list[str] = []     # fares a search listed on a slice its filter left them out of

    @property
    def some_pass(self) -> bool:
        """Some fare met the condition."""
        return bool(self.passing)

    def hidden(self) -> list[str]:
        """Fares read that meet the condition and that the filter leaves out all the same, because
        its term for another fare is wider than that fare: a rung with one of them can be cheaper
        than the answer. Fares left out on purpose (a higher cabin) aren't counted."""
        skips = [s for s in map(Skip.of, self.left_out) if s]
        return list(dict.fromkeys(f.name for f in self.passing.values()
                                  if not set(f.skips) & set(self.left_out) and any(s.covers(f) for s in skips)))

    def wide(self) -> list[str]:
        """What is left out by a term wider than the fare read: a code too long for the filter to
        name alone. The code's other fares on that slice went out unread, whoever asked for it:
        this climb or the `--skip` that started it."""
        return [entry for entry in self.left_out if (skip := Skip.of(entry)) and not skip.exact]

    def _keep(self, row: dict, rung: Rung) -> Step:
        """The step of this rung, moved to this itinerary when it is the cheaper."""
        step = self.steps.setdefault(rung.key, Step(row, rung))
        if amount(row["price"]) < amount(step.row["price"]):
            step.row, step.rung = row, rung
        return step

    def take(self, read: list[tuple[dict, Rung | None]]) -> bool:
        """One search's itineraries, cheapest first, with their rungs (None when a read failed).
        True when the climb can go on: no answer yet, and a new fare to leave out."""
        new: list[str] = []
        skips = [s for s in map(Skip.of, self.left_out) if s]
        for i, (row, rung) in enumerate(read):
            if rung is None:
                continue
            step = self._keep(row, rung)
            for f in rung.fares:
                if not self.condition.fails(f):
                    self.passing.setdefault((f.name, f.slices), f)
                if any(s.covers(f, strict=True) for s in skips) and f.name not in self.leaked:
                    self.leaked.append(f.name)
            if self.condition.meets(rung):
                if i == 0:
                    step.row, step.rung = row, rung     # where this search lists it first
                    self.answer, self.candidate = step, None
                elif self.candidate is None or amount(step.row["price"]) < amount(self.candidate.row["price"]):
                    self.candidate = step
                break       # the itineraries after it cost more
            new += [name for name in self.condition.leaves_out(rung) if name not in self.left_out and name not in new]
        self.left_out += new
        return self.answer is None and bool(new)

    def rungs(self) -> list[Step]:
        """The rungs met, cheapest first, numbered."""
        ordered = sorted(self.steps.values(), key=lambda s: amount(s.row["price"]))
        for number, step in enumerate(ordered, 1):
            step.number = number
        return ordered


# --- fare rules --------------------------------------------------------------------------------

# ATPCO rule categories, as Matrix's rules page numbers them
CATEGORIES = {
    0: "general notes and booking classes", 1: "eligibility", 2: "days and times", 3: "seasons",
    4: "flight restrictions", 5: "advance reservation and ticketing", 6: "minimum stay", 7: "maximum stay",
    8: "stopovers", 9: "transfers", 10: "combinations", 11: "blackout dates", 12: "surcharges",
    13: "accompanied travel", 14: "travel restrictions", 15: "sales restrictions", 16: "penalties",
    17: "higher intermediate point and mileage exceptions", 18: "ticket endorsements", 19: "children discounts",
    20: "tour conductor discounts", 21: "agent discounts", 22: "other discounts", 23: "miscellaneous provisions",
    25: "fare by rule", 26: "groups", 27: "tours", 28: "visit another country", 29: "deposits",
    31: "voluntary changes", 33: "voluntary refunds", 35: "negotiated fares", 50: "application and other conditions",
}
# a booking-class chart row: carrier, classes, then a note such as R/DA or P/BA; the last class
# is the one the fare books, the ones before it must have seats as well (dual RBD)
_CHART_ROW = re.compile(r"\s([A-Z0-9]{2})\s+((?:[A-Z]\s+)+[A-Z])\s+[A-Z]/[A-Z]{2}\s*$")


def rule_sections(data: dict) -> list[dict]:
    """A fare's rules, one entry per category in Matrix's order: `{category, name, kind, text}`."""
    out = []
    for rule_set in (data.get("fareRules") or {}).get("ruleSets") or []:
        for rule in rule_set.get("rules") or []:
            cat = rule.get("category")
            text = "\n".join(b.rstrip() for b in rule.get("blocks") or []).strip("\n")
            text = "\n".join(line.rstrip() for line in text.splitlines())
            out.append({"category": cat, "name": CATEGORIES.get(cat, f"category {cat}"),
                        "kind": str(rule.get("type") or "").lower().replace("-", " ") or None, "text": text})
    return out


def fare_traits(sections: list[dict]) -> str:
    """What the general notes (category 0) say about the fare's kind: unbundled (basic), sell-up,
    and the booking classes that must have seats as well as the booked one (dual RBD)."""
    notes = next((s["text"] for s in sections if s["category"] == 0), "")
    if not notes:
        return ""
    lines = [line.strip() for line in notes.splitlines()]
    title = re.sub(r"\s{2,}\S+$", "", lines[0])   # `ECONOMY OW UNBUNDLED FARES      N`
    if "FARES" not in title and len(lines) > 1 and re.fullmatch(r"(?!APPLIES|FOR )[A-Z /-]+ FARES", lines[1]):
        title += " " + lines[1]   # a long title wraps: `BASIC SEASON MIDWEEK ECONOMY RT   K` / `UNBUNDLED FARES`
    bits = []
    kind = title.replace("BASIC SEASON", "")   # a season's name, on basic and standard fares alike
    if "UNBUNDLED" in kind or "BASIC" in kind:
        bits.append(f"unbundled (basic) fare: {title.lower()}")
    elif "SELL-UP" in title or "SELLUP" in title:
        bits.append(f"sell-up fare: {title.lower()}")
    pairs = []
    for line in notes.splitlines():
        m = _CHART_ROW.search(line)
        if m:
            classes = m[2].split()
            pairs.append(f"{m[1]} {classes[-1]} also needs {' and '.join(classes[:-1])} open")
    if pairs:
        bits.append("dual booking class: " + ", ".join(dict.fromkeys(pairs))
                    + " (the fare sells only while both have seats)")
    return " · ".join(bits)
