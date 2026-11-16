"""CardCenter (cardcenter.cc): a gift card buyer. What it pays for which card, under which deal.

Read-only (D-80): the seller's rates page, the deals behind it and one deal's details, from the
JSON API the site's own pages call, with the user's browser login. Reserving, submitting cards and
anything else that sells or buys a card is never an action here (no tier-3, D-16).
"""

from __future__ import annotations

from typing import Literal

from esum import Listing, Next, NoSuchItem, Result, Tier, Transport, action, site

from .parse import deal_detail, deal_rows, rate_rows, takes

FORMAT = 1

cardcenter = site(
    name="cardcenter",
    base_url="https://cardcenter.cc",
    transport=Transport.BROWSER_HTTP,
    label={"en": "CardCenter: gift card buyer; current buy rates by brand and card value, the deals behind "
                 "them and each deal's details (needs the user's login)",
           "zh": "CardCenter：礼品卡回收平台，按品牌和面值查当前收购价、对应的 deal 及其详情（需登录）"},
    extras={"login_check": {"status": [401, 301, 302, 303, 307, 308]}},
)

RATES = "/Api/BuyOrders/v2"
REDIRECTS = (301, 302, 303, 307, 308)


def _read(ctx, path: str, params: dict | None = None):
    """One GET to a fixed path of the site's API, never followed anywhere else (D-80): a guest gets
    401 or is sent to the login page, and that redirect is the answer, not a place to go with the
    user's cookies."""
    resp = ctx.http.request("GET", path, params=params, headers={"Accept": "application/json"}, check=False,
                            follow_redirects=False)
    if resp.status == 401 or resp.status in REDIRECTS:
        raise ctx.login_needed(detail=f"HTTP {resp.status} from {path}")
    return resp


def _json(resp):
    """The answer's JSON. A failure keeps its status and drops its body: the API's answers carry
    the logged-in account, and an error could too."""
    if not resp.ok:
        raise resp.http_error("")
    return resp.json()
DEAL = Next("deal", {"id": "<deal>"}, "read a deal: where to buy, its dates, notes and every rate under it")


def _shown(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]


def _page(rows: list[dict], offset: int, limit: int) -> tuple[list[dict], dict | None]:
    return _shown(rows[offset:offset + limit]), {"offset": offset + limit} if offset + limit < len(rows) else None


def _matches(text: str, *fields: str) -> bool:
    wanted = text.casefold().strip()
    return any(wanted in f.casefold() for f in fields)


SORTS = {"profit": "best profit first", "margin": "best margin first", "pay": "highest pay first"}


def _ordered(rows: list[dict], sort: str, about: dict) -> list[dict]:
    """Best first by the chosen figure, or soonest-closing first; rows that lack it go last."""
    if sort == "ending":
        about["sort"] = "closing soonest first"
        return sorted(rows, key=lambda r: r["_until"] if r["_until"] is not None else float("inf"))
    if sort not in SORTS:
        return rows
    about["sort"] = SORTS[sort]
    return sorted(rows, key=lambda r: r[f"_{sort}"], reverse=True)


@action(cardcenter, tier=Tier.READ)
def rates(ctx, brand: str | None = None, value: float | None = None,
          sort: Literal["profit", "margin", "pay", "ending", "brand"] = "profit", offset: int = 0,
          limit: int = 50):
    """What CardCenter pays right now: one row per brand, card value and deal, from its Rates page.

    Columns: deal (the id for `deal --id`), brand, value (the card's face value; `$450-500 by $5` is
    a range in steps), cost (what the deal makes the card cost you), pay (what CardCenter pays),
    profit (pay minus cost) and cap (how much it still takes); cost, pay and profit are shares of
    the face value. `--fields` adds margin (the site's own figure: the gain relative to pay, with
    extras such as fuel points counted), title and vendor (the deal's), terms (days until payment),
    per_seller (your own limits), deadline (when to submit), until (CardCenter buys the line until
    then; empty when the site sets no time) and slug. Card credits and rewards are listed like
    purchases, with cost 100% or none: the deal's title and vendor say what a line is.

    Args:
        brand: Only brands whose name contains this, e.g. `amazon`, `home depot`.
        value: Only lines that take a card of this face value in dollars, ranges included, e.g. 100.
        sort: profit (best first), margin (the site's figure, best first), pay (highest first),
            ending (closing soonest first) or brand (the site's order).
        offset: Rows to skip.
        limit: Rows to show.
    """
    rows = rate_rows(_json(_read(ctx, RATES, {"pageSize": 0})))
    about: dict = {}
    if brand:
        about["brand"] = brand
        rows = [r for r in rows if _matches(brand, r["brand"], r["slug"])]
    bought = bool(brand and rows)          # the brand itself is bought, whatever the value filter leaves
    if value is not None:
        about["value"] = f"${value:g}"
        rows = [r for r in rows if takes(r, value)]
    rows = _ordered(rows, sort, about)
    shown, more = _page(rows, offset, limit)
    return Listing(name="rates", rows=shown, fields=("deal", "brand", "value", "cost", "pay", "profit", "cap"),
                   about=about, total=len(rows), more=more,
                   empty="CardCenter has no rate matching that" if about.keys() - {"sort"} else
                         "CardCenter lists no rates right now",
                   next=[DEAL] if rows else [
                       Next("rates", {"value": None, "offset": 0}, "see every value it buys of that brand",
                            when_empty=True) if bought else
                       Next("rates", {"brand": None, "value": None, "offset": 0}, "list every brand it buys",
                            when_empty=True)])


@action(cardcenter, tier=Tier.READ)
def deals(ctx, vendor: str | None = None,
          sort: Literal["site", "profit", "margin", "pay", "ending"] = "site", offset: int = 0, limit: int = 30):
    """The deals CardCenter is buying under right now: one row per deal, with its best line.

    A deal is a way to get the cards (a store promotion, a card-linked offer); its rates are the
    lines `rates` lists. Columns: id (for `deal --id`), vendor (where the deal is), title, brands,
    pay and profit (each the best among the deal's lines, so a deal ranks by its best line);
    `--fields` adds until (when the last of its lines stops being bought; empty when the site sets
    no time), margin (the site's own figure) and lines (how many rates it has). A profit near the
    pay means a line with no cost, such as a trade-in.

    Args:
        vendor: Only deals whose vendor or title contains this, e.g. `amex offer`, `costco`.
        sort: site (by brand, as the Rates page), the deal's best profit, margin or pay first, or
            ending (closing soonest first).
        offset: Rows to skip.
        limit: Rows to show.
    """
    rows = deal_rows(rate_rows(_json(_read(ctx, RATES, {"pageSize": 0}))))
    about: dict = {}
    if vendor:
        about["vendor"] = vendor
        rows = [r for r in rows if _matches(vendor, r["vendor"], r["title"])]
    rows = _ordered(rows, sort, about)
    shown, more = _page(rows, offset, limit)
    return Listing(name="deals", rows=shown, fields=("id", "vendor", "title", "brands", "pay", "profit"),
                   about=about, total=len(rows), more=more,
                   empty=f"no current deal matches {vendor!r}" if vendor else "CardCenter lists no deals right now",
                   next=[Next("deal", {"id": "<id>"}, "read a deal: where to buy, its dates, notes and rates")]
                   if rows else [Next("deals", {"vendor": None, "offset": 0}, "list every current deal", when_empty=True)])


def _rate_line(r: dict) -> str:
    if not r["pay"]:
        what = "no rate given"
    else:
        gain = f", profit {r['profit']} (site margin {r['margin']})" if r["profit"] else ""
        what = f"cost {r['cost'] or 'not given'} → pay {r['pay']}{gain}"
    limits = f"cap {r['cap'] or 'not given'}" + (f" ({r['per_seller']} per seller)" if r["per_seller"] else "")
    rest = [f"paid in {r['terms']}" if r["terms"] else "", f"submit {r['deadline']}" if r["deadline"] else "",
            f"bought until {r['until']}" if r["until"] else ""]
    return f"  {r['brand']} {r['value']}: {what}; {limits}; " + "; ".join(x for x in rest if x)


@action(cardcenter, tier=Tier.READ)
def deal(ctx, id: int):
    """One deal in full: where to buy, when it runs, the site's notes, and every rate under it.

    `--json` carries the same under these keys: id, title, vendor, brand, where, always (always
    available), url, price, list_price, starts, ends, confirmed (last confirmed), notes, category,
    category_note (about the category) and rates (each a `rates` row with every column).

    Args:
        id: The deal id from `rates` or `deals`, e.g. 894500.
    """
    resp = _read(ctx, f"/Api/Deals/{id}")
    if resp.status == 404:
        missing = NoSuchItem(f"CardCenter has no deal {id} (it may have ended)")
        missing.help = "Run `esum cardcenter deals` for the current deals and their ids"
        raise missing
    d = deal_detail(_json(resp))
    lines = [d["title"]]
    lines += [f"{name}: {d[key]}" for name, key in (
        ("vendor", "vendor"), ("brand", "brand"), ("where", "where"), ("url", "url"), ("price", "price"),
        ("list price", "list_price"), ("starts", "starts"), ("ends", "ends"), ("last confirmed", "confirmed"),
        ("category", "category")) if d[key]]
    if d["always"]:
        lines.append("always available")
    if d["notes"]:
        lines += ["", "notes:", d["notes"]]
    if d["category_note"]:
        lines += ["", f"about {d['category']}: {d['category_note']}"]
    lines += ["", "rates:" if d["rates"] else "rates: none right now"]
    lines += [_rate_line(r) for r in d["rates"]]
    return Result(data=d, text="\n".join(lines), summary=f"{d['title']} — {d['vendor']}",
                  next=[Next("rates", {"brand": d["rates"][0]["brand"]}, "compare every rate for this brand")]
                  if d["rates"] else [])
