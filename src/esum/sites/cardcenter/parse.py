"""CardCenter's JSON into rows: buy orders (the rates page) and one deal. Pure functions.

Rates are fractions of face value in the API (0.835); rows show them as the site does (83.5%).
Nothing here keeps a user, an e-mail address or a permission block: the API sends the logged-in
account in every deal.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from esum import one_line

EASTERN = ZoneInfo("America/New_York")   # the site shows every time in ET


def money(value: float | int | None) -> str:
    """`$2,140` or `$79.99`; empty for a missing amount."""
    if value is None:
        return ""
    return f"${value:,.2f}".removesuffix(".00")


def percent(fraction: float | None) -> str:
    """`83.5%` from 0.835; empty for a missing rate."""
    if fraction is None:
        return ""
    return f"{round(fraction * 100, 2):g}%"


STAMP = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?)(?:\.(\d+))?(Z|[+-]\d\d:?\d\d)?")


def moment(stamp: str | None) -> datetime | None:
    """The API's time as an aware datetime: UTC with `Z`, any length of fraction, or a numeric
    offset; None for a missing or unreadable one."""
    found = STAMP.fullmatch((stamp or "").strip())
    if not found:
        return None
    head, fraction, zone = found.groups()
    zone = "+00:00" if zone in (None, "Z") else zone if ":" in zone else f"{zone[:3]}:{zone[3:]}"
    return datetime.fromisoformat(f"{head}{'.' + fraction[:6] if fraction else ''}{zone}")


def eastern(stamp: str | None) -> str:
    """`2026-12-01 00:59 ET` from the API's time; empty for none."""
    when = moment(stamp)
    return when.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M ET") if when else ""


def _stamp(stamp: str | None) -> float | None:
    """Seconds since the epoch, to sort by: the shown ET clock repeats an hour when DST ends."""
    when = moment(stamp)
    return when.timestamp() if when else None


def card_value(order: dict) -> str:
    """The face value an order takes: `$50`, or `$450-500 by $5` for a range in steps."""
    high = order.get("value")
    low = order.get("lowValue")
    text = f"{money(low)}-{money(high).lstrip('$')}" if low is not None and low != high else money(high)
    step = order.get("divisibleByValue")
    return f"{text} by {money(step)}" if step else text


def _per_seller(order: dict) -> str:
    parts = []
    if order.get("unfulfilledPerSellerCap") is not None:
        parts.append(f"{money(order['unfulfilledPerSellerCap'])} unfulfilled")
    if order.get("perSellerCap") is not None:
        parts.append(f"{money(order['perSellerCap'])} total")
    return ", ".join(parts)


def _deadline(order: dict) -> str:
    """When the cards must be submitted: `by <time>` for a fixed deadline, else `within N days of approval`."""
    if order.get("submissionDeadline"):
        return f"by {eastern(order['submissionDeadline'])}"
    days = order.get("maximumSubmissionTerms")
    return f"within {days} day{'s' if days != 1 else ''} of approval" if days is not None else ""


def takes(row: dict, value: float) -> bool:
    """Whether a rate row takes a card of this face value: its own value, or inside its range and
    on its step."""
    low, high, step = row["_low"], row["_high"], row["_step"]
    if high is None or not low <= value <= high:
        return False
    return not step or abs(value / step - round(value / step)) < 1e-9


def rate_row(order: dict, deal: dict | None = None) -> dict[str, Any]:
    """One buy order: what CardCenter pays for one card value of one brand under one deal."""
    deal = deal or order.get("deal") or {}
    brand = order.get("brand") or {}
    margin = order.get("margin")
    pay, cost = order.get("rate"), order.get("purchaseRate")
    profit = pay - cost if pay is not None and cost is not None else None
    high = order.get("value")
    return {
        "deal": deal.get("id"),
        "brand": one_line(brand.get("name") or ""),
        "value": card_value(order),
        "cost": percent(cost),                           # what the deal makes the card cost you
        "pay": percent(pay),                             # what CardCenter pays
        "profit": percent(profit),                       # pay minus cost, in points of face value
        "margin": percent(margin),                       # the site's own figure: relative to pay, extras counted
        "cap": money(order.get("availableCap")),
        "title": one_line(deal.get("title") or ""),
        "vendor": one_line((deal.get("vendor") or {}).get("name") or ""),
        "terms": f"{order['paymentTerms']} days" if order.get("paymentTerms") is not None else "",
        "per_seller": _per_seller(order),
        "deadline": _deadline(order),
        "until": eastern(order.get("autoApproveOffersUntil")),   # CardCenter buys this line until then
        "slug": brand.get("slug") or "",
        "_pay": pay or 0.0,
        "_profit": profit if profit is not None else float("-inf"),
        "_margin": margin if margin is not None else float("-inf"),
        "_high": high,
        "_low": order.get("lowValue") if order.get("lowValue") is not None else high,
        "_step": order.get("divisibleByValue"),
        "_until": _stamp(order.get("autoApproveOffersUntil")),
    }


def rate_rows(data: dict) -> list[dict[str, Any]]:
    """Every buy order on the rates page (`/Api/BuyOrders/v2`), in the site's order (by brand)."""
    return [rate_row(o) for o in data.get("items") or []]


def last_until(lines: list[tuple[float | None, str]]) -> tuple[float | None, str]:
    """When the last of some lines stops being bought, as (sort key, text). A line with no closing
    time is bought with no end, so then the whole has none either: (None, "")."""
    if not lines or any(stamp is None for stamp, _ in lines):
        return None, ""
    return max(lines)


def deal_rows(rates: list[dict]) -> list[dict[str, Any]]:
    """One row per deal behind the rates: its brands and the best pay, profit and margin among its lines."""
    deals: dict[Any, dict] = {}
    for r in rates:
        d = deals.setdefault(r["deal"], {"id": r["deal"], "vendor": r["vendor"], "title": r["title"],
                                         "_brands": [], "lines": 0, "_untils": [], "_pay": 0.0,
                                         "_profit": float("-inf"), "_margin": float("-inf")})
        d["lines"] += 1
        if r["brand"] not in d["_brands"]:
            d["_brands"].append(r["brand"])
        d["_pay"] = max(d["_pay"], r["_pay"])
        d["_margin"] = max(d["_margin"], r["_margin"])
        d["_profit"] = max(d["_profit"], r["_profit"])
        d["_untils"].append((r["_until"], r["until"]))
    for d in deals.values():
        d["_until"], d["until"] = last_until(d.pop("_untils"))
        d["brands"] = ", ".join(d["_brands"])
        d["pay"] = percent(d["_pay"]) if d["_pay"] else ""       # a deal whose lines give no rate
        d["profit"] = percent(d["_profit"]) if d["_profit"] != float("-inf") else ""
        d["margin"] = percent(d["_margin"]) if d["_margin"] != float("-inf") else ""
    return list(deals.values())


def deal_detail(data: dict) -> dict[str, Any]:
    """One deal (`/Api/Deals/<id>`): where and when it runs, its notes, and each rate CardCenter
    pays under it. The account blocks (`users[].user`, e-mails, permissions) are dropped."""
    places = [name for flag, name in (("availableOnline", "online"), ("availableInStore", "in store"))
              if data.get(flag)]
    category: dict = {}
    rates: list[dict] = []
    for party in data.get("users") or []:
        if party.get("role") == "Myself":
            continue
        category = category or party.get("category") or {}
        rates += [rate_row(o, data) for o in party.get("rates") or []]
    brands = list(dict.fromkeys(r["brand"] for r in rates if r["brand"]))
    return {
        "id": data.get("id"),
        "title": one_line(data.get("title") or ""),
        "vendor": one_line((data.get("vendor") or {}).get("name") or ""),
        "brand": one_line((data.get("brand") or {}).get("name") or "") or ", ".join(brands),
        "where": " and ".join(places),
        "always": bool(data.get("alwaysAvailable")),
        "url": data.get("url") or "",
        "price": money(data.get("currentPrice")),
        "list_price": money(data.get("listPrice")),
        "starts": eastern(data.get("startDate")),
        # a standing deal has no end of its own: its rates say until when CardCenter buys
        "ends": eastern(data.get("endDate")) or last_until([(r["_until"], r["until"]) for r in rates])[1],
        "confirmed": eastern(data.get("lastConfirmedAt")),
        "notes": (data.get("notes") or "").strip(),
        "category": one_line(category.get("name") or ""),
        "category_note": one_line(category.get("description") or ""),
        "rates": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rates],
    }
