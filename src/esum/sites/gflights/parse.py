"""Google Flights: the `tfs` search token and the search page's `ds:1` payload, as pure functions.

A search is addressed by `tfs`, a base64url protobuf naming the trip (segments, dates, airports,
pinned flights, stop limit, cabin, passengers). The page answers with the result rows inline in an
`AF_initDataCallback({key: 'ds:1', ...})` blob. Positions below were read from live pages.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import date as _date, datetime, timedelta, timezone
from typing import Any

PAGE = "https://www.google.com/travel/flights"
CABINS = {"economy": 1, "premium": 2, "business": 3, "first": 4}
CABIN_NAMES = {v: k for k, v in CABINS.items()}

_BLOB = re.compile(r"AF_initDataCallback\((\{.*?\})\);", re.S)
_KEY = re.compile(r"key:\s*'([^']+)'")
_DATA = re.compile(r"data:(.*?),\s*(?:sideChannel|errorHasStatus)", re.S)
_MAX_U64 = (1 << 64) - 1


# ---- the tfs token ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Leg:
    """One flight pinned in a segment: how a round trip asks for returns to a chosen outbound."""
    origin: str
    date: str
    dest: str
    airline: str
    number: str


# A flight named in full, as `fares --flights` takes it and as a row from behind "View more
# flights" is named: SFO-NRT:2027-03-10:UA837 (an airline code is two characters, one a letter).
# An itinerary is its flights joined by `+`.
# A flight number is up to four digits and, now and then, a letter after them (UA837A).
_FLIGHT = re.compile(r"([A-Z]{3})-([A-Z]{3}):(\d{4}-\d{2}-\d{2}):([A-Z][A-Z0-9]|[0-9][A-Z])0*(\d{1,4}[A-Z]?)")


def named_flight(text: str) -> Leg | None:
    """The flight `ORIGIN-DEST:YYYY-MM-DD:FLIGHT` names, or None when the text is not that."""
    m = _FLIGHT.fullmatch(text)
    return Leg(m[1], m[3], m[2], m[4], m[5]) if m else None


def named_flights(text: str) -> list[Leg] | None:
    """The flights an itinerary's full name gives, or None when it is not one (a first-page row's
    id is its flight numbers alone: UA837, LH2473+LH458)."""
    legs = [named_flight(part) for part in re.sub(r"\s+", "", text).upper().split("+")]
    return legs if all(legs) else None


def flights_name(legs) -> str:
    """An itinerary's full name: each flight's airports, departure date and number (025 is 25, as
    `named_flight` reads it, so one itinerary has one name whichever page it came from)."""
    return "+".join(f"{leg.origin}-{leg.dest}:{leg.date}:{leg.airline}{leg.number.lstrip('0') or '0'}"
                    for leg in legs)


@dataclass(frozen=True)
class Segment:
    origins: tuple[str, ...]
    dests: tuple[str, ...]
    date: str
    legs: tuple[Leg, ...] = ()
    max_stops: int | None = None


@dataclass(frozen=True)
class Query:
    segments: tuple[Segment, ...]
    adults: int = 1
    cabin: int = 1
    currency: str | None = None
    one_way: bool = field(default=True)

    @property
    def round_trip(self) -> bool:
        return not self.one_way

    def pinned(self) -> bool:
        return any(s.legs for s in self.segments)


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _ld(f: int, payload: bytes) -> bytes:
    return _varint(f << 3 | 2) + _varint(len(payload)) + payload


def _vf(f: int, n: int) -> bytes:
    return _varint(f << 3) + _varint(n)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _airport(code: str) -> bytes:
    return _vf(1, 1) + _ld(2, code.encode())


def _leg(leg: Leg) -> bytes:
    return (_ld(1, leg.origin.encode()) + _ld(2, leg.date.encode()) + _ld(3, leg.dest.encode())
            + _ld(5, leg.airline.encode()) + _ld(6, leg.number.encode()))


def _segment(s: Segment) -> bytes:
    body = _ld(2, s.date.encode())
    if s.max_stops is not None:
        body += _vf(5, s.max_stops)
    for leg in s.legs:
        body += _ld(4, _leg(leg))
    for code in s.origins:
        body += _ld(13, _airport(code))
    for code in s.dests:
        body += _ld(14, _airport(code))
    return _ld(3, body)


def tfs(q: Query, *, booking: bool = False) -> str:
    """The `tfs` value for a search page, or (`booking`) for the booking page of pinned flights."""
    body = _vf(1, 28) + _vf(2, 2) + b"".join(_segment(s) for s in q.segments)
    body += b"".join(_vf(8, 1) for _ in range(q.adults))
    body += _vf(9, q.cabin) + _vf(14, 1)
    if booking:
        body += _ld(16, _vf(1, _MAX_U64))
    body += _vf(19, 2 if q.one_way else 1)
    return _b64(body)


def handle(q: Query) -> str:
    """The `search` value printed to the agent: the tfs token, `.CUR` for a chosen currency, and a
    check.

    The check makes a value with a character changed in copying fail as a whole: without it, most
    changes still decode, to another airport or date, and the call answers for a different search.
    """
    body = tfs(q) + (f".{q.currency}" if q.currency else "")
    return f"{body}.{_check(body)}"


def _check(body: str) -> str:
    return _b64(hashlib.blake2s(body.encode(), digest_size=3).digest())


def page_url(q: Query) -> str:
    return f"{PAGE}?tfs={tfs(q)}&hl=en&gl=US" + (f"&curr={q.currency}" if q.currency else "")


def booking_url(q: Query) -> str:
    return f"{PAGE}/booking?tfs={tfs(q, booking=True)}&hl=en&gl=US" + (f"&curr={q.currency}" if q.currency else "")


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7


def _fields(buf: bytes) -> list[tuple[int, int | bytes]]:
    out: list[tuple[int, int | bytes]] = []
    i = 0
    while i < len(buf):
        tag, i = _read_varint(buf, i)
        f, wire = tag >> 3, tag & 7
        if wire == 0:
            v, i = _read_varint(buf, i)
            out.append((f, v))
        elif wire == 2:
            n, i = _read_varint(buf, i)
            if i + n > len(buf):
                raise ValueError("truncated field")
            out.append((f, buf[i:i + n]))
            i += n
        else:
            raise ValueError(f"wire type {wire}")
    return out


def _decode(token: str) -> tuple[list[Segment], int, int, int | None]:
    """The fields of a tfs token; raises ValueError or IndexError on any malformed part."""
    top = _fields(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    segments, adults, cabin, kind = [], 0, 1, None
    for f, v in top:
        if f == 3 and isinstance(v, bytes):
            date, legs, stops, origins, dests = "", [], None, [], []
            for sf, sv in _fields(v):
                if sf == 2 and isinstance(sv, bytes):
                    date = sv.decode()
                elif sf == 5 and isinstance(sv, int):
                    stops = sv
                elif sf == 4 and isinstance(sv, bytes):
                    parts = {lf: lv.decode() for lf, lv in _fields(sv) if isinstance(lv, bytes)}
                    legs.append(Leg(parts.get(1, ""), parts.get(2, ""), parts.get(3, ""), parts.get(5, ""),
                                    parts.get(6, "")))
                elif sf in (13, 14) and isinstance(sv, bytes):
                    code = next((av.decode() for af, av in _fields(sv) if af == 2 and isinstance(av, bytes)), "")
                    (origins if sf == 13 else dests).append(code)
            segments.append(Segment(tuple(origins), tuple(dests), date, tuple(legs), stops))
        elif f == 8:
            adults += 1
        elif f == 9 and isinstance(v, int):
            cabin = v
        elif f == 19 and isinstance(v, int):
            kind = v
    return segments, adults, cabin, kind


def parse_handle(value: str) -> Query:
    """Read a `search` value back into a Query. Raises ValueError when it is not one ESUM printed."""
    body, _, check = value.strip().rpartition(".")
    if not body or check != _check(body):
        raise ValueError("check does not match")
    token, _, currency = body.partition(".")
    if currency and not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError(f"currency suffix {currency!r}")
    try:
        segments, adults, cabin, kind = _decode(token)
    except (ValueError, IndexError) as e:   # UnicodeDecodeError is a ValueError
        raise ValueError(str(e)) from None
    if not segments or kind not in (1, 2) or cabin not in CABIN_NAMES \
            or not all(s.origins and s.dests and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s.date) for s in segments):
        raise ValueError("not a Google Flights search token")
    return Query(tuple(segments), max(adults, 1), cabin, currency or None, one_way=kind == 2)


def pin(q: Query, index: int, legs: list[Leg]) -> Query:
    segs = list(q.segments)
    segs[index] = replace(segs[index], legs=tuple(legs))
    return replace(q, segments=tuple(segs))


# ---- the page payload ------------------------------------------------------------------------

def payload(html: str) -> Any | None:
    """The `ds:1` data of a search page, or None when the page has none (a consent or block page)."""
    for m in _BLOB.finditer(html):
        blob = m.group(1)
        key = _KEY.search(blob)
        if not key or key.group(1) != "ds:1":
            continue
        data = _DATA.search(blob)
        if not data:
            return None
        try:
            return json.loads(data.group(1))
        except ValueError:
            return None
    return None


def page_error(data: Any) -> int | None:
    """Google's ErrorResponse status code (2: date in the past, 3: date too far ahead), else None."""
    try:
        if isinstance(data, list) and data and data[0] == 3 and "ErrorResponse" in data[2][0][0]:
            return int(data[2][0][1][1])
    except (IndexError, TypeError, ValueError):
        return -1
    return None


def _get(seq: Any, *path: int) -> Any:
    for i in path:
        if not isinstance(seq, list) or i >= len(seq):
            return None
        seq = seq[i]
    return seq


def hm(minutes: int | None) -> str:
    if not minutes:
        return ""
    h, m = divmod(int(minutes), 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _clock(t: Any) -> str:
    t = t if isinstance(t, list) else []
    return f"{(_get(t, 0) or 0):02d}:{(_get(t, 1) or 0):02d}"


def _day(d: Any) -> str:
    return f"{d[0]:04d}-{d[1]:02d}-{d[2]:02d}" if isinstance(d, list) and len(d) == 3 and all(d) else ""


_POWER = {1: "power and USB", 3: "power", 5: "USB"}
_VIDEO = {8: "live TV", 9: "on-demand video", 10: "stream to device"}
_WIFI = {2: "free Wi-Fi", 3: "paid Wi-Fi"}
_SEAT = {1: "average legroom", 2: "below average legroom", 3: "above average legroom",
         4: "extra reclining seat", 5: "lie-flat seat", 6: "lie-flat suite with door", 8: "recliner"}


def _on(slots: Any, i: int) -> bool:
    v = _get(slots, i)
    return v is True or (isinstance(v, int) and not isinstance(v, bool) and v > 0)


def amenities(leg: list) -> list[str]:
    slots = _get(leg, 12)
    out = []
    if (power := next((_POWER[i] for i in range(1, 7) if _on(slots, i) and i in _POWER), None)):
        out.append(power)
    if (video := next((_VIDEO[i] for i in _VIDEO if _on(slots, i)), None)):
        out.append(video)
    if (wifi := _WIFI.get(_get(slots, 11))):
        out.append(wifi)
    return out


def _operator(text: Any) -> str | None:
    """Google's operator line, without the "Operated by" and dangling "as" it sometimes keeps."""
    if not isinstance(text, str):
        return None
    text = re.sub(r"\s+as$", "", re.sub(r"^operated by\s+", "", text.strip(), flags=re.I))
    return text or None


def leg_row(leg: list) -> dict:
    flight = _get(leg, 22) or []
    codeshares = [f"{c[0]}{c[1]}" for c in (_get(leg, 15) or []) if isinstance(c, list) and len(c) > 1]
    return {
        "flight": f"{_get(flight, 0) or ''}{_get(flight, 1) or ''}",
        "carrier": _get(flight, 0) or "", "number": str(_get(flight, 1) or ""),
        "airline": _get(flight, 3) or _get(flight, 0),
        "operated_by": _operator(_get(leg, 2)),
        "from": _get(leg, 3), "from_name": _get(leg, 4),
        "to": _get(leg, 6), "to_name": _get(leg, 5),
        "depart": f"{_day(_get(leg, 20))} {_clock(_get(leg, 8))}",
        "arrive": f"{_day(_get(leg, 21))} {_clock(_get(leg, 10))}",
        "duration": hm(_get(leg, 11)),
        "aircraft": _get(leg, 17),
        "legroom": _get(leg, 30) or _get(leg, 14),
        "cabin": CABIN_NAMES.get(_get(leg, 16)),
        "seat": _SEAT.get(_get(leg, 13)),
        "amenities": amenities(leg),
        "also_sold_as": codeshares,
        "co2_kg": round(_get(leg, 31) / 1000) if isinstance(_get(leg, 31), int) else None,
    }


def _currency(token: Any) -> str | None:
    """The ISO currency inside a row's price token (protobuf field 3 → field 3)."""
    if not isinstance(token, str) or not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.replace("+", "-").replace("/", "_") + "=" * (-len(token) % 4))
        for f, v in _fields(raw):
            if f == 3 and isinstance(v, bytes):
                for nf, nv in _fields(v):
                    if nf == 3 and isinstance(nv, bytes):
                        return nv.decode().upper()
    except (ValueError, IndexError, UnicodeDecodeError):
        return None
    return None


def itinerary_row(detail: list, price_block: Any = None) -> dict:
    """A result row from a row's detail list (`row[0]`) and price block (`row[1]`)."""
    legs = [leg_row(leg) for leg in (_get(detail, 2) or [])]
    if not legs:
        raise ValueError("row without flights")
    head = _get(price_block, 0)
    price = head[-1] if isinstance(head, list) and head and isinstance(head[-1], (int, float)) else None
    stops_at = [(legs[i]["to"], legs[i + 1]["from"]) for i in range(len(legs) - 1)]
    layovers = []
    for i, lay in enumerate(_get(detail, 13) or []):
        if isinstance(lay, list) and i < len(stops_at):
            land, leave = stops_at[i]
            text = f"{hm(_get(lay, 0))} {land}"
            if land != leave:
                text += f"→{leave} (change airports)"
            if i + 1 < len(legs) and legs[i]["arrive"][:10] != legs[i + 1]["depart"][:10]:
                text += " overnight"
            layovers.append(text)
    emissions = _get(detail, 22)
    co2, typical = _get(emissions, 7), _get(emissions, 8)
    stops = len(legs) - 1
    via = " ".join(land if land == leave else f"{land}/{leave}" for land, leave in stops_at)
    first, last = legs[0], legs[-1]
    flights = " ".join(leg["flight"] for leg in legs)
    text = (f"{first['from']} {first['depart'][5:]} → {last['to']} {last['arrive'][5:]} · {hm(_get(detail, 9))} · "
            + (f"via {via}" if via else "nonstop") + f" · {flights}")
    carriers = sorted({leg["carrier"] for leg in legs})
    warnings = []
    if _get(detail, 12):
        warnings.append("separate tickets (self-transfer)")
    if any(land != leave for land, leave in stops_at):
        warnings.append("change of airport")
    if any(leg["operated_by"] for leg in legs):
        warnings += [f"{leg['flight']} operated by {leg['operated_by']}" for leg in legs if leg["operated_by"]]
    return {
        "id": "+".join(leg["flight"] for leg in legs),
        "price": price,
        "currency": _currency(_get(price_block, 1)),
        "itinerary": text,
        "carriers": " ".join(carriers),
        "aircraft": " | ".join(leg["aircraft"] or "?" for leg in legs),
        "seats": " | ".join(leg["seat"] or "?" for leg in legs),
        "cabins": " | ".join(leg["cabin"] or "?" for leg in legs),
        "amenities": " | ".join(", ".join(leg["amenities"]) or "none listed" for leg in legs),
        "stops": stops,
        "duration": hm(_get(detail, 9)),
        "minutes": _get(detail, 9),
        "depart": first["depart"], "arrive": last["arrive"],
        "layovers": layovers,
        "co2_kg": round(co2 / 1000) if isinstance(co2, int) else None,
        "co2_typical_kg": round(typical / 1000) if isinstance(typical, int) else None,
        "warnings": warnings,
        "legs": legs,
    }


def flights(data: Any) -> list[dict]:
    """Every result row on the page, Google's "best" block first, duplicates dropped."""
    rows, seen = [], set()
    for block in (2, 3):
        for raw in _get(data, block, 0) or []:
            try:
                row = itinerary_row(raw[0], _get(raw, 1))
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            if row["id"] not in seen:
                seen.add(row["id"])
                row["best"] = block == 2
                rows.append(row)
    return rows


def has_more(data: Any) -> bool:
    """Whether the page keeps more rows behind "View more flights" (a count beside the other-flights block:
    0 on the pages without the button, 1-10 on those with it)."""
    return bool(_get(data, 3, 1))


def selected(data: Any) -> dict | None:
    """The outbound a pinned (returns) page was asked for."""
    detail = _get(data, 1, 0, 2)
    try:
        return itinerary_row(detail) if isinstance(detail, list) else None
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def money(price: float | int | None, currency: str | None) -> str:
    if price is None:
        return "unpriced"
    return f"{currency or ''}{price:,.0f}" if float(price).is_integer() else f"{currency or ''}{price:,.2f}"


# Google's verdict code, checked against the page's "Prices are currently …" line: 1 low, 2-3 typical,
# 5 high (4 not seen; between typical and high, so typical).
LEVELS = {1: "low", 2: "typical", 3: "typical", 4: "typical", 5: "high"}


def insight(data: Any) -> dict | None:
    """Google's price insight: today's lowest price against what the trip usually costs, and its history."""
    block = _get(data, 5)
    if not isinstance(block, list):
        return None
    low, usual = _get(block, 1, 1), _get(block, 2, 1)
    if not isinstance(low, (int, float)):
        return None
    history = []
    for point in _get(block, 10, 0) or []:
        if isinstance(point, list) and len(point) == 2 and isinstance(point[1], (int, float)):
            day = datetime.fromtimestamp(point[0] / 1000, tz=timezone.utc).date().isoformat()
            history.append((day, point[1]))
    return {"level": LEVELS.get(_get(block, 0)), "lowest": low, "usual": usual,
            "typical_low": _get(block, 4, 1), "typical_high": _get(block, 5, 1),
            "history": [{"date": d, "price": p} for d, p in history]}


def insight_text(info: dict | None, currency: str | None) -> str | None:
    if not info:
        return None
    text = f"{info['level'] or 'unrated'}: lowest {money(info['lowest'], currency)}"
    if isinstance(info["usual"], (int, float)) and info["usual"] != info["lowest"]:
        diff = info["lowest"] - info["usual"]
        text += f" ({money(abs(diff), currency)} {'under' if diff < 0 else 'over'} the usual "                 f"{money(info['usual'], currency)})"
    if info["typical_low"] is not None and info["typical_high"] is not None:
        text += f"; typical range {money(info['typical_low'], currency)}–{money(info['typical_high'], currency)}"
    return text


def history_text(info: dict | None, currency: str | None) -> str | None:
    points = [(p["date"], p["price"]) for p in (info or {}).get("history") or []]
    if len(points) < 2:
        return None
    lo = min(points, key=lambda p: p[1])
    hi = max(points, key=lambda p: p[1])
    return (f"{points[0][0]} {money(points[0][1], currency)} → {points[-1][0]} {money(points[-1][1], currency)}; "
            f"low {money(lo[1], currency)} on {lo[0]}, high {money(hi[1], currency)} on {hi[0]}")


# --- the booking page's options, from its text ---------------------------------------------------

# a price on its own line: $1,859, €1.234, CA$1,859, CHF 1,859
_PRICE_LINE = re.compile(r"^(?:[A-Z]{1,3})?\s?[^\w\s]?\s?\d[\d,.]*$")
_OPTIONS_END = re.compile(r"^(\d+ more booking options?|Prices include .*|Price insights)$")


def booking_options(text: str) -> list[dict]:
    """The booking page's options, in the page's order: `{seller, airline, brand, price, terms}`.

    A seller whose options the page shows open (the airline, as a rule) gives one row per fare
    brand, with the brand's terms as the page words them; the others give their one price, and
    `terms` holds the page's remark on it (separate tickets)."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    rows: list[dict] = []
    seller, airline, opened, pending, row = "", False, False, [], None
    for line in lines:
        if line.startswith("Book with "):
            name = line.removeprefix("Book with ")
            airline = name.endswith("Airline")      # the page's badge, run into the name
            seller, opened, pending, row = name.removesuffix("Airline").strip(), False, [], None
        elif not seller:
            continue
        elif _OPTIONS_END.match(line):
            break
        elif line == "Hide options":
            opened = True
        elif line in ("Continue", "Continue anyway"):   # "anyway": a brand below the cabin searched
            pending, row = [], None
        elif line == "View options":
            continue
        elif _PRICE_LINE.match(line) and row is None:
            row = {"seller": seller, "airline": airline, "brand": pending.pop() if opened and pending else "",
                   "price": line, "terms": list(pending)}
            rows.append(row)
        elif row is not None:
            row["terms"].append(line)
        else:       # the page runs a remark's heading into its text
            pending.append(re.sub(r"^Separate tickets(?=\S)", "Separate tickets: ", line))
    return rows


# --- the rows behind "View more flights", from what more.js returned -------------------------------

_MONTHS = {name: i for i, name in enumerate(("January", "February", "March", "April", "May", "June", "July", "August",
                                             "September", "October", "November", "December"), 1)}
_LEG = re.compile(r"^([A-Z]{3})-([A-Z]{3})-([A-Z0-9]{2})-(\d+[A-Z]?)-(\d{4})(\d{2})(\d{2})$")
_CLOCK = r"(\d{1,2}):(\d{2})\s?([AP]M)"
_WHEN = rf"{_CLOCK} on \w+, (\w+) (\d{{1,2}})"
_TIMES = re.compile(rf"Leaves .* at {_WHEN} and arrives at .* at {_WHEN}\.")
_TOTAL = re.compile(r"Total duration ([^.]*)\.(.*?)(?=Layover \(|Select flight|$)", re.S)
_LAYOVER = re.compile(r"Layover \(\d+ of \d+\) is an? (.*?)layover (?:at|in) ")     # "in Tokyo": it changes airports
_OPERATED = re.compile(r"\bOperated by ([^.]+)\.")
# the row's own text, a second source: "8:33 AM\n – \n4:40 PM+1", "15 hr 7 min", "2 hr 40 min YVR"
#   (the page's other layout: "11:05 AM\nSFO\n6:55 AM+2\nSIN" and "1 stop27 hr 50 min")
_TEXT_TIMES = re.compile(rf"{_CLOCK}\D{{0,40}}?{_CLOCK}\s*([+-]\d)?")
_SPAN = r"(?:\d+ hr(?: \d+ min)?|\d+ min)"
_TEXT_TOTAL = re.compile(rf"({_SPAN})")       # the first span in the text is the whole trip's
_TEXT_WAIT = re.compile(rf"^\s*({_SPAN}(?: overnight)?)\s+([A-Z]{{3}})\s*$", re.M)


def _minutes(text: str) -> int | None:
    hours, mins = re.search(r"(\d+) hr", text), re.search(r"(\d+) min", text)
    return (int(hours.group(1)) * 60 if hours else 0) + (int(mins.group(1)) if mins else 0) or None


def _clock24(hour: str, minute: str, half: str) -> str:
    return f"{int(hour) % 12 + (12 if half == 'PM' else 0):02d}:{minute}"


def _token_price(token: Any) -> tuple[int | None, str | None]:
    """The price and ISO currency inside a row's price token (protobuf field 3: amount, decimal
    places, currency). The page shows the amount rounded up to a whole unit, so this does too."""
    if not isinstance(token, str) or not token:
        return None, None
    try:
        raw = base64.urlsafe_b64decode(token.replace("+", "-").replace("/", "_") + "=" * (-len(token) % 4))
        for f, v in _fields(raw):
            if f == 3 and isinstance(v, bytes):
                inner = dict(_fields(v))
                amount, places, code = inner.get(1), inner.get(2, 0), inner.get(3)
                price = -(-amount // 10 ** places) if isinstance(amount, int) and isinstance(places, int) else None
                return price, code.decode().upper() if isinstance(code, bytes) else None
    except (ValueError, IndexError, UnicodeDecodeError):
        pass
    return None, None


def _label_price(text: Any) -> int | None:
    """`1175 US dollars` or `From 1175 US dollars round trip total` → 1175."""
    m = re.search(r"\d[\d,]*", text) if isinstance(text, str) else None
    return int(m.group(0).replace(",", "")) if m else None


def _sentence(item: dict) -> str:
    """The row's description: of the labels more.js found in the row, the one that tells the times
    (the longest of them), or the longest label when none does."""
    if isinstance(item.get("label"), str):
        return item["label"]
    labels = [t for t in item.get("labels") or [] if isinstance(t, str)]
    return max((t for t in labels if _TIMES.search(t)), key=len, default="") or max(labels, key=len, default="")


def _arrival_day(start: _date, month: int, day: int) -> _date | None:
    """A month and day on or after `start` (a day before it at most: an eastbound date-line crossing)."""
    for year in (start.year - 1, start.year, start.year + 1):
        try:
            found = _date(year, month, day)
        except ValueError:
            continue
        if found >= start - timedelta(days=2):
            return found
    return None


def more_row(item: dict) -> dict:
    """A result row from one row more.js returned. Its flights (airports, flight numbers and dates)
    come from its emissions link and name it: without them there is no row (`ValueError`). The
    times, duration and layovers come from the sentence Google's page describes the row with, else
    from the row's own text; the price from its label, else from its price token. What neither
    source gives stays unknown and is named in `missing`. A first-page row's keys, plus `more` and
    `missing`; aircraft, seats and each flight's times are unknown until the itinerary's own page
    is read."""
    flown = str(item.get("flights") or "").partition("itinerary=")[2]
    legs = []
    for part in flown.split(","):
        m = _LEG.match(part)
        if not m:
            raise ValueError(f"row without flights: {flown!r}")
        origin, dest, carrier, number, y, mo, d = m.groups()
        number = number.lstrip("0") or "0"
        legs.append({"flight": f"{carrier}{number}", "carrier": carrier, "number": number, "airline": carrier,
                     "operated_by": None, "from": origin, "from_name": None, "to": dest, "to_name": None,
                     "depart": f"{y}-{mo}-{d}", "arrive": "", "duration": "", "aircraft": None, "legroom": None,
                     "cabin": None, "seat": None, "amenities": [], "also_sold_as": [], "co2_kg": None})
    label, text = _sentence(item), str(item.get("text") or "")
    missing: list[str] = []
    start = legs[0]["depart"]
    first = _date.fromisoformat(start)
    depart, arrive = start, ""       # the date alone until a time is read
    if (times := _TIMES.search(label)) and times.group(4) in _MONTHS and times.group(9) in _MONTHS:
        h1, m1, half1, _, _, h2, m2, half2, month2, day2 = times.groups()
        landed = _arrival_day(first, _MONTHS[month2], int(day2))
        if landed:
            depart, arrive = f"{start} {_clock24(h1, m1, half1)}", f"{landed.isoformat()} {_clock24(h2, m2, half2)}"
    if not arrive and (times := _TEXT_TIMES.search(text)):
        h1, m1, half1, h2, m2, half2, offset = times.groups()
        landed = first + timedelta(days=int(offset or 0))
        depart, arrive = f"{start} {_clock24(h1, m1, half1)}", f"{landed.isoformat()} {_clock24(h2, m2, half2)}"
    if not arrive:
        missing.append("times")
    legs[0]["depart"], legs[-1]["arrive"] = depart, arrive
    total = _TOTAL.search(label)
    minutes = _minutes(total.group(1)) if total else None
    if minutes is None and (span := _TEXT_TOTAL.search(text)):
        minutes = _minutes(span.group(1))
    if minutes is None:
        missing.append("duration")
    stops_at = [(legs[i]["to"], legs[i + 1]["from"]) for i in range(len(legs) - 1)]
    said, written = _LAYOVER.findall(label), {airport: wait for wait, airport in _TEXT_WAIT.findall(text)}
    layovers = []
    for i, (land, leave) in enumerate(stops_at):
        wait = said[i] if i < len(said) else written.get(land, "")
        if not _minutes(wait) and "layovers" not in missing:
            missing.append("layovers")
        entry = f"{hm(_minutes(wait))} {land}".strip()
        if land != leave:
            entry += f"→{leave} (change airports)"
        if "overnight" in wait:
            entry += " overnight"
        layovers.append(entry)
    via = " ".join(land if land == leave else f"{land}/{leave}" for land, leave in stops_at)
    numbers = " ".join(leg["flight"] for leg in legs)
    warnings = []
    if any(land != leave for land, leave in stops_at):
        warnings.append("change of airport")
    if (operator := _OPERATED.search(label.partition(" Leaves ")[0])):
        warnings.append(f"a flight operated by {_operator(operator.group(1))}")
    if total and (remark := " ".join(total.group(2).split())):     # the page's own remark on the row
        warnings.append(remark)
    paid, currency = _token_price(item.get("token"))
    price = _label_price(item.get("price"))
    if price is None:
        price = paid
    if price is None:
        missing.append("price")
    if currency is None:
        missing.append("currency")
    co2, typical = item.get("co2"), item.get("co2_typical")
    unknown = " | ".join("?" for _ in legs)
    when = (f"{legs[0]['from']} {depart[5:]} → {legs[-1]['to']} {arrive[5:]}" if arrive else
            f"{legs[0]['from']} {start[5:]} → {legs[-1]['to']} · time unknown")
    return {
        # named by its flights in full: nothing else is needed to read the itinerary again
        "id": flights_name(Leg(leg["from"], leg["depart"][:10], leg["to"], leg["carrier"], leg["number"])
                           for leg in legs),
        "price": price,
        "currency": currency,
        "itinerary": (f"{when} · {hm(minutes) or 'duration unknown'} · "
                      + (f"via {via}" if via else "nonstop") + f" · {numbers}"),
        "carriers": " ".join(sorted({leg["carrier"] for leg in legs})),
        "aircraft": unknown, "seats": unknown, "cabins": unknown, "amenities": unknown,
        "stops": len(legs) - 1,
        "duration": hm(minutes),
        "minutes": minutes,
        "depart": depart, "arrive": arrive,
        "layovers": layovers,
        "co2_kg": round(int(co2) / 1000) if str(co2 or "").isdigit() else None,
        "co2_typical_kg": round(int(typical) / 1000) if str(typical or "").isdigit() else None,
        "warnings": warnings,
        "legs": legs,
        "best": False,
        "more": True,
        "missing": missing,
    }


def more_rows(items: Any) -> tuple[list[dict], int]:
    """The rows more.js returned, in the page's order, each itinerary once; and how many of its
    rows could not be named (no flights to read), which are left out."""
    rows, seen, unreadable = [], set(), 0
    for item in items if isinstance(items, list) else []:
        try:
            row = more_row(item)
        except (TypeError, ValueError, KeyError, AttributeError):
            unreadable += 1
            continue
        if row["id"] not in seen:
            seen.add(row["id"])
            rows.append(row)
    return rows, unreadable
