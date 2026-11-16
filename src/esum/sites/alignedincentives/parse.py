"""Aligned Incentives' seller-portal deals page into rows. Pure functions.

The page carries every current deal as JSON in `<script id="sellerDealsInitialDataJson">`: the
deal, its notes, and one line item per brand and card value with what the card costs, what AI pays
and the profit, already in percent. The page also names the logged-in account; nothing here reads it.
"""

from __future__ import annotations

import html as htmllib
import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from esum import one_line

EASTERN = ZoneInfo("America/New_York")   # the portal shows every time in ET
DEALS_JSON = re.compile(r'<script[^>]*\bid="sellerDealsInitialDataJson"[^>]*>(.*?)</script>', re.S)


def money(value: float | int | None) -> str:
    if value is None:
        return ""
    return f"${value:,.2f}".removesuffix(".00")


def percent(value: float | None) -> str:
    """`94.53%`: the portal's rates are percentages already."""
    return "" if value is None else f"{round(value, 2):g}%"


STAMP = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?)(?:\.(\d+))?(Z|[+-]\d\d:?\d\d)?")


class PageChanged(RuntimeError):
    """The deals page is not laid out as this parser knows it. Never carries page content."""


def moment(stamp: str | None) -> datetime | None:
    """The page's time as an aware datetime: UTC with `Z`, any length of fraction (the portal sends
    seven digits), or a numeric offset; None for a missing or unreadable one."""
    found = STAMP.fullmatch((stamp or "").strip())
    if not found:
        return None
    head, fraction, zone = found.groups()
    zone = "+00:00" if zone in (None, "Z") else zone if ":" in zone else f"{zone[:3]}:{zone[3:]}"
    return datetime.fromisoformat(f"{head}{'.' + fraction[:6] if fraction else ''}{zone}")


def eastern(stamp: str | None) -> str:
    """`2026-11-19 00:59 ET` from the page's time; empty for none."""
    when = moment(stamp)
    return when.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M ET") if when else ""


def _stamp(stamp: str | None, missing: float) -> float:
    when = moment(stamp)
    return when.timestamp() if when else missing


def deals_json(page: str) -> list[dict]:
    """The deals the page embeds. PageChanged when the block is missing, is not JSON or is not a
    list: a logged-in page that lost its deals is a layout change, never "no deals" or a login."""
    found = DEALS_JSON.search(page)
    if not found:
        raise PageChanged("the deals page has no `sellerDealsInitialDataJson` block")
    raw = found.group(1)
    for text in (raw, htmllib.unescape(raw)):     # raw JSON first: a note may hold a literal `&quot;`
        try:
            data = json.loads(text)
            break
        except ValueError:
            continue
    else:
        raise PageChanged("the deals block on the deals page is not JSON")
    if not isinstance(data, list):
        raise PageChanged("the deals block on the deals page is not a list of deals")
    return data


def _span(values: list[float]) -> str:
    """`93%`, or `93% to 94.53%` over a deal's lines."""
    if not values:
        return ""
    low, high = min(values), max(values)
    return percent(high) if low == high else f"{percent(low)} to {percent(high)}"


def _needs(deal: dict) -> str:
    needs = [name for flag, name in (("anyReceiptRequired", "receipt photo"), ("anyCardPhotosRequired", "card photos"))
             if deal.get(flag)]
    return ", ".join(needs)


def line_row(deal: dict, line: dict) -> dict[str, Any]:
    """One line item: what AI pays for one card value of one brand under one deal."""
    # preApprovedQuantity is what the portal lets you reserve now (its own Cap column);
    # userPreApprovedQuantity is your ceiling and can be larger than what is left
    count, denom = line.get("preApprovedQuantity"), line.get("denom")
    needs = [name for flag, name in (("receiptRequired", "receipt photo"), ("photosRequired", "card photos"))
             if line.get(flag)]
    return {
        "deal": deal.get("id"),
        "brand": one_line(line.get("brand") or ""),
        "value": money(denom),
        "cost": percent(line.get("acquisitionCostRate")),   # what the deal makes the card cost you
        "pay": percent(line.get("buyRate")),                 # what AI pays
        "profit": percent(line.get("profitRate")),
        "cap": money(count * denom) if count is not None and denom is not None else "",   # yours, pre-approved
        "source": one_line(deal.get("dealSource") or ""),
        "title": one_line(deal.get("title") or ""),
        "cards": count,
        "left": line.get("globalQuantityRemaining"),         # cards everyone together may still reserve
        "needs": ", ".join(needs),
        "_pay": line.get("buyRate") or 0.0,
        "_profit": line.get("profitRate") if line.get("profitRate") is not None else float("-inf"),
        "_value": denom,
    }


def rate_rows(deals: list[dict]) -> list[dict[str, Any]]:
    return [line_row(d, line) for d in deals for line in d.get("lineItems") or []]


def deal_row(deal: dict) -> dict[str, Any]:
    """One deal: its source, brands, and the span of pay and profit over its lines."""
    lines = deal.get("lineItems") or []
    brands = list(dict.fromkeys(one_line(x.get("brand") or "") for x in lines))
    pays = [x["buyRate"] for x in lines if x.get("buyRate") is not None]
    profits = [x["profitRate"] for x in lines if x.get("profitRate") is not None]
    costs = [x["acquisitionCostRate"] for x in lines if x.get("acquisitionCostRate") is not None]
    return {
        "id": deal.get("id"),
        "title": one_line(deal.get("title") or ""),
        "source": one_line(deal.get("dealSource") or ""),
        "brands": ", ".join(brands),
        "pay": _span(pays),
        "profit": _span(profits),
        "cost": _span(costs),
        "cap": money(deal.get("remainingPreApprovalCapacity")),   # what you may still reserve
        "ends": eastern(deal.get("endDate")),
        "started": eastern(deal.get("startDate")),
        "needs": _needs(deal),
        "lines": len(lines),
        "_started": _stamp(deal.get("startDate"), 0.0),
        "_ends": _stamp(deal.get("endDate"), float("inf")),
        "_pay": max(pays, default=0.0),
        "_profit": max(profits, default=float("-inf")),
    }


def deal_detail(deal: dict) -> dict[str, Any]:
    """One deal in full, with its notes and every line."""
    row = {k: v for k, v in deal_row(deal).items() if not k.startswith("_")}
    hours = str(deal.get("reservationHoursLength") or "")
    return {**row,
            "url": deal.get("dealUrl") or "",
            "category": one_line(deal.get("categoryCoding") or ""),
            "hold": f"{hours} hours" if hours else "",       # how long a reservation is held
            "total_left": money(deal.get("remainingCapacityTotal")),
            "notes": (deal.get("notes") or "").strip(),
            "source_notes": (deal.get("dealSourceNotes") or "").replace("\r\n", "\n").strip(),
            "rates": [{k: v for k, v in line_row(deal, x).items() if not k.startswith("_")}
                      for x in deal.get("lineItems") or []]}
