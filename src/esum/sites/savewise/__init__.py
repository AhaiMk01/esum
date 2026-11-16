"""Savewise (getsavewise.com): what each shopping portal pays at a store, the card-linked offers
there, the Top Offers lists, the bank-offer stacks and discount gift cards. Read-only and public.

A store's portal rates and card offers come from `api.getsavewise.com`, the API Savewise's own
browser extension calls (PORTED_FROM: the Savewise Chrome extension, its
`src/background/api/{auth,endpoints}.js`, read and rewritten in Python). The extension gets an
anonymous token for every install, signed in or not, in two steps: `POST /auth/challenge` and
`POST /auth/authenticate` with `client_id: chrome_ext`; ESUM does the same for each call and keeps
nothing. The extension also carries fixed "legacy" tokens; ESUM never uses them. The website's
store pages hide these rates from guests; the extension shows them to anyone (D-81).

The Top Offers lists, the stacking page and the gift cards are the website's public pages, read
from the `__NEXT_DATA__` they embed.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import quote

from esum import ArgumentError, Credential, Listing, Next, NoSuchItem, Tier, Transport, action, site

from .parse import PageChanged, card_rows, gift_rows, merchant_of, merchant_offers, page_props, stack_rows, top_rows

FORMAT = 1
PORTED_FROM = ("Savewise Chrome extension (Chrome Web Store id mmdidpkgkknffjbnnbpcdpchhbmdmbga), "
               "background.js: the anonymous token steps and the two read endpoints, as facts about the API; "
               "no code copied")

API = "https://api.getsavewise.com"
CLIENT = "chrome_ext"     # the extension's client id: an anonymous token, no account

savewise = site(
    name="savewise",
    base_url="https://getsavewise.com",
    transport=Transport.DIRECT,
    label={"en": "Savewise: what every shopping portal (Rakuten, TopCashBack, Capital One Shopping, airline "
                 "malls…) pays at a store, its card-linked offers, Top Offers lists, bank-offer stacks "
                 "and discount gift cards",
           "zh": "Savewise：某商家在各返利站（Rakuten、TopCashBack、C1 Shopping、航空商城等）的返现率、"
                 "卡 offer、Top Offers 榜单、银行 offer 叠加和折扣礼品卡"},
)


def _answer(resp, what: str) -> dict:
    """An API answer's JSON; a failure keeps its status and drops its body."""
    if not resp.ok:
        raise resp.http_error("")
    try:
        return resp.json()
    except ValueError:
        raise PageChanged(f"the {what} answer is not JSON") from None


def _token(ctx) -> Credential:
    """An anonymous API token, as the extension gets one; it lives for this call only."""
    challenge = _answer(ctx.http.request("POST", f"{API}/auth/challenge", json={"client_id": CLIENT},
                                         check=False, follow_redirects=False), "token challenge")
    granted = _answer(ctx.http.request("POST", f"{API}/auth/authenticate", follow_redirects=False, check=False,
                                       json={"client_id": CLIENT,
                                             "challenge_token": challenge.get("challenge_token")}), "token")
    token = granted.get("access_token")
    if not isinstance(token, str) or not token:
        raise PageChanged("the token answer has no access_token")
    return Credential({"Authorization": f"Bearer {token}"})


def _page(ctx, path: str) -> dict:
    return page_props(ctx.http.get(path).text(), path)


def _domain(ctx, merchant: str) -> str:
    """The store's web address, from what the user typed: an address is used as it is; a name is
    looked up on Savewise's store page of that name."""
    wanted = merchant.strip()
    if not wanted:
        raise ArgumentError("--merchant is empty")
    if "." in wanted or "/" in wanted:
        return wanted
    found = merchant_of((_page(ctx, f"/merchant/{quote(wanted.lower())}").get("merchantData") or {}).get("merchant"))
    if not found["domain"]:
        missing = NoSuchItem(f"Savewise has no store named {merchant!r}")
        missing.help = (f"Pass the store's web address instead, e.g. `esum savewise rates --merchant "
                        f"{wanted.lower().replace(' ', '')}.com`")
        raise missing
    return found["domain"]


def _shown(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]


def _window(rows: list[dict], offset: int, limit: int) -> tuple[list[dict], dict | None]:
    return _shown(rows[offset:offset + limit]), {"offset": offset + limit} if offset + limit < len(rows) else None


def _rates_step(rows: list[dict]) -> Next:
    """`rates --merchant` with the store's address when every row is that one store's."""
    domains = {r["domain"] for r in rows}
    domain = domains.pop() if len(domains) == 1 and "" not in domains else "<domain>"
    return Next("rates", {"merchant": domain}, "see every portal's rate at one store")


def _matches(text: str, *fields: str) -> bool:
    wanted = text.casefold().strip()
    return any(wanted in (f or "").casefold() for f in fields)


@action(savewise, tier=Tier.READ)
def rates(ctx, merchant: str):
    """What every shopping portal pays at one store today, as Savewise's extension shows it.

    Rows are grouped by kind (cash back in percent, flat cash back, points or miles per dollar,
    flat points, flat miles), best first in each; kinds don't compare with one another. Columns:
    portal, rate (`up to` when the portal says so), bonus (the rate with the portal's current
    bonus, which ranks the row; empty without one) and details (the portal's rate by category or
    exclusions, when it has them). `--fields` adds kind, listed (the portal's own wording), signup
    (its sign-up bonus), date, merchant, domain and link
    (the portal's store page, through Savewise's referral link).

    Args:
        merchant: The store's web address or Savewise's name for it, e.g. `walmart.com`, `best buy`.
    """
    domain = _domain(ctx, merchant)
    resp = ctx.http.request("GET", f"{API}/offers/today/merchant/url", params={"url": domain},
                            credential=_token(ctx), check=False, follow_redirects=False)
    if resp.status in (400, 404):
        missing = NoSuchItem(f"Savewise has no store at {domain!r}")
        missing.help = "Check the address (e.g. `bestbuy.com`), or try the store's name with `--merchant`"
        raise missing
    store, rows = merchant_offers(_answer(resp, "store offers"))
    return Listing(name="rates", rows=_shown(rows), fields=("portal", "rate", "bonus", "details"),
                   about={"merchant": store["name"] or domain, "domain": store["domain"] or domain},
                   total=len(rows), empty=f"no portal pays at {store['name'] or domain} today",
                   next=[Next("cards", {"merchant": store["domain"] or domain},
                              "see the card-linked offers (Amex, Chase, Citi…) at this store", when_empty=True)])


@action(savewise, tier=Tier.READ)
def cards(ctx, merchant: str, bank: str | None = None):
    """The card-linked offers at one store (Amex Offers, Chase Offers, Citi Merchant Offers…).

    One row per offer, soonest expiry first. Columns: bank, rate, offer (what to spend, what you
    get) and expires. `--fields` adds id, cards (the cards it has been seen on), starts, spend (the
    least to spend), max (the most it pays), category and terms. Whether a given card has the offer
    is up to the bank; these are offers Savewise has seen.

    Args:
        merchant: The store's web address or Savewise's name for it, e.g. `walmart.com`, `best buy`.
        bank: Only this bank's offers, e.g. `amex`, `chase`, `citi`.
    """
    domain = _domain(ctx, merchant)
    rows = card_rows(_answer(ctx.http.request("GET", f"{API}/card-linked/offers", params={"domain_url": domain},
                                              credential=_token(ctx), check=False, follow_redirects=False),
                             "card offers"))
    about = {"merchant": domain}
    if bank:
        about["bank"] = bank
        rows = [r for r in rows if _matches(bank, r["bank"])]
    return Listing(name="cards", rows=rows, fields=("bank", "rate", "offer", "expires"), about=about,
                   total=len(rows), empty=f"Savewise lists no card offer at {domain}" + (f" from {bank}" if bank else ""),
                   next=[Next("rates", {"merchant": domain}, "see what each shopping portal pays there", when_empty=True)])


Board = Literal["cashback", "miles", "points", "multipliers"]


@action(savewise, tier=Tier.READ)
def top(ctx, by: Board = "cashback", portal: str | None = None, merchant: str | None = None, offset: int = 0,
        limit: int = 30):
    """Savewise's Top Offers: the biggest portal payouts across every store, best first.

    `cashback` is flat cash back ($ per sign-up or purchase), `miles` and `points` are flat awards,
    `multipliers` is everything per dollar (cash back %, miles or points per $). Columns: merchant,
    portal, rate and domain; `--fields` adds kind, bonus, details, listed, signup, date and link.
    The lists are long (thousands of rows); filter by portal or store.

    Args:
        by: Which list: cashback, miles, points or multipliers.
        portal: Only this portal's offers, e.g. `rakuten`, `topcashback`, `rove`.
        merchant: Only stores whose name or address contains this.
        offset: Rows to skip.
        limit: Rows to show.
    """
    rows = top_rows(_page(ctx, f"/top-offers/{by}"))
    about: dict = {"list": by}
    if portal:
        about["portal"] = portal
        rows = [r for r in rows if _matches(portal, r["portal"])]
    if merchant:
        about["merchant"] = merchant
        rows = [r for r in rows if _matches(merchant, r["merchant"], r["domain"])]
    shown, more = _window(rows, offset, limit)
    return Listing(name="top", rows=shown, fields=("merchant", "portal", "rate", "domain"), about=about,
                   total=len(rows), more=more, empty="no offer on that list matches",
                   next=[_rates_step(rows)] if rows else
                   [Next("top", {"portal": None, "merchant": None, "offset": 0}, "list the whole list", when_empty=True)])


@action(savewise, tier=Tier.READ)
def stack(ctx, bank: Literal["amex", "chase", "citi", "simplymiles"] | None = None,
          portal: Literal["rakuten", "rove", "aadvantage"] | None = None, merchant: str | None = None,
          sort: Literal["site", "expires"] = "site", offset: int = 0, limit: int = 30):
    """Bank card offers that stack with a shopping portal at the same store, from Savewise's stacking page.

    The page pairs Amex, Chase and Citi offers with Rakuten, Amex and Chase offers with Rove, and
    SimplyMiles with the AAdvantage eShopping mall. Columns: merchant, bank, card (the card offer's
    rate), portal and portal_rate (that portal's rate there now); `--fields` adds offer (its
    wording), expires, starts and domain.

    Args:
        bank: Only this bank's offers.
        portal: Only stacks on this portal.
        merchant: Only stores whose name or address contains this.
        sort: site (the page's order) or expires (soonest first).
        offset: Rows to skip.
        limit: Rows to show.
    """
    rows = stack_rows(_page(ctx, "/top-offers/stack"))
    about: dict = {}
    for key, wanted in (("bank", bank), ("portal", portal)):
        if wanted:
            about[key] = wanted
            # the portal flag is the stable block key: the page names AAdvantage `AmericanAirlines`
            rows = [r for r in rows if (r["_portal"] == wanted if key == "portal" else _matches(wanted, r[key]))]
    if merchant:
        about["merchant"] = merchant
        rows = [r for r in rows if _matches(merchant, r["merchant"], r["domain"])]
    if sort == "expires":
        about["sort"] = "soonest expiry first"
        rows = sorted(rows, key=lambda r: r["expires"] or "9999")
    shown, more = _window(rows, offset, limit)
    return Listing(name="stack", rows=shown, fields=("merchant", "bank", "card", "portal", "portal_rate"),
                   about=about, total=len(rows), more=more, empty="no stack matches",
                   next=[_rates_step(rows)] if rows else
                   [Next("stack", {"bank": None, "portal": None, "merchant": None, "offset": 0}, "list every stack",
                         when_empty=True)])


@action(savewise, tier=Tier.READ)
def giftcards(ctx, brand: str | None = None, offset: int = 0, limit: int = 50):
    """Discounted gift cards Savewise lists (from CardDepot), in stock first, biggest discount first.

    Columns: brand, discount (off face value), stock and seller; `--fields` adds face and price
    (when one card is listed), seen (when Savewise last saw it, UTC) and url (the seller's page).
    A discount is the best one seen; the seller's page has the denominations and today's price.

    Args:
        brand: Only brands whose name contains this, e.g. `home depot`.
        offset: Rows to skip.
        limit: Rows to show.
    """
    rows = gift_rows(_page(ctx, "/gift-cards"))
    about: dict = {}
    if brand:
        about["brand"] = brand
        rows = [r for r in rows if _matches(brand, r["brand"])]
    shown, more = _window(rows, offset, limit)
    return Listing(name="giftcards", rows=shown, fields=("brand", "discount", "stock", "seller"), about=about,
                   total=len(rows), more=more, empty=f"no discounted {brand} card listed" if brand else
                   "Savewise lists no discounted gift cards right now",
                   next=[] if rows else [Next("giftcards", {"brand": None, "offset": 0}, "list every discounted card",
                                              when_empty=True)])
