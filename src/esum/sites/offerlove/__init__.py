"""Offer.love: card-linked merchant offers (Amex Offers, Chase Offers, BankAmeriDeals…) from the public
v1 API (search, bank counts) and the site's server-rendered pages (merchant, bank, latest, expiring,
one offer, the merchant directory), all public. `my-offers` reads the signed-in user's own offers from the My Offers page in
the browser, through a page script (D-95); it needs no key (D-78)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from esum import ArgumentError, Listing, NoSuchItem, Next, Result, Tier, Transport, action, site

from . import parse
from .parse import (BANKS, PageChanged, api_rows, bank_counts, directory, in_directory, list_rows, merchant_page,
                    merchant_slug, merge, matches, my_offer_rows, nearest_merchants, offer_id, offer_page,
                    page_count, plain, row_count, split_expired)

FORMAT = 1
EXTRAS = ()

API = "https://data.offer.love/v1"
MY_OFFERS = "https://offer.love/myoffers/"
MY_OFFERS_SCRIPT = (Path(__file__).parent / "myoffers.js").read_text(encoding="utf-8")
MY_OFFERS_WAIT = 45   # seconds for the page to sign in and sync the offers

Bank = Literal["amex", "boa", "chase", "citi", "paypal", "us-bank", "wells-fargo"]
Sort = Literal["site", "expires"]

offerlove = site(
    name="offerlove",
    base_url="https://offer.love",
    transport=Transport.DIRECT,
    fallback=Transport.IN_PAGE,   # the site's pages sit behind a Cloudflare check at times; its API does not
    label={"en": "Offer.love: card-linked merchant offers (Amex Offers, Chase Offers, BoA, Citi, US Bank, "
                 "Wells Fargo, PayPal): search by merchant, list by bank, newest and expiring, full terms, "
                 "and the directory of every merchant with an offer",
           "zh": "Offer.love：各银行信用卡商家 offer（Amex Offers、Chase Offers 等）聚合，按商家/银行查，最新与快到期，"
                 "以及所有有 offer 的商家目录"},
)

OFFER = Next("offer", {"id": "<id>"}, "read an offer's full terms")


def _merchant_step(rows: list[dict]) -> Next:
    """`merchant --slug` with the real slug when every row is one merchant's, else a placeholder."""
    slugs = {r["merchant"] for r in rows}
    slug = slugs.pop() if len(slugs) == 1 else "<merchant>"
    return Next("merchant", {"slug": slug}, "list every active offer at that merchant")


def _columns(filtered: bool, last: str) -> tuple[str, ...]:
    # one bank's rows all carry its name; show the merchant there instead
    return ("id", "name", "title", last) if filtered else ("id", "bank", "title", last)


def _sorted(rows: list[dict], sort: str, about: dict) -> list[dict]:
    if sort == "expires":
        about["sort"] = "soonest expiry first"
        return sorted(rows, key=lambda r: r.get("expires") or "9999")
    return rows


def _page(rows: list[dict], offset: int, limit: int) -> tuple[list[dict], dict | None]:
    shown = rows[offset:offset + limit]
    return shown, {"offset": offset + limit} if offset + limit < len(rows) else None


def _past(rows: list[dict], shown: list[dict], offset: int) -> str | None:
    """Why a page is empty although rows matched: the offset is past the last of them."""
    return f"no row at offset {offset}: {len(rows)} matched" if rows and not shown else None


def _first_page(action_name: str) -> Next:
    return Next(action_name, {"offset": 0}, "start from the first row", when_empty=True)


@action(offerlove, tier=Tier.READ)
def search(ctx, query: str):
    """Active offers whose merchant, bank or wording matches, through the public API.

    Expired offers never match (a merchant's recent ones are in `merchant --past`), and the API
    misses some live offers, so a 0 here is not proof: check `merchant --slug`. The API answers at most 10 offers per call with no paging; when 10 come back there may be more,
    so take the merchant slug to `merchant` for all of one merchant's offers. Columns: offer id (for
    `offer --id`), bank, title and expiry date; `--fields` adds merchant (the slug for `merchant
    --slug`), name, added, terms, offer_id (the API's own id, unused by other actions) and url.
    Anonymous use is limited to 100 API calls a day.

    Args:
        query: Merchant name, bank or keyword (at least 2 characters), e.g. `walmart`, `doordash`.
    """
    data = ctx.http.get_json(f"{API}/search", params={"q": query})
    rows = api_rows(data)
    answered = len(data.get("results") or [])
    about = {"query": query}
    if answered >= int(data.get("per_page") or 10):
        about["note"] = "the API returns at most 10 offers; more may exist (use merchant --slug)"
    if answered > len(rows):
        about["merged"] = f"{answered - len(rows)} offers share another offer's page (same title and date)"
    guess = re.sub(r"[^a-z0-9]", "", query.lower()) or "<merchant>"
    return Listing(name="offers", rows=rows, fields=_columns(False, "expires"), about=about,
                   total=len(rows), empty=f"no active offer matches {query!r}",
                   next=[OFFER, _merchant_step(rows)] if rows else [
                       Next("merchant", {"slug": guess},
                            "read the merchant's page: the API search misses some offers, and the page "
                            "has past ones too (slugs are names without spaces)", when_empty=True)])


@action(offerlove, tier=Tier.READ)
def merchant(ctx, slug: str, bank: Bank | None = None, past: bool = False):
    """Every active offer at one merchant, across banks, with its terms.

    Columns: offer id, bank, title and expiry date (and status with `--past`); `--fields` adds
    status, merchant (the slug), name (the merchant's name on that offer), age (since it was
    added), terms and url. Find a slug with `merchants --query` (the site's directory) or `search`;
    a slug is usually the name without spaces, and the page is live, so guessing one is fine.

    Args:
        slug: Merchant slug, the first part of an offer id (`walmart`), or the merchant page URL.
        bank: Only this bank's offers.
        past: Also list the merchant's recently expired offers (the site's Past Offers).
    """
    slug = merchant_slug(slug)
    found = merchant_page(ctx.http.get_text(f"/offers/{slug}/"))
    if found is None:
        return Listing(name="offers", rows=[], fields=_columns(False, "expires"),
                       about={"merchant": slug}, total=0,
                       empty=f"offer.love has no merchant with the slug {slug!r}",
                       next=[Next("merchants", {"query": slug}, "find the merchant's slug in the directory",
                                  when_empty=True),
                             Next("search", {"query": slug}, "search offers' wording through the API",
                                  when_empty=True)])
    name, rows = found
    if not past:
        rows = [r for r in rows if r["status"] == "active"]
    if bank:
        rows = [r for r in rows if r["bank"] == BANKS[bank][1]]
    which = "active or past" if past else "active"
    fields = ("id", "bank", "title", "expires", "status") if past else ("id", "bank", "title", "expires")
    about = {"merchant": f"{name} ({slug})", "bank": BANKS[bank][1] if bank else "all",
             "url": f"{ctx.site.base_url}/offers/{slug}/"}
    if rows:
        about["note"] = ("one row per title and bank, as the page lists them; the directory (`merchants`) can "
                         "count more, since offers that differ only by location share a row")
    return Listing(name="offers", rows=rows, fields=fields, about=about,
                   total=len(rows),
                   empty=f"{name} has no {which} {BANKS[bank][1] + ' ' if bank else ''}offers",
                   next=[OFFER] + ([] if past else [Next("merchant", {"slug": slug, "past": True},
                                                         "include recently expired offers", when_empty=True)]))


@action(offerlove, tier=Tier.READ)
def merchants(ctx, query: str | None = None, min_offers: int = 1, sort: Literal["offers", "name"] = "offers",
              offset: int = 0, limit: int = 50):
    """The merchants with an active offer and how many each has: the site's Offer Directory, whole.

    One request reads the whole directory (several thousand merchants), so filtering and paging
    cost no more. Columns: merchant (the slug `merchant --slug` takes), name and offers (the
    directory's count of the merchant's active offers; `merchant` can list fewer, since it shows
    one offer per title and bank). `--fields` adds url. The header gives the directory's size and,
    when you filter, the offers at the merchants found. This is the way to the site's offers as
    a whole: pick merchants here, then read each with `merchant`.

    The site rebuilds the directory about once a day, so it is not complete: a merchant whose
    offers were all added since then is missing (Chipotle was, with a live Citi offer), and banks
    name a merchant differently, so one brand can have two entries (`kiehls` on its Chase offer's
    page, `kiehlssince1851` here). A merchant missing here is not proof it has no offer: read its
    page with `merchant --slug <name without spaces>`. A name can also be a bank's own label for
    an offer rather than a store (`The`, `Earn`).

    Args:
        query: Only merchants whose name or slug has every word of this, ignoring case, spaces, punctuation and accents (`pf changs`, `bagel`).
        min_offers: Only merchants with at least this many active offers (1 or more).
        sort: offers (most offers first, in the site's order among equals) or name (A to Z).
        offset: Rows to skip.
        limit: Rows to show.
    """
    if min_offers < 1:
        raise ArgumentError(f"--min-offers {min_offers} is below 1: every merchant in the directory has an offer",
                            help="give 1 or more, e.g. `--min-offers 5`")
    if query is not None and not plain(query):
        raise ArgumentError(f"--query {query!r} has no letter or digit to look for",
                            help="give a word from the merchant's name, e.g. `--query bagel`")
    found = directory(ctx.http.get_text("/offers/"))
    about: dict = {"url": f"{ctx.site.base_url}/offers/",
                   "site_total": f"{len(found)} merchants, {sum(r['offers'] or 0 for r in found)} offers"}
    named = [r for r in found if in_directory(r, query)] if query else found
    near: list[dict] = []
    if query:
        about["query"] = query
        if not named and (near := nearest_merchants(found, query)):
            about["closest"] = ", ".join(f"{r['merchant']} ({r['name']})" for r in near)
    rows = named
    if min_offers > 1:
        about["min_offers"] = min_offers
        rows = [r for r in named if (r["offers"] or 0) >= min_offers]
    if rows and len(rows) != len(found):
        about["offers"] = sum(r["offers"] or 0 for r in rows)
    if sort == "name":
        about["sort"] = "name"
        rows = sorted(rows, key=lambda r: r["name"].casefold())
    shown, more = _page(rows, offset, limit)
    if shown:
        about["note"] = ("offers is the directory's count; `merchant` shows one offer per title and bank, "
                         "often fewer (many differ only by location)")

    past = _past(rows, shown, offset)
    empty, steps = "the directory lists no merchant", []
    if past:
        empty, steps = past, [_first_page("merchants")]
    elif query and not named:
        # nothing has that name: the directory may be behind, so point at the merchant's own page
        empty = (f"no merchant in the directory matches {query!r}; the directory is a day behind at times, "
                 "so this is not proof")
        guess = re.sub(r"[^a-z0-9]", "", query.lower())
        if near:
            steps.append(Next("merchant", {"slug": near[0]["merchant"]},
                              f"list the offers at {near[0]['name']}, the closest name", when_empty=True))
        if guess and not (near and near[0]["merchant"] == guess):
            steps.append(Next("merchant", {"slug": guess}, "read that merchant's own page, which is live (a slug "
                              "is usually the name without spaces)", when_empty=True))
        steps += [Next("search", {"query": query}, "search offers' wording through the API", when_empty=True),
                  Next("merchants", {"query": None, "min_offers": 1, "offset": 0}, "list the whole directory",
                       when_empty=True)]
    elif min_offers > 1:
        # the name matched; only the count filter left nothing
        empty = (f"{len(named)} {'merchant matches' if len(named) == 1 else 'merchants match'} {query!r}, "
                 f"none with {min_offers} or more offers" if query
                 else f"no merchant in the directory has {min_offers} or more offers")
        steps = [Next("merchants", {"min_offers": 1, "offset": 0}, "drop the offer-count filter", when_empty=True)]
    return Listing(name="merchants", rows=shown, fields=("merchant", "name", "offers"), about=about,
                   total=len(rows), more=more, empty=empty,
                   next=[_merchant_step(shown)] if shown else steps)


def _rows(n: int) -> str:
    return f"{n} row" if n == 1 else f"{n} rows"


def _live(rows: list[dict], about: dict, listed: int | None = None) -> list[dict]:
    if listed is not None and listed > len(rows):
        repeats = listed - len(rows)
        about["merged"] = f"{_rows(repeats)} {'repeats' if repeats == 1 else 'repeat'} another row's page"
    rows, hidden = split_expired(rows, parse.today())
    if hidden:
        about["hidden"] = f"{_rows(hidden)} already past their expiry date (the site's list lags)"
    return rows


@action(offerlove, tier=Tier.READ)
def bank(ctx, name: Bank, sort: Sort = "site", offset: int = 0, limit: int = 30):
    """One bank's active offers as its page on the site lists them: the first 100, in the site's order.

    The header gives the bank's whole count. The site lists no more than these 100 for a bank and
    its directory doesn't say which bank a merchant's offers are from, so the rest can't be listed:
    start from the merchant you care about (`merchant --slug <slug> --bank <bank>`; `merchants`
    finds the slug) or try `search`. Rows already past their expiry date are left out (`hidden`), and rows
    that repeat another row's offer page are merged (`merged`). Columns: offer id,
    merchant name, title and expiry date; `--fields` adds merchant, bank, excerpt (the start of the
    terms), due (the site's `Exp Dec 5` badge, a UTC date) and url.

    Args:
        name: The bank.
        sort: site (the page's order) or expires (soonest first, within those 100).
        offset: Rows to skip.
        limit: Rows to show (max 100).
    """
    slug, label = BANKS[name]
    page = ctx.http.get_text(f"/offers/bank/{slug}/")
    offers, merchants = page_count(page)
    about = {"bank": label, "url": f"{ctx.site.base_url}/offers/bank/{slug}/"}
    if offers is not None:
        about["site_total"] = (f"{offers} active offers from {merchants} merchants; "
                               f"the page shows only its first {row_count(page)}")
    rows = _sorted(_live(list_rows(page), about, row_count(page)), sort, about)
    shown, more = _page(rows, offset, limit)
    past = _past(rows, shown, offset)
    return Listing(name="offers", rows=shown, fields=_columns(True, "expires"), about=about,
                   total=len(rows), more=more, empty=past or f"offer.love lists no active {label} offers",
                   next=[OFFER, _merchant_step(shown),
                         _first_page("bank") if past else Next("banks", {}, "see which banks have offers",
                                                              when_empty=True)])


@action(offerlove, tier=Tier.READ)
def banks(ctx):
    """Every bank offer.love tracks, with its count of active offers (the value for `bank --name`)."""
    rows = bank_counts(ctx.http.get_json(f"{API}/banks"))
    return Listing(name="banks", rows=rows, fields=("bank", "offers"), total=len(rows),
                   empty="the API lists no banks",
                   next=[Next("bank", {"name": "<bank>"}, "list a bank's offers")])


def _feed(ctx, path: str, what: str, bank: str | None, offset: int, limit: int, last: str,
          empty_steps: list[Next], sort: str = "site"):
    about = {"list": what, "bank": BANKS[bank][1] if bank else "all"}
    rows = list_rows(ctx.http.get_text(path), merge=False)
    if bank:
        rows = [r for r in rows if r["bank"] == BANKS[bank][1]]
    rows = _sorted(_live(merge(rows), about, len(rows)), sort, about)
    shown, more = _page(rows, offset, limit)
    past = _past(rows, shown, offset)
    return Listing(name="offers", rows=shown, fields=_columns(bool(bank), last), about=about,
                   total=len(rows), more=more,
                   empty=past or f"no live {BANKS[bank][1] + ' ' if bank else ''}offers in the site's {what}",
                   next=[OFFER, _merchant_step(shown)] if shown else [_first_page(ctx.action.name)] if past
                   else empty_steps)


@action(offerlove, tier=Tier.READ)
def latest(ctx, bank: Bank | None = None, sort: Literal["added", "expires"] = "added", offset: int = 0,
           limit: int = 30):
    """Newest offers first: the site's latest list (its 200 newest, usually the last few days).

    Rows already past their expiry date are left out. Columns: offer id, bank (merchant name with
    `--bank`), title and age (since it was added); `--fields` adds merchant, name, bank, expires
    and url.

    Args:
        bank: Only this bank's offers.
        sort: added (newest first) or expires (soonest expiry first, within the 200).
        offset: Rows to skip.
        limit: Rows to show (max 200).
    """
    by_expiry = sort == "expires"
    return _feed(ctx, "/latest/", "200 newest offers", bank, offset, limit, "expires" if by_expiry else "age",
                 [Next("bank", {"name": bank or "<bank>"}, "list a bank's offers instead", when_empty=True)],
                 "expires" if by_expiry else "site")


@action(offerlove, tier=Tier.READ)
def expiring(ctx, bank: Bank | None = None, offset: int = 0, limit: int = 30):
    """Offers expiring soonest: the site's expiring list (its 200 soonest within 7 days).

    The 200 are often all due the same day, so a longer horizon isn't reachable here; use
    `merchant` or `search` for a given merchant's dates. The site rebuilds the list a few times a
    day, so rows already past their expiry date are left out, and the header says how many; when
    none are left, `bank --name <bank> --sort expires` or `latest --sort expires` give the soonest
    of a sample.
    Columns: offer id, bank (merchant name with `--bank`), title and expiry date; `--fields` adds
    merchant, name, bank, due (the site's badge, such as `Expires today!`) and url.

    Args:
        bank: Only this bank's offers.
        offset: Rows to skip.
        limit: Rows to show (max 200).
    """
    return _feed(ctx, "/expiring/", "200 soonest-expiring offers", bank, offset, limit, "expires",
                 [Next("bank", {"name": bank or "<bank>", "sort": "expires"},
                       "list a bank's offers, soonest expiry first (its page's first 100)", when_empty=True),
                  Next("latest", {"bank": bank, "sort": "expires"},
                       "list the 200 newest offers, soonest expiry first", when_empty=True)])


@action(offerlove, tier=Tier.READ)
def offer(ctx, id: str):
    """One offer: merchant, bank, status, dates and its full terms.

    `expires` is the date in the id; `valid_through` is the site's own expiry value, a UTC instant
    or a bare date. When the terms give a date, that is the bank's own, and it can be a day later
    than `expires`; some terms give none ("can end at any time").

    Args:
        id: Offer id from any list (`walmart/spend-50-or-more-earn-10-back/2026-11-14`), or its page URL.
    """
    path = offer_id(id)
    o = offer_page(ctx.http.get_text(f"/offers/{path}/"))
    if o is None:
        raise NoSuchItem(f"offer.love has no offer {path!r} (it may have been removed, or the id is mistyped)")
    lines = [o["title"],
             f"merchant: {o['name']} ({o['merchant']})",
             f"bank: {o['bank']}",
             f"status: {o['status']}",
             f"expires: {o['expires']} (the site stores {o['valid_through']})",
             f"added: {o['added']}",
             f"url: {o['url']}",
             "",
             "terms:",
             o["terms"]]
    return Result(data=o, text="\n".join(lines), summary=f"{o['title']} — {o['name']} ({o['bank']})",
                  next=[Next("merchant", {"slug": o["merchant"]}, "list every active offer at this merchant")])


# My Offers names banks as its badges do; the CLI's bank values map onto those
_MY_BANK = {"amex": "Amex", "boa": "BoA", "chase": "Chase", "citi": "Citi", "paypal": "Paypal",
            "us-bank": "US Bank", "wells-fargo": "Wells Fargo"}


@action(offerlove, tier=Tier.READ, name="my-offers")
def my_offers(ctx, query: str | None = None, bank: Bank | None = None, card: str | None = None,
              added: Literal["yes", "no"] | None = None, sort: Sort = "site", offset: int = 0,
              limit: int = 50):
    """The signed-in user's own offers, every bank and card linked on Offer.love, from its My Offers page.

    Opens the page in a background browser tab, waits until the page has synced the offers, and reads
    them all from the site's own store in the page (the page itself shows 50 at a time). One row per
    offer and card. Offers past their expiry are left out (`hidden`). Columns: merchant, bank, card,
    title, expiry (a New York date, as the page prints it) and added (yes: on the card; no: still to
    add; not needed: the bank applies it without adding); `--fields` adds slug, started, status (the
    bank's own word) and terms. A table cell stops at 300 characters and gift card exclusions are
    often late in the terms, so read them with `--fields terms --json`, which has them whole. There
    is no offer id: the user's own expiry can differ from the public offer page's, so a public id
    built from it may not exist. Needs the user signed in to Offer.love in ESUM's browser.

    Args:
        query: Only offers whose merchant or title contains this (case and spaces ignored).
        bank: Only this bank's offers.
        card: Only cards whose name contains this, such as 8503 or Sapphire.
        added: yes for offers already on the card, no for ones still to add.
        sort: site (the page's order, by merchant) or expires (soonest first).
        offset: Rows to skip.
        limit: Rows to show.
    """
    page = ctx.page_script(MY_OFFERS, MY_OFFERS_SCRIPT, {"timeout_ms": MY_OFFERS_WAIT * 1000}) or {}
    if page.get("signed_out"):
        raise ctx.login_needed(detail="the My Offers page shows its sign-in button")
    if not page.get("signed_in"):
        raise PageChanged(f"the My Offers page showed neither its sign-in nor its sign-out button "
                          f"within {MY_OFFERS_WAIT} s")
    if page.get("offers") is None:
        raise PageChanged("the My Offers page has no offersData in its OFFER_LOVE_DB store")
    rows, expired = my_offer_rows(page)
    about: dict = {"url": MY_OFFERS}
    if page.get("header"):
        about["site_total"] = page["header"]
    if not page.get("synced"):
        about["note"] = (f"the page had not said 'Offers synced' after {MY_OFFERS_WAIT} s; "
                         "these are the offers it had stored, which may be out of date")
    if page.get("updated"):
        about["updated"] = page["updated"]
    if expired:
        about["hidden"] = f"{_rows(expired)} already past their expiry"
    if query:
        rows = [r for r in rows if matches(r, query)]
    if bank:
        rows = [r for r in rows if r["bank"].lower() == _MY_BANK[bank].lower()]
    if card:
        want = card.lower().replace(" ", "")
        rows = [r for r in rows if want in r["card"].lower().replace(" ", "")]
    if added:
        rows = [r for r in rows if (r["added"] == "no") == (added == "no")]
    rows = _sorted(rows, sort, about)
    shown, more = _page(rows, offset, limit)
    past = _past(rows, shown, offset)
    return Listing(name="offers", rows=shown, fields=("merchant", "bank", "card", "title", "expires", "added"),
                   about=about, total=len(rows), more=more,
                   empty=past or ("no offers of yours match" if (query or bank or card or added)
                                  else "Offer.love's My Offers lists no live offers for you"),
                   next=[*([_first_page("my-offers")] if past else []),
                         Next("my-offers", {"query": "<merchant>"},
                              "list only that merchant's offers of yours (add `--fields terms --json` for "
                              "the whole terms, where gift card exclusions usually are)"),
                         Next("search", {"query": "<merchant>"}, "list every bank's public offers at that merchant")])
