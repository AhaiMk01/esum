"""Offer.love parsers: the public JSON API's offer objects and the site's server-rendered HTML.

Every offer is addressed by its page path, `<merchant>/<title-slug>/<date>`, which every list on the
site links to. The date is the Pacific-time date of the offer's expiry instant: the site builds the
path that way, and the API's offers are turned into the same path here.
"""

from __future__ import annotations

import difflib
import html as htmllib
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from esum import ArgumentError, html_to_body, html_to_label, one_line, text_of

BASE = "https://offer.love"
PACIFIC = ZoneInfo("America/Los_Angeles")
# My Offers prints each expiry as a New York date
EASTERN = ZoneInfo("America/New_York")

# CLI bank value -> (the bank's page slug, the label the site prints on its badges)
BANKS = {
    "amex": ("amex", "Amex"),
    "boa": ("bank-of-america", "BoA"),
    "chase": ("chase", "Chase"),
    "citi": ("citi", "Citi"),
    "paypal": ("paypal", "Paypal"),
    "us-bank": ("us-bank", "US Bank"),
    "wells-fargo": ("wells-fargo", "Wells Fargo"),
}

_ID = re.compile(r"([a-z0-9][a-z0-9._-]*)/([a-z0-9-]+)/(\d{4}-\d\d-\d\d|n-a)")
_ROW = re.compile(r'<a href="/offers/([^"]+)/"[^>]*>\s*<div class="offer-row">(.*?)</a>', re.S)
_CARD = re.compile(r'<a href="/offers/([^"]+)/" class="offer-detail-link">(.*?)<div class="od-arrow">', re.S)


_MOJIBAKE = re.compile("[ÂÃ][-¿]")


def unmangle(text: str) -> str:
    """Undo UTF-8 text the site decoded as Windows-1252 once (`cafÃ©` -> `café`); else unchanged."""
    if not _MOJIBAKE.search(text):
        return text
    try:
        return text.encode("cp1252").decode("utf-8")
    except UnicodeError:
        return text


def _markup(fragment: str) -> str:
    """Terms HTML as the site meant it: some banks' terms arrive with their tags escaped once more
    (`&lt;br/&gt;`, Wells Fargo) and with stray byte-order marks (Amex)."""
    fragment = re.sub(r"&lt;(/?[a-zA-Z](?:(?!&gt;)[^<>])*)&gt;", r"<\1>", fragment)
    return fragment.replace("\ufeff", "")


def label(fragment: str) -> str:
    return unmangle(html_to_label(_markup(fragment), keep_links=False))


def offer_id(ref: str) -> str:
    """`walmart/spend-50-or-more-earn-10-back/2026-11-14` from that id, `/offers/…/` or the page URL."""
    raw = ref.strip()
    path = re.sub(r"^https?://(?:www\.)?offer\.love", "", raw, flags=re.I)
    path = re.sub(r"[?#].*$", "", path).strip("/")
    path = re.sub(r"^offers/", "", path)
    if not _ID.fullmatch(path):
        raise ArgumentError(f"not an offer id: {ref!r}",
                            help="an id looks like walmart/spend-50-or-more-earn-10-back/2026-11-14 "
                                 "(the path after offer.love/offers/); take one from a list's id column")
    return path


def merchant_slug(slug: str) -> str:
    """A merchant slug (`walmart`) from the slug, `/offers/walmart/` or the page URL."""
    s = re.sub(r"^https?://(?:www\.)?offer\.love", "", slug.strip(), flags=re.I)
    s = re.sub(r"^/?offers/", "", s).strip("/").lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", s):
        raise ArgumentError(f"not a merchant slug: {slug!r}",
                            help="a slug is the merchant column of a list or search row, such as walmart")
    return s


def _instant(instant: str | None) -> datetime | None:
    if not instant:
        return None
    try:
        when = datetime.fromisoformat(str(instant).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def pacific_date(instant: str | None) -> str | None:
    when = _instant(instant)
    return when.astimezone(PACIFIC).date().isoformat() if when else None


def slug_path(merchant: str, title: str, merchant_name: str, expires_at: str | None) -> str:
    """The page path the site links an API offer to: its title (or, past 100 characters, the merchant's
    name) lower-cased with everything but letters, digits and spaces dropped, then the Pacific date of
    the expiry instant (`n-a` without one)."""
    words = title if title and len(title) <= 100 else (merchant_name or "offer")
    slug = re.sub(r"\s+", "-", re.sub(r"[^a-z0-9\s]", "", words.lower()))
    return f"{merchant}/{slug}/{pacific_date(expires_at) or 'n-a'}"


def _expires(path: str) -> str | None:
    date = path.rsplit("/", 1)[-1]
    return None if date == "n-a" else date


def _url(path: str) -> str:
    return f"{BASE}/offers/{path}/"


def api_rows(data: dict) -> list[dict[str, Any]]:
    """Rows from an API list (`/v1/search`, `/v1/merchants/…`): `{"total", "per_page", "results": [...]}`.

    Offers that share a page (same merchant, title and expiry date) are one row, as on the site."""
    rows, seen = [], set()
    for o in data.get("results") or []:
        merchant = one_line(o.get("domainSld") or "")
        name = one_line(o.get("merchantName") or "")
        title = unmangle(one_line(o.get("title") or ""))
        path = slug_path(merchant, title, name, o.get("expiresAt"))
        if path in seen:
            continue
        seen.add(path)
        rows.append({"id": path, "merchant": merchant, "name": name, "title": title,
                     "bank": one_line(o.get("bank") or ""), "expires": _expires(path),
                     "added": (o.get("addedAt") or "")[:10] or None,
                     "terms": label(o.get("details") or ""),
                     "offer_id": o.get("offerId"), "url": _url(path)})
    return rows


def bank_counts(data: list) -> list[dict[str, Any]]:
    labels = {label.lower(): key for key, (_, label) in BANKS.items()}
    return [{"bank": labels.get(str(b.get("bank", "")).lower(), one_line(str(b.get("bank", "")))),
             "name": one_line(str(b.get("bank", ""))), "offers": int(b.get("count") or 0)}
            for b in data or []]


def _badge(fragment: str) -> str:
    m = re.search(r'<span class="badge[^"]*">(.*?)</span>', fragment, re.S)
    return text_of(m.group(1)) if m else ""


def list_rows(page: str, merge: bool = True) -> list[dict[str, Any]]:
    """Offer rows of the latest, expiring and bank pages, first occurrence of each id kept.

    A latest row's line under the title is `Merchant — 16h ago`, a bank row's `Merchant — terms…`,
    an expiring row's just the merchant; its badge says when it expires.
    """
    rows, seen = [], set()
    for m in _ROW.finditer(page):
        path, inner = m.group(1), m.group(2)
        if not _ID.fullmatch(path) or (merge and path in seen):
            continue
        seen.add(path)
        alt = re.search(r'<img[^>]*\balt="([^"]*)"', inner)
        title = re.search(r"<h4>(.*?)</h4>", inner, re.S)
        line = re.search(r"<p>(.*?)</p>", inner, re.S)
        name = unmangle(text_of(alt.group(1))) if alt else ""
        rest = ""
        if line:
            parts = line.group(1).split("&mdash;", 1)
            rest = parts[1] if len(parts) > 1 else ""
        rest_text = unmangle(text_of(rest))
        due = re.search(r'class="(?:exp-badge|sr-exp)">(.*?)<', inner)
        row = {"id": path, "merchant": path.split("/", 1)[0], "name": name,
               "title": label(title.group(1)) if title else "",
               "bank": _badge(inner), "expires": _expires(path), "url": _url(path)}
        if re.fullmatch(r"\d+[mhdwy] ago|just now", rest_text):
            row["age"] = rest_text
        elif rest_text:
            row["excerpt"] = rest_text
        if due:
            row["due"] = text_of(due.group(1))
        rows.append(row)
    return rows


def today() -> str:
    """Today's date in Los Angeles, the zone of the dates in offer ids."""
    return datetime.now(PACIFIC).date().isoformat()


def split_expired(rows: list[dict[str, Any]], on: str) -> tuple[list[dict[str, Any]], int]:
    """`(rows still live on date `on`, number of rows already past their expiry)`.

    An id's date is the Pacific date of the expiry instant, so a date before `on` (also Pacific)
    is certainly past; a row dated `on` itself may still be live and is kept.
    """
    kept = [r for r in rows if not r.get("expires") or r["expires"] >= on]
    return kept, len(rows) - len(kept)


def merge(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first row for each id, in order."""
    seen: set[str] = set()
    return [r for r in rows if not (r["id"] in seen or seen.add(r["id"]))]


def row_count(page: str) -> int:
    """Offer rows on a list page before repeated pages are merged (the bank page shows 100)."""
    return len(_ROW.findall(page))


def page_count(page: str) -> tuple[int | None, int | None]:
    """`(offers, merchants)` from a bank page's `1926 active offers from 1672 merchants`."""
    m = re.search(r"(\d+) active offers from (\d+) merchants", page)
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def heading(page: str) -> str:
    m = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    return text_of(m.group(1)) if m else ""


def merchant_page(page: str) -> tuple[str, list[dict[str, Any]]] | None:
    """`(merchant name, offer rows)` of `/offers/<slug>/`, or None when the site has no such merchant
    (it answers 200 with `No offers found for "<slug>"`)."""
    title = heading(page)
    if title.startswith("No offers found"):
        return None
    name = re.sub(r"\s+Offers$", "", title)
    past = re.search(r"<h2[^>]*>Past Offers</h2>", page)
    rows = []
    for m in _CARD.finditer(page):
        path, inner = m.group(1), m.group(2)
        if not _ID.fullmatch(path):
            continue
        h3 = re.search(r"<h3>(.*?)</h3>", inner, re.S)
        alt = re.search(r'<img[^>]*\balt="([^"]*)"', inner)
        added = re.search(r"Added ([^<]*?)</span>", inner)
        terms = re.search(r'<p class="od-details">(.*?)</p>\s*<div class="od-cards">', inner, re.S) \
            or re.search(r'<p class="od-details">(.*)</p>', inner, re.S)
        rows.append({"id": path, "merchant": path.split("/", 1)[0],
                     "name": text_of(alt.group(1)) if alt else name,
                     "title": label(h3.group(1)) if h3 else "",
                     "bank": _badge(inner), "expires": _expires(path),
                     "status": "expired" if past and m.start() > past.start() else "active",
                     "age": text_of(added.group(1)) if added else None,
                     "terms": label(terms.group(1)) if terms else "",
                     "url": _url(path)})
    return name, rows


def _ld(page: str, kind: str) -> dict | None:
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == kind:
            return data
    return None


def _dd(page: str, label: str) -> str | None:
    m = re.search(rf"<dt>{label}:</dt>\s*<dd[^>]*>(.*?)</dd>", page, re.S)
    return m.group(1) if m else None


def offer_page(page: str) -> dict[str, Any] | None:
    """One offer from its page, or None when the site says `Offer not found` (it answers 200)."""
    if heading(page) == "Offer not found":
        return None
    ld = _ld(page, "Offer")
    if ld is None:
        raise RuntimeError("offer page without its schema.org Offer data (the site layout may have changed)")
    url = ld.get("url") or ""
    path = offer_id(url) if url else None
    merchant_link = re.search(r'<dt>Merchant:</dt>\s*<dd><a href="/offers/([^/"]+)/"', page)
    bank = _dd(page, "Bank")
    added = _dd(page, "Added")
    status = "active" if str(ld.get("availability", "")).endswith("InStock") else "expired"
    instant = ld.get("validThrough")
    return {"id": path, "title": unmangle(text_of(htmllib.unescape(ld.get("name") or ""))),
            "merchant": merchant_link.group(1) if merchant_link else (path or "").split("/", 1)[0],
            "name": text_of((ld.get("seller") or {}).get("name") or ""),
            "bank": text_of(bank) if bank else "", "status": status,
            "expires": _expires(path) if path else pacific_date(instant), "valid_through": instant,
            "added": _added(text_of(added)) if added else None,
            "terms": unmangle(html_to_body(_markup(ld.get("description") or ""), keep_links=True)), "url": url}


def _added(text: str) -> str:
    try:
        return datetime.strptime(text, "%B %d, %Y").date().isoformat()
    except ValueError:
        return text


class PageChanged(RuntimeError):
    """A page no longer looks the way its parser or page script expects. Never carries page content."""


# What each bank's status says about the offer being on the card (My Offers keeps the bank's own
# words): Amex enrolls, Chase activates, BoA either activates or needs nothing.
_ADDED = {"ENROLLED": "yes", "ACTIVATED": "yes", "NO_ACTIVATION_REQUIRED": "not needed",
          "ELIGIBLE": "no", "NEW": "no", "SERVED": "no"}


def _card(o: dict) -> str:
    """The card as My Offers names it (`Sapphire Preferred (...8503)`); Amex rows carry only the
    card number's last digits."""
    if o.get("accountLabel"):
        return one_line(str(o["accountLabel"]))
    number = str(o.get("cardNumber") or "")
    return f"{o.get('bank') or 'card'} (...{number})" if number else ""


def my_offer_rows(page: dict, now: datetime | None = None) -> tuple[list[dict[str, Any]], int]:
    """`(one row per offer and card that has not expired by `now`, number left out as expired)`,
    from what `myoffers.js` read out of the My Offers store, in the store's order (by merchant).
    Dates are New York dates, as the page prints them."""
    now = now or datetime.now(timezone.utc)
    rows, expired = [], 0
    for o in page.get("offers") or []:
        ends = _instant(o.get("expiresAt"))
        if ends and ends < now:
            expired += 1
            continue
        started = _instant(o.get("startedAt"))
        status = str(o.get("enrollmentStatus") or o.get("offerStatus") or "")
        rows.append({
            "merchant": one_line(unmangle(str(o.get("merchantName") or o.get("merchant") or ""))),
            "slug": str(o.get("merchant") or ""),
            "bank": str(o.get("bank") or ""),
            "card": _card(o),
            "title": one_line(unmangle(str(o.get("title") or ""))),
            "expires": ends.astimezone(EASTERN).date().isoformat() if ends else None,
            "started": started.astimezone(EASTERN).date().isoformat() if started else None,
            "added": _ADDED.get(status, ""),
            "status": status,
            "terms": unmangle(html_to_body(_markup(str(o.get("details") or "")), keep_links=False)),
        })
    return rows, expired


def matches(row: dict[str, Any], query: str) -> bool:
    """Whether `query` is in the row's merchant name, slug or title, ignoring case and spaces."""
    want = re.sub(r"\s+", "", query.lower())
    return any(want in re.sub(r"\s+", "", str(row.get(k) or "").lower()) for k in ("merchant", "slug", "title"))


_DIRECTORY = re.compile(r"\bconst\s+ALL\s*=\s*")


def plain(text: str) -> str:
    """Letters and digits only, lower case, accents dropped: `P.F. Chang's` and `pf changs`
    compare equal, and so do `Âu Lạc` and `au lac`."""
    bare = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", "", bare.casefold())


def _count(value: Any) -> int | None:
    """An offer count as a number; None for anything else (`true`, a name, nothing)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return int(value) if isinstance(value, str) and value.strip().isdigit() else None


def directory(page: str) -> list[dict[str, Any]]:
    """The Offer Directory's merchants, from the list the page embeds for its own filter box
    (`const ALL=[[slug, name, logo, offers], …]`, the same on each of its pages), most offers
    first. PageChanged when the list is missing, isn't JSON, has no merchant or has no counts."""
    found = _DIRECTORY.search(page)
    if not found:
        raise PageChanged("the Offer Directory page has no merchant list (`const ALL=`)")
    try:
        data, _ = json.JSONDecoder().raw_decode(page, found.end())
    except ValueError:
        raise PageChanged("the Offer Directory page's merchant list is not JSON") from None
    rows = [{"merchant": item[0], "name": unmangle(one_line(item[1] if isinstance(item[1], str) else "")),
             "offers": _count(item[3]), "url": f"{BASE}/offers/{item[0]}/"}
            for item in (data if isinstance(data, list) else [])
            if isinstance(item, list) and len(item) >= 4 and isinstance(item[0], str) and item[0]]
    if not rows:
        raise PageChanged("the Offer Directory page's merchant list is empty")
    if all(r["offers"] is None for r in rows):
        raise PageChanged("the Offer Directory page's merchant list has no offer counts")
    # the site lists them most offers first; sort anyway, keeping its order among equals
    return sorted(rows, key=lambda r: -(r["offers"] or 0))


def in_directory(row: dict[str, Any], query: str) -> bool:
    """Whether every word of `query` is in the merchant's name or slug, ignoring case, spaces,
    punctuation and accents."""
    name, slug = plain(row["name"]), plain(row["merchant"])
    words = [w for w in (plain(part) for part in query.split()) if w]
    return bool(words) and all(w in name or w in slug for w in words)


def nearest_merchants(rows: list[dict[str, Any]], query: str, n: int = 3) -> list[dict[str, Any]]:
    """The merchants whose name or slug is nearly what was typed, nearest first: for a query that
    matched none."""
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        by_key.setdefault(plain(row["name"]), row)
        by_key.setdefault(plain(row["merchant"]), row)
    near = difflib.get_close_matches(plain(query), [k for k in by_key if k], n=2 * n, cutoff=0.75)
    return list({by_key[k]["merchant"]: by_key[k] for k in near}.values())[:n]
