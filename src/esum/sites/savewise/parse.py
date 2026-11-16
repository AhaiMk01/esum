"""Savewise's data into rows: one merchant's portal rates and card offers (the API its browser
extension calls), the Top Offers lists, the stacking page and the discount gift cards (the
`__NEXT_DATA__` its public pages embed). Pure functions.

Rates come as a number and a symbol (`12.0` and `%`, `600.0` and `Miles`, `210` and `Miles/$` per
`unit_spent` dollars); rows show them as one string (`12%`, `600 miles`, `210 miles/$10`). The
portal's own wording (`Up to 11% Cash Back`) is kept as `listed`.
"""

from __future__ import annotations

import json
import re
from typing import Any

from esum import html_to_text, one_line

NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class PageChanged(RuntimeError):
    """A page or answer is not laid out as this parser knows it. Never carries page content."""


def page_props(html: str, where: str) -> dict:
    """A Next.js page's `props.pageProps`. PageChanged when it isn't there."""
    found = NEXT_DATA.search(html)
    if not found:
        raise PageChanged(f"{where} has no __NEXT_DATA__ block")
    try:
        props = json.loads(found.group(1))["props"]["pageProps"]
    except (ValueError, KeyError, TypeError):
        raise PageChanged(f"the __NEXT_DATA__ block on {where} has no pageProps") from None
    if not isinstance(props, dict):
        raise PageChanged(f"the __NEXT_DATA__ block on {where} has no pageProps")
    return props


def _num(value: Any) -> str:
    """`12`, `2.5`, `50,000`."""
    if not isinstance(value, (int, float)):
        return ""
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}".rstrip("0").rstrip(".")


UNITS = {"Miles": "miles", "Points": "points", "Miles/$": "miles/$", "Points/$": "points/$"}


def rate(value: Any, symbol: str | None, unit: Any = None, listed: str = "") -> str:
    """One rate as a string: `12%`, `$8`, `600 miles`, `5 points/$`, `210 miles/$10`; `up to `
    in front when the portal says so. Empty for a missing rate."""
    if not isinstance(value, (int, float)):
        return ""
    number = _num(value)
    if symbol == "%":
        shown = f"{number}%"
    elif symbol == "$":
        shown = f"${number}"
    elif symbol in ("Miles/$", "Points/$"):
        per = f"{_num(unit)}" if isinstance(unit, (int, float)) and unit > 1 else ""
        shown = f"{number} {UNITS[symbol]}{per}"
    else:
        shown = f"{number} {UNITS.get(symbol or '', (symbol or '').lower())}".rstrip()
    return f"up to {shown}" if listed.strip().lower().startswith("up to") else shown


KINDS = {"SPC": "cash %", "SDC": "cash $", "PPD": "points/$", "MPD": "miles/$", "PNT": "points", "SME": "miles"}
ORDER = list(KINDS)   # how `rates` groups a merchant's offers: like with like, best first in each


def offer_row(item: dict) -> dict:
    """One portal offer (the API's and the Top Offers lists' shape) as a row."""
    merchant, offer, portal = item.get("merchant") or {}, item.get("offer") or {}, item.get("portal") or {}
    listed = one_line(str(offer.get("offer_rate_raw") or ""))
    symbol, unit = offer.get("offer_type_symbol"), offer.get("unit_spent")
    kind = offer.get("offer_type_enum") or ""
    return {
        "portal": portal.get("name") or "",
        "rate": rate(offer.get("offer_rate"), symbol, unit, listed),
        "kind": KINDS.get(kind, offer.get("offer_type") or ""),
        "merchant": merchant.get("merchant_name") or "",
        "domain": merchant.get("website_url") or "",
        "bonus": rate(offer.get("bonus_offer_rate"), symbol, unit),
        "details": one_line(offer.get("additional_details") or ""),
        "listed": listed,
        "signup": one_line(portal.get("sign_up_bonus") or ""),
        "date": offer.get("offer_date_iso") or "",
        "link": offer.get("portal_merchant_url") or "",
        "_kind": kind,
        "_rate": _per_dollar(offer.get("bonus_offer_rate") or offer.get("offer_rate") or 0, kind, unit),
    }


def _per_dollar(value: Any, kind: str, unit: Any) -> float:
    """What ranks a row: a per-dollar award per one dollar (`210 miles/$10` ranks as 21/$), else the
    figure itself."""
    number = float(value) if isinstance(value, (int, float)) else 0.0
    if kind in ("PPD", "MPD") and isinstance(unit, (int, float)) and unit > 0:
        return number / unit
    return number


def merchant_offers(data: dict) -> tuple[dict, list[dict]]:
    """The API's `/offers/today/merchant/url` answer: the merchant, and its offers grouped by kind
    (cash back in percent first), best first within each."""
    body = data.get("data") if isinstance(data, dict) else None
    if not isinstance(body, dict) or not isinstance(body.get("offers"), list):
        raise PageChanged("the merchant offers answer has no data.offers list")
    rows = [offer_row({**o, "merchant": body.get("merchant")}) for o in body["offers"] if isinstance(o, dict)]
    rank = {k: i for i, k in enumerate(ORDER)}
    rows.sort(key=lambda r: (rank.get(r["_kind"], len(rank)), -float(r["_rate"])))
    return merchant_of(body.get("merchant")), rows


def merchant_of(merchant: dict | None) -> dict:
    m = merchant or {}
    return {"name": m.get("merchant_name") or "", "domain": m.get("website_url") or "",
            "slug": m.get("normalized_merchant_name") or ""}


def top_rows(props: dict) -> list[dict]:
    """A Top Offers page's list, in the site's order (best first)."""
    offers = (props.get("topOffersData") or {}).get("offers") if isinstance(props, dict) else None
    if not isinstance(offers, list):
        raise PageChanged("the Top Offers page has no topOffersData.offers list")
    return [offer_row(o) for o in offers if isinstance(o, dict)]


def _expires(offer: dict) -> str:
    when = offer.get("expiration_date_iso") or ""
    return when if re.fullmatch(r"\d{4}-\d\d-\d\d", when) else ""


def card_rows(data: dict) -> list[dict]:
    """The API's `/card-linked/offers` answer: one row per offer, soonest expiry first."""
    body = data.get("data") if isinstance(data, dict) else None
    if not isinstance(body, dict) or not isinstance(body.get("offers"), list):
        raise PageChanged("the card offers answer has no data.offers list")
    rows = []
    for o in body["offers"]:
        if not isinstance(o, dict):
            continue
        d, cards = o.get("offer_details") or {}, [c for c in o.get("cards") or [] if isinstance(c, dict)]
        banks = sorted({c.get("bank_name") or "" for c in cards} - {""})
        names = list(dict.fromkeys(one_line(c.get("pretty_card_name") or c.get("card_name") or "") for c in cards))
        reward = {k: d.get(k) for k in ("minimum_reward", "maximum_reward")}
        rows.append({
            "id": o.get("offer_id"),
            "bank": ", ".join(banks),
            "rate": rate(d.get("offer_rate"), d.get("symbol")),
            "offer": one_line(html_to_text(d.get("description") or "", keep_links=False)),
            "expires": _expires(d),
            "cards": ", ".join(names),
            "starts": d.get("start_date") or "",
            "spend": f"${_num(reward['minimum_reward'])}" if isinstance(reward["minimum_reward"], (int, float)) else "",
            "max": f"${_num(reward['maximum_reward'])}" if isinstance(reward["maximum_reward"], (int, float)) else "",
            "category": (d.get("offer_category") or "").lower(),
            "terms": one_line(html_to_text(d.get("terms") or "", keep_links=False)),
        })
    rows.sort(key=lambda r: r["expires"] or "9999")
    return rows


STACKS = {   # the stacking page's blocks: which bank's card offers, stacked on which portal
    "AmexOffers": ("amex", "rakuten"), "ChaseOffers": ("chase", "rakuten"), "CitiOffers": ("citi", "rakuten"),
    "SimplyMilesOffers": ("simplymiles", "aadvantage"), "RoveAmexOffers": ("amex", "rove"),
    "RoveChaseOffers": ("chase", "rove"),
}


def stack_rows(props: dict) -> list[dict]:
    """The stacking page: one row per card offer, with the portal's rate at the same merchant."""
    blocks = {k: props.get(k) for k in STACKS if isinstance(props.get(k), dict)}
    if not blocks:
        raise PageChanged("the stacking page has none of its bank blocks")
    rows = []
    for key, block in blocks.items():
        bank, portal_key = STACKS[key]
        portal = block.get("portal") or portal_key
        for m in block.get("merchants") or []:
            if not isinstance(m, dict):
                continue
            merchant = merchant_of(m.get("merchant"))
            portals = [rate(p.get("offer_rate"), p.get("symbol"), p.get("unit_spent"), str(p.get("offer_rate_raw") or ""))
                       for p in m.get("offers") or [] if isinstance(p, dict)]
            for c in m.get("card_offers") or []:
                if not isinstance(c, dict):
                    continue
                rows.append({
                    "merchant": merchant["name"], "bank": bank, "card": rate(c.get("offer_rate"), c.get("symbol")),
                    "portal": portal, "portal_rate": ", ".join(x for x in portals if x),
                    "expires": _expires(c), "domain": merchant["domain"],
                    "offer": one_line(html_to_text(c.get("description") or c.get("offer_rate_raw") or "",
                                                   keep_links=False)),
                    "starts": c.get("start_date") or "",
                    "_portal": portal_key,
                })
    return rows


def gift_rows(props: dict) -> list[dict]:
    """The gift cards page: CardDepot's discounted cards and Rove's miles-earning cards, in stock
    first, then biggest discount, then most miles."""
    cards = props.get("giftCardOffers") if isinstance(props, dict) else None
    if not isinstance(cards, list):
        raise PageChanged("the gift cards page has no giftCardOffers list")
    rows = []
    for c in cards:
        if not isinstance(c, dict):
            continue
        bps, miles = c.get("discount_bps"), c.get("miles_multiplier")
        rows.append({
            "brand": one_line(c.get("provider_brand") or ""),
            "seller": c.get("provider") or "",
            "discount": f"{_num(bps / 100)}%" if isinstance(bps, (int, float)) else "",
            "miles": (f"{'up to ' if c.get('miles_is_up_to') else ''}{_num(miles)} miles/$"
                      if isinstance(miles, (int, float)) else ""),
            "stock": "yes" if c.get("in_stock") else "no",
            "url": c.get("url_to_buy") or "",
            "_bps": bps if isinstance(bps, (int, float)) else -1,
            "_miles": miles if isinstance(miles, (int, float)) else -1,
            "_stock": bool(c.get("in_stock")),
        })
    rows.sort(key=lambda r: (not r["_stock"], -r["_bps"], -r["_miles"]))
    return rows
