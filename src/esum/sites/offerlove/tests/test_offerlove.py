"""Offline tests for Offer.love on saved API answers and pages (offer.love, data.offer.love/v1)."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from esum import ArgumentError, Options
from esum.core.registry import Registry
from esum.sites.offerlove.parse import (api_rows, bank_counts, list_rows, merchant_page, merchant_slug, offer_id,
                                        offer_page, page_count, slug_path, split_expired, unmangle)
from esum.transport import Response

from conftest import json_response

FIX = Path(__file__).parent / "fixtures"
API = "https://data.offer.love/v1"


def text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def html_response(url: str, name: str) -> Response:
    return Response(200, url, {"content-type": "text/html;charset=UTF-8"}, text(name).encode("utf-8"))


# --- ids and slugs ---------------------------------------------------------------------------------

def test_offer_id_accepts_the_path_the_offers_path_and_the_page_url():
    want = "walmart/spend-50-or-more-earn-10-back/2026-11-14"
    assert offer_id(want) == want
    assert offer_id(f"/offers/{want}/") == want
    assert offer_id(f"https://offer.love/offers/{want}/?ref=x") == want


@pytest.mark.parametrize("bad", ["foo", "walmart", "walmart/spend", "walmart/Spend/2026-11-14", "a/b/2026-1-1"])
def test_offer_id_rejects_anything_else(bad):
    with pytest.raises(ArgumentError):
        offer_id(bad)


def test_merchant_slug_from_url_and_case():
    assert merchant_slug("https://offer.love/offers/walmart/") == "walmart"
    assert merchant_slug("Walmart") == "walmart"
    with pytest.raises(ArgumentError):
        merchant_slug("wal mart")


def test_slug_path_is_the_title_and_the_pacific_date_of_the_expiry():
    # 2026-11-30T05:00Z is still Nov 29 in Los Angeles; the site links the offer under that date
    assert slug_path("walmart", "Earn $5 Back on a purchase of $75 or more.", "Walmart",
                     "2026-11-30T05:00:00.000Z") == "walmart/earn-5-back-on-a-purchase-of-75-or-more/2026-11-29"
    long = "x" * 101
    assert slug_path("lids", long, "Lids Store", None) == "lids/lids-store/n-a"


# --- API -------------------------------------------------------------------------------------------

def test_api_rows_map_offers_to_their_pages_and_merge_shared_pages():
    data = json.loads(text("search_walmart.json"))
    rows = api_rows(data)
    assert len(data["results"]) == 7 and len(rows) == 6   # two Citi offers share one page
    first = rows[0]
    assert first["id"] == "walmart/earn-5-back-on-a-purchase-of-75-or-more/2026-11-29"
    assert (first["merchant"], first["bank"], first["expires"], first["added"]) == ("walmart", "Citi", "2026-11-29",
                                                                                 "2026-11-02")
    assert first["offer_id"] == "citi:2cd4ece4-ec18-4d53-a8fe-b72aa1cf05d2"
    amex = rows[1]
    assert amex["id"] == "walmart/spend-50-or-more-earn-10-back/2026-11-14" and amex["name"] == "Walmart.com"
    assert "<" not in amex["terms"] and amex["terms"].startswith("Earn a one-time $10")


def test_bank_counts_use_cli_names():
    rows = bank_counts(json.loads(text("banks.json")))
    assert rows[0] == {"bank": "boa", "name": "BoA", "offers": 4157}
    assert [r["bank"] for r in rows] == ["boa", "chase", "citi", "amex", "us-bank", "wells-fargo"]


# --- pages -----------------------------------------------------------------------------------------

def test_latest_rows_have_age_and_drop_repeated_pages():
    rows = list_rows(text("latest.html"))
    assert len(rows) == 6 and len({r["id"] for r in rows}) == 6
    first = rows[0]
    assert first["id"] == "thebreakfastclub/earn-5-cash-back-on-your-the-breakfast-club-purchases/2026-12-01"
    assert (first["bank"], first["name"], first["age"], first["expires"]) == ("US Bank", "The Breakfast Club",
                                                                              "16h ago", "2026-12-01")
    assert rows[1]["title"] == "Alana's Cafe - (up to $10)"


def test_expiring_rows_carry_the_due_badge():
    rows = list_rows(text("expiring.html"))
    assert rows[0]["id"] == "moma/spend-150-or-more-earn-15-back/2026-11-08"
    assert (rows[0]["bank"], rows[0]["name"], rows[0]["due"]) == ("Amex", "MoMA", "Expires today!")
    assert "age" not in rows[0]


def test_bank_page_rows_and_site_total():
    rows = list_rows(text("bank_chase.html"))
    assert rows[0]["id"] == "spartataverna/5-cash-back-get-up-to-100-back/2026-12-04"
    assert rows[0]["excerpt"].startswith("Earn 5% cash back on all of your Sparta Taverna purchases")
    assert rows[1]["name"] == "Mambo Latín Kitchen & Empanadas"
    assert page_count(text("bank_chase.html")) == (1931, 1677)


def test_merchant_page_marks_past_offers():
    name, rows = merchant_page(text("merchant_walmart.html"))
    assert name == "Walmart" and len(rows) == 11
    assert [r["status"] for r in rows].count("active") == 6
    assert (rows[0]["id"], rows[0]["bank"], rows[0]["age"]) == (
        "walmart/earn-5-back-on-a-purchase-of-75-or-more/2026-11-29", "Citi", "7d ago")
    assert rows[6]["id"] == "walmart/earn-10-back-on-a-purchase-of-75-or-more/2026-10-27"
    assert rows[6]["status"] == "expired"
    assert rows[0]["terms"].startswith("Shop Walmart.com for Every Day Low Prices")


def test_unknown_merchant_page_is_none():
    assert merchant_page(text("merchant_missing.html")) is None


def test_offer_page_active_and_expired():
    o = offer_page(text("offer_walmart.html"))
    assert o["id"] == "walmart/spend-50-or-more-earn-10-back/2026-11-14"
    assert (o["merchant"], o["name"], o["bank"], o["status"]) == ("walmart", "Walmart.com", "Amex", "active")
    assert (o["expires"], o["valid_through"], o["added"]) == ("2026-11-14", "2026-11-15T00:00:00.000Z", "2026-10-15")
    assert "by 11/15/2026" in o["terms"] and "<br>" not in o["terms"]
    gone = offer_page(text("offer_expired.html"))
    assert (gone["status"], gone["bank"], gone["merchant"]) == ("expired", "BoA", "chevron")
    assert "Location: 4000 S Figueroa St" in gone["terms"]


def test_missing_offer_page_is_none():
    assert offer_page(text("offer_missing.html")) is None


# --- whole actions ---------------------------------------------------------------------------------

@pytest.fixture
def offerlove(monkeypatch):
    import esum.sites.offerlove.parse as parse
    monkeypatch.setattr(parse, "today", lambda: "2026-11-08")   # the day the fixtures were live
    return Registry.discover().get("offerlove")


def run(action_runner, target, name, kwargs, json_out=True):
    output = io.StringIO()
    action_runner(target, target.all_actions[name], kwargs, Options(json=json_out), stream=output)
    return json.loads(output.getvalue())["data"] if json_out else output.getvalue()


def test_search_action_notes_the_api_cap(action_runner, fake_http, offerlove):
    url = f"{API}/search?q=walmart"
    data = json.loads(text("search_walmart.json"))
    data["results"] = data["results"] * 2   # ten or more answers: the API's per-call cap
    data["results"] = [dict(r, title=f"{r['title']} {i}") for i, r in enumerate(data["results"])][:10]
    fake_http.routes[url] = json_response(url, data)
    out = run(action_runner, offerlove, "search", {"query": "walmart"})
    assert len(out["offers"]) == 10 and "at most 10" in out["note"]


def test_merchant_action_hides_past_offers_unless_asked(action_runner, fake_http, offerlove):
    url = "https://offer.love/offers/walmart/"
    fake_http.routes[url] = html_response(url, "merchant_walmart.html")
    active = run(action_runner, offerlove, "merchant", {"slug": "walmart"})
    assert len(active["offers"]) == 6
    every = run(action_runner, offerlove, "merchant", {"slug": "walmart", "past": True})
    assert len(every["offers"]) == 11
    amex = run(action_runner, offerlove, "merchant", {"slug": "walmart", "bank": "amex"})
    assert {r["bank"] for r in amex["offers"]} == {"Amex"} and len(amex["offers"]) == 4


def test_unknown_merchant_is_an_empty_list_naming_search(action_runner, fake_http, offerlove):
    url = "https://offer.love/offers/nosuchmerchantxyz/"
    fake_http.routes[url] = html_response(url, "merchant_missing.html")
    out = run(action_runner, offerlove, "merchant", {"slug": "nosuchmerchantxyz"}, json_out=False)
    assert "no merchant with the slug 'nosuchmerchantxyz'" in out
    assert "esum offerlove search --query nosuchmerchantxyz" in out
    assert "esum offerlove merchants --query nosuchmerchantxyz" in out


def test_latest_action_filters_by_bank_and_pages(action_runner, fake_http, offerlove):
    url = "https://offer.love/latest/"
    fake_http.routes[url] = html_response(url, "latest.html")
    boa = run(action_runner, offerlove, "latest", {"bank": "boa"})
    assert boa["offers"] and {r["bank"] for r in boa["offers"]} == {"BoA"}
    page = run(action_runner, offerlove, "latest", {"limit": 2}, json_out=False)
    assert "esum offerlove latest --offset 2 --limit 2" in page


def test_bank_action_reports_the_site_total(action_runner, fake_http, offerlove):
    url = "https://offer.love/offers/bank/chase/"
    fake_http.routes[url] = html_response(url, "bank_chase.html")
    out = run(action_runner, offerlove, "bank", {"name": "chase"})
    assert out["site_total"] == "1931 active offers from 1677 merchants; the page shows only its first 8"
    assert len(out["offers"]) == 8
    text = run(action_runner, offerlove, "bank", {"name": "chase", "limit": 2}, json_out=False)
    assert "offers[2]{id,name,title,expires}:" in text   # one bank: the merchant's name, not the bank


def test_offer_action_missing_and_malformed(action_runner, fake_http, offerlove, capsys):
    url = "https://offer.love/offers/walmart/nosuch/2026-11-29/"
    fake_http.routes[url] = html_response(url, "offer_missing.html")
    registry = Registry.discover()
    assert action_runner.cli(["offerlove", "offer", "--id", "walmart/nosuch/2026-11-29"], registry=registry) == 1
    assert "has no offer 'walmart/nosuch/2026-11-29'" in capsys.readouterr().out
    assert action_runner.cli(["offerlove", "offer", "--id", "foo"], registry=registry) == 2
    assert "not an offer id" in capsys.readouterr().out
    assert not [r for r in fake_http.requests if "foo" in r.url]


def test_offer_action_prints_terms_and_merchant_step(action_runner, fake_http, offerlove):
    url = "https://offer.love/offers/walmart/spend-50-or-more-earn-10-back/2026-11-14/"
    fake_http.routes[url] = html_response(url, "offer_walmart.html")
    out = run(action_runner, offerlove, "offer", {"id": "walmart/spend-50-or-more-earn-10-back/2026-11-14"},
              json_out=False)
    assert "status: active" in out and "expires: 2026-11-14 (the site stores 2026-11-15T00:00:00.000Z)" in out
    assert "esum offerlove merchant --slug walmart" in out


def test_search_names_the_merchant_when_every_row_is_one_merchants(action_runner, fake_http, offerlove):
    url = f"{API}/search?q=walmart"
    fake_http.routes[url] = json_response(url, json.loads(text("search_walmart.json")))
    out = run(action_runner, offerlove, "search", {"query": "walmart"}, json_out=False)
    assert "esum offerlove merchant --slug walmart" in out and "<merchant>" not in out
    assert "at most 10" not in out   # 7 answers: under the cap


def test_split_expired_drops_only_rows_dated_before_today():
    rows = [{"id": "a/x/2026-11-07", "expires": "2026-11-07"}, {"id": "b/x/2026-11-08", "expires": "2026-11-08"},
            {"id": "c/x/2026-12-01", "expires": "2026-12-01"}, {"id": "d/x/n-a", "expires": None}]
    kept, hidden = split_expired(rows, "2026-11-08")
    assert [r["id"] for r in kept] == ["b/x/2026-11-08", "c/x/2026-12-01", "d/x/n-a"] and hidden == 1


def test_stale_expiring_list_says_what_it_hid(action_runner, fake_http, offerlove, monkeypatch):
    import esum.sites.offerlove.parse as parse
    monkeypatch.setattr(parse, "today", lambda: "2026-11-09")   # the day after: the list is stale
    url = "https://offer.love/expiring/"
    fake_http.routes[url] = html_response(url, "expiring.html")
    out = run(action_runner, offerlove, "expiring", {}, json_out=False)
    assert "7 rows already past their expiry date" in out and "0 found" in out
    assert "esum offerlove bank --name <bank>" in out   # an empty list still names a next step
    chase = run(action_runner, offerlove, "expiring", {"bank": "chase"}, json_out=False)
    assert "hidden: 4 rows already" in chase   # counted within the bank asked for, not the whole list
    assert "esum offerlove bank --name chase" in chase


def test_unmangle_repairs_double_encoded_text_only():
    assert unmangle("relaxed cafÃ© atmosphere") == "relaxed café atmosphere"
    assert unmangle("Mambo Latín Kitchen") == "Mambo Latín Kitchen"   # already right: unchanged
    assert unmangle("Ã alone") == "Ã alone"


def test_bank_action_says_when_repeats_were_merged(action_runner, fake_http, offerlove):
    url = "https://offer.love/offers/bank/chase/"
    page = text("bank_chase.html")
    first = page.index('<a href="/offers/spartataverna/')
    end = page.index("</a>", first) + 4
    page = page[:first] + page[first:end] * 2 + page[end:]   # the site repeats a row
    fake_http.routes[url] = Response(200, url, {"content-type": "text/html"}, page.encode("utf-8"))
    out = run(action_runner, offerlove, "bank", {"name": "chase"})
    assert out["merged"] == "1 row repeats another row's page" and len(out["offers"]) == 8
    assert out["site_total"].endswith("its first 9")


def test_empty_search_points_at_the_merchant_page(action_runner, fake_http, offerlove):
    url = f"{API}/search?q=star+bucks"
    fake_http.routes[url] = json_response(url, {"total": 0, "per_page": 10, "results": []})
    out = run(action_runner, offerlove, "search", {"query": "star bucks"}, json_out=False)
    assert "no active offer matches" in out
    assert "esum offerlove merchant --slug starbucks`" in out and "misses some offers" in out


def test_search_says_when_offers_shared_a_page(action_runner, fake_http, offerlove):
    url = f"{API}/search?q=walmart"
    fake_http.routes[url] = json_response(url, json.loads(text("search_walmart.json")))
    out = run(action_runner, offerlove, "search", {"query": "walmart"})
    assert out["merged"].startswith("1 offers share another offer's page")


def test_terms_with_escaped_tags_and_byte_order_marks_come_out_clean():
    # Wells Fargo terms escape their tags once more; Amex terms carry stray BOMs
    raw = ("<div>Earn 10% cash back, with a $4.00 maximum.&lt;br/&gt;&lt;br/&gt;Welcome to Moe&#039;s! "
           "&lt;a class='cardlytics_anchor_styling' href='https://example.com/terms?r=1&amp;xt=2'&gt;Terms&lt;/a&gt;</div>"
           "affiliates.﻿ More")
    page = ('<h1>x</h1><script type="application/ld+json">'
            + json.dumps({"@type": "Offer", "name": "Moe's 10%", "description": raw,
                          "url": "https://offer.love/offers/moes/moes-10/2026-11-29/",
                          "availability": "https://schema.org/InStock"})
            + "</script>")
    terms = offer_page(page)["terms"]
    assert "<" not in terms and "﻿" not in terms and "&lt;" not in terms
    assert "Welcome to Moe's!" in terms and "https://example.com/terms" in terms


def test_latest_merges_repeats_per_bank_and_sorts_by_expiry(action_runner, fake_http, offerlove):
    url = "https://offer.love/latest/"
    fake_http.routes[url] = html_response(url, "latest.html")
    out = run(action_runner, offerlove, "latest", {"bank": "boa", "sort": "expires"})
    assert out["merged"].startswith("2 rows repeat") and out["sort"] == "soonest expiry first"
    dates = [r["expires"] for r in out["offers"]]
    assert dates == sorted(dates)
    url = "https://offer.love/offers/bank/chase/"
    fake_http.routes[url] = html_response(url, "bank_chase.html")
    chase = [r["expires"] for r in run(action_runner, offerlove, "bank", {"name": "chase", "sort": "expires"})["offers"]]
    assert chase == sorted(chase) and chase[0] == "2026-11-18"


def test_empty_expiring_names_a_sorted_bank_list(action_runner, fake_http, offerlove, monkeypatch):
    import esum.sites.offerlove.parse as parse
    monkeypatch.setattr(parse, "today", lambda: "2026-11-09")
    url = "https://offer.love/expiring/"
    fake_http.routes[url] = html_response(url, "expiring.html")
    out = run(action_runner, offerlove, "expiring", {"bank": "amex"}, json_out=False)
    assert "esum offerlove bank --name amex --sort expires` to list a bank's offers" in out
    assert "esum offerlove latest --bank amex --sort expires" in out


def test_search_with_rows_names_the_merchant_once(action_runner, fake_http, offerlove):
    url = f"{API}/search?q=walmart"
    fake_http.routes[url] = json_response(url, json.loads(text("search_walmart.json")))
    out = run(action_runner, offerlove, "search", {"query": "walmart"}, json_out=False)
    assert out.count("esum offerlove merchant --slug walmart") == 1


def test_the_site_keeps_no_key_and_has_no_login():
    # the API-key actions (login, logout) were removed: that API could only search, 50 at most;
    # my-offers reads the signed-in page in the browser instead
    target = Registry.discover().get("offerlove")
    assert sorted(target.all_actions) == ["bank", "banks", "expiring", "latest", "merchant", "merchants",
                                          "my-offers", "offer", "search"]
    assert "key_entry" not in target.extras


# --- my-offers (page script) -----------------------------------------------------------------------

class MyOffersTab:
    def __init__(self, session, url):
        self.session, self.url, self.closed = session, url, False

    def evaluate(self, fn, arg=None):
        self.session.scripts.append((fn, arg))
        return self.session.page

    def close(self):
        self.closed = True


class Browser:
    """ESUM's browser session as a page script sees it: tabs opened on a URL, run in, closed."""

    def __init__(self, page: dict):
        self.page, self.tabs, self.scripts = page, [], []
        self.where = "ESUM's browser (managed)"

    def open_tab(self, url, cleared=None, *, site=""):
        self.tabs.append(MyOffersTab(self, url))
        return self.tabs[-1]


NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


def my_offers(action_runner, target, browser, json_out=True, **kwargs):
    output = io.StringIO()
    action_runner(target, target.all_actions["my-offers"], kwargs, Options(json=json_out), stream=output,
                  session=browser)
    return json.loads(output.getvalue())["data"] if json_out else output.getvalue()


@pytest.fixture
def frozen(monkeypatch):
    import esum.sites.offerlove.parse as parse
    real = parse.my_offer_rows
    monkeypatch.setattr("esum.sites.offerlove.my_offer_rows", lambda page, now=None: real(page, NOW))


def test_my_offer_rows_name_the_card_say_whether_it_is_added_and_drop_expired_offers():
    from esum.sites.offerlove.parse import my_offer_rows
    rows, expired = my_offer_rows(json.loads(text("my_offers.json")), NOW)
    assert expired == 1 and len(rows) == 5
    assert [(r["bank"], r["card"], r["added"]) for r in rows] == [
        ("Amex", "Amex (...11111)", "yes"), ("Amex", "Amex (...22222)", "no"),
        ("Chase", "Sapphire Preferred (...3333)", "yes"), ("Chase", "Sapphire Preferred (...3333)", "no"),
        ("BoA", "Customized Cash Rewards (...4444)", "not needed")]
    # the page prints New York dates: 04:59 UTC on the 14th is still the 13th there
    assert rows[0]["expires"] == "2026-12-13" and rows[0]["started"] == "2026-11-01"
    assert rows[0]["terms"].startswith("Get a one-time $50 statement credit") and "<b>" not in rows[0]["terms"]
    assert rows[4]["merchant"] == "Café X" and rows[4]["slug"] == "cafe-x"


def test_my_offers_reads_the_page_store_in_a_background_tab_and_filters(action_runner, offerlove, frozen):
    browser = Browser(json.loads(text("my_offers.json")))
    data = my_offers(action_runner, offerlove, browser, query="LOWE S")
    assert [(t.url, t.closed) for t in browser.tabs] == [("https://offer.love/myoffers/", True)]
    assert browser.scripts[0][1] == {"timeout_ms": 45000} and "OFFER_LOVE_DB" in browser.scripts[0][0]
    assert [r["card"] for r in data["offers"]] == ["Amex (...11111)", "Amex (...22222)", "Sapphire Preferred (...3333)"]
    assert data["site_total"] == "6 offers from 4 merchants" and "1 row already past" in data["hidden"]
    data = my_offers(action_runner, offerlove, Browser(json.loads(text("my_offers.json"))), added="no", sort="expires")
    assert [r["merchant"] for r in data["offers"]] == ["Staples", "Lowe's"]
    data = my_offers(action_runner, offerlove, Browser(json.loads(text("my_offers.json"))), bank="boa", card="4444")
    assert [r["merchant"] for r in data["offers"]] == ["Café X"]
    out = my_offers(action_runner, offerlove, Browser(json.loads(text("my_offers.json"))), json_out=False, bank="chase")
    assert "offers[2]{merchant,bank,card,title,expires,added}:" in out and "Sapphire Preferred (...3333)" in out


def test_my_offers_says_login_needed_only_when_the_page_shows_its_sign_in_button(action_runner, offerlove):
    from esum import LoginNeeded
    from esum.sites.offerlove.parse import PageChanged
    out = {"signed_in": False, "signed_out": True, "synced": False, "header": "", "offers": None}
    with pytest.raises(LoginNeeded):
        my_offers(action_runner, offerlove, Browser(out))
    # a page showing neither button (a check page, a redesign) is not a sign-out
    with pytest.raises(PageChanged, match="neither its sign-in nor its sign-out"):
        my_offers(action_runner, offerlove, Browser(dict(out, signed_out=False)))
    with pytest.raises(PageChanged, match="no offersData"):
        my_offers(action_runner, offerlove, Browser(dict(out, signed_in=True, signed_out=False)))


def test_my_offers_notes_an_unsynced_page(action_runner, offerlove, frozen):
    page = dict(json.loads(text("my_offers.json")), synced=False)
    assert "had not said 'Offers synced'" in my_offers(action_runner, offerlove, Browser(page))["note"]


# --- the merchant directory ------------------------------------------------------------------------
# directory.html is the Offer Directory page of 2026-11-18 cut to its heading, three cards and 16 of
# the 5,894 merchants its filter list held. The page showed "P.F. Chang's 57 offers", "Sprouts
# Farmers Market 51 offers", "Shell 32 offers", "76 26 offers" … "The 13 offers".

def test_directory_rows_read_as_the_page_lists_them():
    from esum.sites.offerlove.parse import directory
    rows = directory(text("directory.html"))
    assert [(r["merchant"], r["name"], r["offers"]) for r in rows[:5]] == [
        ("pfchangs", "P.F. Chang's", 57), ("sproutsfarmersmarket", "Sprouts Farmers Market", 51),
        ("shell", "Shell", 32), ("76", "76", 26), ("sprouts", "Sprouts", 24)]
    assert rows[7] == {"merchant": "jerseymikes", "name": "Jersey Mike's Subs - Altamonte Springs Cranes Roost",
                       "offers": 20, "url": "https://offer.love/offers/jerseymikes/"}
    assert [(r["name"], r["offers"]) for r in rows if r["merchant"] == "the"] == [("The", 13)]
    assert len(rows) == 16 and next(r for r in rows if r["merchant"] == "ulcla")["name"] == "Âu Lạc LA"


def test_directory_query_ignores_case_spaces_and_punctuation():
    from esum.sites.offerlove.parse import directory, in_directory, nearest_merchants
    rows = directory(text("directory.html"))

    def found(query):
        return [r["merchant"] for r in rows if in_directory(r, query)]

    assert found("pf changs") == ["pfchangs"] and found("P.F. CHANG'S") == ["pfchangs"]
    assert found("bagel") == ["ess-a-bagel", "marysbagelcafe", "zuckersbagelschelsea"]
    assert found("bagel chelsea") == ["zuckersbagelschelsea"]
    assert found("sprouts") == ["sproutsfarmersmarket", "sprouts"] and found("giftcards.com") == ["giftcards.com"]
    assert found("  ") == [] and found("zzz") == []
    assert [r["merchant"] for r in nearest_merchants(rows, "walmrt")] == ["walmart"]
    assert nearest_merchants(rows, "qqqq") == []


def test_a_directory_page_without_its_list_is_a_layout_failure():
    from esum.sites.offerlove.parse import PageChanged, directory
    for page in ("<html><body>Offer Directory</body></html>", "<script>const ALL=[oops];</script>",
                 "<script>const ALL=[];</script>", '<script>const ALL={"a":1};</script>'):
        with pytest.raises(PageChanged):
            directory(page)


def test_merchants_action_lists_filters_sorts_and_pages(action_runner, fake_http, offerlove):
    url = "https://offer.love/offers/"
    fake_http.routes[url] = html_response(url, "directory.html")
    out = run(action_runner, offerlove, "merchants", {"limit": 3}, json_out=False)
    assert 'site_total: "16 merchants, 279 offers"' in out and "count: 3 of 16 total" in out
    assert "merchants[3]{merchant,name,offers}:" in out and "pfchangs,P.F. Chang's,57" in out
    assert "note: \"offers is the directory's count; `merchant` shows one offer per title and bank" in out
    assert "esum offerlove merchants --offset 3 --limit 3" in out and not any(
        line.startswith("offers:") for line in out.splitlines())      # no filter, so no filtered sum
    bagels = run(action_runner, offerlove, "merchants", {"query": "bagel"})
    assert (bagels["total"], bagels["offers"]) == (3, 5)
    one = run(action_runner, offerlove, "merchants", {"query": "pf changs"}, json_out=False)
    assert "esum offerlove merchant --slug pfchangs" in one          # the hand-off names the one merchant
    big = run(action_runner, offerlove, "merchants", {"min_offers": 20, "sort": "name"})
    assert [r["merchant"] for r in big["merchants"]] == ["76", "britishairways", "giftcards.com", "jerseymikes",
                                                        "pfchangs", "shell", "sprouts", "sproutsfarmersmarket"]
    assert big["offers"] == 253 and big["min_offers"] == 20
    assert len(fake_http.requests) == 4                              # one request a call, whatever the filter


def test_merchants_action_with_nothing_to_show(action_runner, fake_http, offerlove):
    url = "https://offer.love/offers/"
    fake_http.routes[url] = html_response(url, "directory.html")
    typo = run(action_runner, offerlove, "merchants", {"query": "walmrt"}, json_out=False)
    assert "closest: walmart (Walmart)" in typo and "0 found: no merchant in the directory matches 'walmrt'" in typo
    assert "esum offerlove search --query walmrt" in typo
    assert "esum offerlove merchant --slug walmart` to list the offers at Walmart, the closest name" in typo
    assert not any(line.startswith("offers:") for line in typo.splitlines())
    none = run(action_runner, offerlove, "merchants", {"query": "bagel", "min_offers": 9}, json_out=False)
    # the name matched three merchants; only the count filter emptied it, so no guess and no "not proof"
    assert "0 found: 3 merchants match 'bagel', none with 9 or more offers" in none
    assert "to drop the offer-count filter" in none and "merchant --slug bagel" not in none and "not proof" not in none
    # nothing has the name: dropping the count filter would not help, so it isn't offered
    both = run(action_runner, offerlove, "merchants", {"query": "walmrt", "min_offers": 10}, json_out=False)
    assert "no merchant in the directory matches 'walmrt'" in both and "offer-count filter" not in both
    assert "0 found: no merchant in the directory has 60 or more offers" in run(
        action_runner, offerlove, "merchants", {"min_offers": 60}, json_out=False)
    past = run(action_runner, offerlove, "merchants", {"offset": 50}, json_out=False)
    assert "0 found: no row at offset 50: 16 matched" in past and "to start from the first row" in past


def test_a_merchant_missing_from_the_directory_is_sent_to_its_own_page(action_runner, fake_http, offerlove):
    # the directory is rebuilt about daily: Chipotle had a live Citi offer and no entry (2026-11-18)
    url = "https://offer.love/offers/"
    fake_http.routes[url] = html_response(url, "directory.html")
    out = run(action_runner, offerlove, "merchants", {"query": "Chipotle"}, json_out=False)
    assert "matches 'Chipotle'; the directory is a day behind at times, so this is not proof" in out
    assert "esum offerlove merchant --slug chipotle` to read that merchant's own page, which is live" in out
    typo = run(action_runner, offerlove, "merchants", {"query": "walmrt"}, json_out=False)
    assert typo.index("merchant --slug walmart`") < typo.index("merchant --slug walmrt`")     # the closest name first


def test_merchant_rows_say_what_a_row_is(action_runner, fake_http, offerlove):
    url = "https://offer.love/offers/walmart/"
    fake_http.routes[url] = html_response(url, "merchant_walmart.html")
    out = run(action_runner, offerlove, "merchant", {"slug": "walmart"})
    assert out["note"].startswith("one row per title and bank") and "`merchants`" in out["note"]


def test_an_offset_past_the_last_row_is_not_an_empty_list(action_runner, fake_http, offerlove):
    url = "https://offer.love/offers/bank/chase/"
    fake_http.routes[url] = html_response(url, "bank_chase.html")
    out = run(action_runner, offerlove, "bank", {"name": "chase", "offset": 98}, json_out=False)
    assert "0 found: no row at offset 98: 8 matched" in out and "lists no active Chase offers" not in out
    assert "Run `esum offerlove bank --name chase` to start from the first row" in out
    url = "https://offer.love/latest/"
    fake_http.routes[url] = html_response(url, "latest.html")
    out = run(action_runner, offerlove, "latest", {"offset": 500}, json_out=False)
    assert "0 found: no row at offset 500:" in out and "Run `esum offerlove latest` to start from the first row" in out


def test_directory_queries_fold_accents_and_suggest_by_slug_too():
    from esum.sites.offerlove.parse import directory, in_directory, nearest_merchants, plain
    rows = directory(text("directory.html"))
    assert [r["merchant"] for r in rows if in_directory(r, "au lac")] == ["ulcla"]        # the name is `Âu Lạc LA`
    assert plain("P.F. Chang's") == "pfchangs" and plain("Âu Lạc") == "aulac" and plain("-- ") == ""
    assert [r["merchant"] for r in nearest_merchants(rows, "pfchang")] == ["pfchangs"]    # near the slug, far from the name's words
    assert [r["merchant"] for r in nearest_merchants(rows, "sproutz")] == ["sprouts"]


def test_the_directory_list_in_other_shapes():
    from esum.sites.offerlove.parse import PageChanged, directory
    # spaces around `=`, a count written as text, a `true` where a count belongs, a row without a name
    rows = directory('<script>const ALL = [["b","Bee","",2],["a","Ay","","7"],["c","Cee","",true],["d",null,"",1]];</script>')
    assert [(r["merchant"], r["name"], r["offers"]) for r in rows] == [("a", "Ay", 7), ("b", "Bee", 2), ("d", "", 1),
                                                                    ("c", "Cee", None)]      # most offers first
    with pytest.raises(PageChanged, match="no offer counts"):
        directory('<script>const ALL=[["a","Ay","","many"],["b","Bee","",null]];</script>')


def test_merchants_refuses_a_count_below_one_and_a_query_with_nothing_to_look_for(action_runner, fake_http, capsys):
    url = "https://offer.love/offers/"
    fake_http.routes[url] = html_response(url, "directory.html")
    reg = Registry.discover()
    for argv, said in ((["--min-offers", "0"], "--min-offers 0 is below 1"),
                       (["--min-offers=-5"], "--min-offers -5 is below 1"),
                       (["--query=---"], "--query '---' has no letter or digit"),
                       (["--query", "   "], "has no letter or digit"), (["--query", ""], "has no letter or digit")):
        assert action_runner.cli(["offerlove", "merchants", *argv], registry=reg) == 2
        assert said in capsys.readouterr().out
    assert fake_http.requests == []                                    # refused before any request


def test_merchants_next_steps_keep_the_typed_flags(action_runner, fake_http, capsys):
    url = "https://offer.love/offers/"
    fake_http.routes[url] = html_response(url, "directory.html")
    reg = Registry.discover()
    assert action_runner.cli(["offerlove", "merchants", "--query", "bagel", "--min-offers", "9"], registry=reg) == 0
    assert "Run `esum offerlove merchants --query bagel --min-offers 1` to drop the offer-count filter" in capsys.readouterr().out
    assert action_runner.cli(["offerlove", "merchants", "--sort", "name", "--offset", "40"], registry=reg) == 0
    out = capsys.readouterr().out
    assert "no row at offset 40: 16 matched" in out
    assert "Run `esum offerlove merchants --sort name --offset 0` to start from the first row" in out


def test_expiring_and_my_offers_past_the_last_row(action_runner, fake_http, offerlove, frozen):
    url = "https://offer.love/expiring/"
    fake_http.routes[url] = html_response(url, "expiring.html")
    out = run(action_runner, offerlove, "expiring", {"bank": "amex", "offset": 900}, json_out=False)
    listed = run(action_runner, offerlove, "expiring", {"bank": "amex"})["total"]
    if listed:      # the fixture's Amex rows, after the ones past their date are left out
        assert f"0 found: no row at offset 900: {listed} matched" in out and "to start from the first row" in out
    mine = my_offers(action_runner, offerlove, Browser(json.loads(text("my_offers.json"))), json_out=False, offset=50)
    assert "0 found: no row at offset 50: 5 matched" in mine and "to start from the first row" in mine
