"""Offline tests for Offer.love on saved API answers and pages (offer.love, data.offer.love/v1)."""

from __future__ import annotations

import io
import json
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
    # the API-key actions (my-offers, login, logout) were removed: the API could only search, 50 at most
    target = Registry.discover().get("offerlove")
    assert sorted(target.all_actions) == ["bank", "banks", "expiring", "latest", "merchant", "offer", "search"]
    assert "key_entry" not in target.extras
