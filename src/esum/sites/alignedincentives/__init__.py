"""Aligned Incentives (alignedincentiv.es): a gift card buyer. Its current deals and what it pays.

Read-only (D-80): the seller portal's deals page, which carries every current deal as JSON, with
the user's browser login. Reserving a deal, uploading cards and anything else that sells a card is
never an action here (no tier-3, D-16).
"""

from __future__ import annotations

from typing import Literal

from esum import ArgumentError, Listing, Next, NoSuchItem, Result, Tier, Transport, action, site

from .parse import deal_detail, deal_row, deals_json, rate_rows

FORMAT = 1

alignedincentives = site(
    name="alignedincentives",
    base_url="https://portal.alignedincentiv.es",
    transport=Transport.BROWSER_HTTP,
    label={"en": "Aligned Incentives: gift card buyer; current deals by source and brand, what it pays and the "
                 "profit per card value, and each deal's notes (needs the user's login)",
           "zh": "Aligned Incentives：礼品卡回收平台，按来源和品牌查当前 deal、收购价和利润，以及 deal 说明（需登录）"},
    extras={"login_check": {"status": [401, 301, 302, 303, 307, 308]}},
)

REDIRECTS = (301, 302, 303, 307, 308)

DEAL = Next("deal", {"id": "<id>"}, "read a deal: how to do it, its notes, dates and every rate")


def _deals(ctx) -> list[dict]:
    """The deals page's deals: one GET to that fixed path, never followed anywhere else (D-80). A
    guest is sent to the login page, and that redirect is the answer, not a place to go with the
    user's cookies. A failure keeps its status and drops its body, which names the account."""
    resp = ctx.http.request("GET", "/Seller/Deals", check=False, follow_redirects=False)
    if resp.status == 401 or resp.status in REDIRECTS:
        raise ctx.login_needed(detail=f"HTTP {resp.status} from /Seller/Deals")
    if not resp.ok:
        raise resp.http_error("")
    return deals_json(resp.text())


def _shown(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]


def _page(rows: list[dict], offset: int, limit: int) -> tuple[list[dict], dict | None]:
    return _shown(rows[offset:offset + limit]), {"offset": offset + limit} if offset + limit < len(rows) else None


def _has(text: str, *fields: str) -> bool:
    wanted = text.casefold().strip()
    return any(wanted in f.casefold() for f in fields)


@action(alignedincentives, tier=Tier.READ)
def deals(ctx, source: str | None = None, brand: str | None = None, photo_free: bool = False,
          sort: Literal["newest", "profit", "pay", "ending"] = "newest", offset: int = 0, limit: int = 30):
    """Every deal the portal lists now, one row per deal, newest first.

    A deal is a way to get cards somewhere (its source) that Aligned Incentives then buys. Columns:
    id (for `deal --id`), title, source, pay and profit (the span over the deal's card values, as
    shares of face value), cap (what you may still reserve) and ends; `--fields` adds brands,
    cost (the span of what the cards cost you), started, needs (receipt or card photos) and lines
    (how many card values it has). Sorting by
    profit or pay ranks a deal by its best line. A profit equal to the pay means a line with no
    cost: a credit or reward, not a purchase. Rates are for the payment speed the portal picks by
    default (the shortest available).

    Args:
        source: Only deals whose source or title contains this, e.g. `amex offer`, `newegg`, `heb`.
        brand: Only deals with a brand containing this, e.g. `amazon`, `delta`.
        photo_free: Only deals that need no receipt photo and no card photos.
        sort: newest (by start), profit (best first), pay (highest first) or ending (soonest first).
        offset: Rows to skip.
        limit: Rows to show.
    """
    found = _deals(ctx)
    about: dict = {}
    if brand:
        about["brand"] = brand
        found = [d for d in found if any(_has(brand, x.get("brand") or "") for x in d.get("lineItems") or [])]
    rows = [deal_row(d) for d in found]
    if source:
        about["source"] = source
        rows = [r for r in rows if _has(source, r["source"], r["title"])]
    if photo_free:
        about["needs"] = "no receipt or card photos"
        rows = [r for r in rows if not r["needs"]]
    key = {"newest": "_started", "profit": "_profit", "pay": "_pay", "ending": "_ends"}[sort]
    if sort != "newest":
        about["sort"] = {"profit": "best profit first", "pay": "highest pay first", "ending": "ending soonest first"}[sort]
    rows = sorted(rows, key=lambda r: r[key], reverse=sort != "ending")
    shown, more = _page(rows, offset, limit)
    return Listing(name="deals", rows=shown, fields=("id", "title", "source", "pay", "profit", "cap", "ends"),
                   about=about, total=len(rows), more=more,
                   empty="no current deal matches" if about else "the portal lists no deals right now",
                   next=[DEAL] if rows else [Next("deals", {"source": None, "brand": None, "photo_free": False, "offset": 0},
                                                  "list every current deal", when_empty=True)])


@action(alignedincentives, tier=Tier.READ)
def rates(ctx, brand: str | None = None, value: float | None = None,
          sort: Literal["profit", "pay", "brand"] = "profit", offset: int = 0, limit: int = 50):
    """What Aligned Incentives pays right now: one row per brand and card value of every deal.

    Columns: deal (the id for `deal --id`), brand, value (the card's face value), cost (what the
    deal makes the card cost you, as a share of face value), pay (what AI pays), profit, cap (what
    you may still reserve of it) and source; `--fields` adds title, cards (how many you may
    reserve), left (how many everyone together still may) and needs (receipt or card photos). A line
    with cost 0% is a credit or reward, not a purchase.

    Args:
        brand: Only brands whose name contains this, e.g. `amazon`, `chipotle`.
        value: Only cards of this face value in dollars, e.g. 100.
        sort: profit (best first), pay (highest first) or brand (A to Z).
        offset: Rows to skip.
        limit: Rows to show.
    """
    rows = rate_rows(_deals(ctx))
    about: dict = {"sort": {"profit": "best profit first", "pay": "highest pay first", "brand": "brand A to Z"}[sort]}
    if brand:
        about["brand"] = brand
        rows = [r for r in rows if _has(brand, r["brand"])]
    bought = bool(brand and rows)          # the brand itself is bought, whatever the value filter leaves
    if value is not None:
        about["value"] = f"${value:g}"
        rows = [r for r in rows if r["_value"] == value]
    rows = (sorted(rows, key=lambda r: (r["brand"].casefold(), -r["_pay"])) if sort == "brand" else
            sorted(rows, key=lambda r: r[f"_{sort}"], reverse=True))
    shown, more = _page(rows, offset, limit)
    return Listing(name="rates", rows=shown,
                   fields=("deal", "brand", "value", "cost", "pay", "profit", "cap", "source"),
                   about=about, total=len(rows), more=more,
                   empty="no current deal buys a card matching that" if about.keys() - {"sort"} else
                         "the portal lists no rates right now",
                   next=[Next("deal", {"id": "<deal>"}, "read a deal: how to do it, its notes, dates and every rate")]
                   if rows else [
                       Next("rates", {"value": None, "offset": 0}, "see every value it buys of that brand",
                            when_empty=True) if bought else
                       Next("rates", {"brand": None, "value": None, "offset": 0}, "list every brand it buys",
                            when_empty=True)])


@action(alignedincentives, tier=Tier.READ)
def deal(ctx, id: str):
    """One deal in full: how to do it, the portal's notes on it and on its source, and every rate.

    `--json` carries the same under these keys: id, title, source, brands, pay, profit, cost, cap
    (your cap), total_left (left for everyone), ends, started, hold (how long a reservation is held),
    needs, lines, url, category, notes, source_notes (about the source) and rates (each a `rates`
    row with every column).

    Args:
        id: The deal id from `deals` or `rates`, e.g. b6e64b5c-1bcf-4f0e-bb7f-2b1f18249409; its
            first 8 or more characters are enough when no other current deal starts the same.
    """
    wanted = id.strip().lower()
    matches = [d for d in _deals(ctx) if str(d.get("id", "")).lower().startswith(wanted)] if len(wanted) >= 8 else []
    if len(matches) > 1:
        raise ArgumentError(f"--id {id!r} starts {len(matches)} current deals' ids",
                            help="Give more of the id: `esum alignedincentives deals` lists them whole")
    if not matches:
        missing = NoSuchItem(f"the portal lists no deal {id!r} now (it may have ended, or the id is cut short: "
                             "ids are 36 characters, and at least the first 8 are needed)")
        missing.help = "Run `esum alignedincentives deals` for the current deals and their ids"
        raise missing
    d = deal_detail(matches[0])
    lines = [d["title"], f"source: {d['source']}"]
    lines += [f"{name}: {d[key]}" for name, key in (
        ("url", "url"), ("category", "category"), ("started", "started"), ("ends", "ends"),
        ("your cap", "cap"), ("left for everyone", "total_left"), ("a reservation is held for", "hold"),
        ("needs", "needs")) if d[key]]
    if d["notes"]:
        lines += ["", "notes:", d["notes"]]
    if d["source_notes"]:
        lines += ["", f"about {d['source']}:", d["source_notes"]]
    lines += ["", "rates (value: cost → pay, profit; your cap):"]
    lines += [f"  {r['brand']} {r['value']}: {r['cost']} → {r['pay']}, {r['profit']}; {r['cap'] or 'no cap given'}"
              + (f" ({r['cards']} cards; needs {r['needs']})" if r["needs"] else f" ({r['cards']} cards)")
              for r in d["rates"]]
    return Result(data=d, text="\n".join(lines), summary=f"{d['title']} — {d['source']}",
                  next=[Next("rates", {"brand": d["rates"][0]["brand"]}, "compare every rate for this brand")]
                  if d["rates"] else [])
