"""Savewise (D-81): one store's portal rates and card offers from the extension's API (with an
anonymous token, as the extension gets one), and the Top Offers, stacking and gift card pages.

Fixtures are the API's real answers and the pages' real `__NEXT_DATA__`, trimmed to a few rows and
rewrapped in a minimal page. Nothing in them belongs to an account: the API and pages here serve
anyone. Token answers are made up."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from conftest import json_response

from esum import Options
from esum.core.registry import Registry
from esum.sites.savewise.parse import (PageChanged, card_rows, gift_rows, merchant_offers, page_props, rate,
                                       stack_rows, top_rows)
from esum.transport import Response

FIX = Path(__file__).parent / "fixtures"
SITE = "https://getsavewise.com"
API = "https://api.getsavewise.com"
TOKEN = "test-token-not-real"
OFFERS = f"{API}/offers/today/merchant/url?url=walmart.com"
CARDS = f"{API}/card-linked/offers?domain_url=walmart.com"


def text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def data(name: str):
    return json.loads(text(name))


def page(url: str, name: str) -> Response:
    return Response(200, url, {"content-type": "text/html; charset=utf-8"}, text(name).encode())


@pytest.fixture
def savewise():
    return Registry.discover().get("savewise")


@pytest.fixture
def api(fake_http):
    """The two token steps answer as the real API does."""
    fake_http.routes[f"{API}/auth/challenge"] = json_response(
        f"{API}/auth/challenge", {"challenge_token": "challenge-not-real", "expires_in": 60, "token_type": "challenge"})
    fake_http.routes[f"{API}/auth/authenticate"] = json_response(
        f"{API}/auth/authenticate", {"access_token": TOKEN, "expires_in": 1800, "token_type": "Bearer"})
    return fake_http


def run(action_runner, target, name, kwargs, json_out=False):
    output = io.StringIO()
    action_runner(target, target.all_actions[name], kwargs, Options(json=json_out), stream=output)
    return json.loads(output.getvalue())["data"] if json_out else output.getvalue()


def test_rates_read_as_the_portals_mean_them():
    assert rate(12.0, "%", None, "12.0% cashback") == "12%"
    assert rate(11, "%", None, "Up to 11% Cash Back") == "up to 11%"
    assert rate(8.0, "$", 1, "Up to $8") == "up to $8"
    assert rate(600.0, "Miles") == "600 miles" and rate(56200, "Points") == "56,200 points"
    assert rate(5.0, "Points/$", 1.0) == "5 points/$" and rate(210, "Miles/$", 10) == "210 miles/$10"
    assert rate(6.4, "%") == "6.4%" and rate(None, "%") == ""


def test_merchant_offers_group_by_kind_and_rank_by_the_bonus_rate():
    store, rows = merchant_offers(data("merchant_offers.json"))
    assert store == {"name": "Walmart", "domain": "walmart.com", "slug": "walmart"}
    assert [r["kind"] for r in rows] == ["cash %"] * 4 + ["cash $"] * 2 + ["points/$"] * 2 + ["miles"]
    first = rows[0]
    assert (first["portal"], first["rate"], first["bonus"]) == ("BeFrugal", "up to 11%", "16.5%")
    assert rows[1]["portal"] == "Price.com" and rows[1]["details"].startswith("Apparel, Accessories, and Jewelry: 12.0%")
    c1 = next(r for r in rows if r["portal"] == "CapitalOneShopping")
    assert (c1["rate"], c1["listed"]) == ("2%", "200")      # the portal's own figure is in basis points
    assert rows[-1]["rate"] == "600 miles" and rows[-1]["signup"]


def test_card_offers_soonest_first_with_their_cards():
    rows = card_rows(data("card_offers.json"))
    assert [r["expires"] for r in rows] == ["2026-11-14", "2026-11-22", "2026-11-30", ""]
    first = rows[0]
    assert (first["bank"], first["rate"], first["spend"], first["max"]) == ("Amex", "$49", "$98", "$49")
    assert first["offer"] == "Spend $98 on Walmart+ Annual Membership, earn $49 back" and first["terms"]
    assert rows[-1]["bank"] == "Citi" and "Citi Strata Premier" in rows[-1]["cards"] and rows[-1]["spend"] == ""


def test_pages_lists():
    top = top_rows(page_props(text("top_multipliers.html"), "top"))
    assert top[0]["rate"] == "200%" and top[0]["merchant"] == "Liquid Web"
    assert any(r["rate"].endswith("miles/$10") for r in top)
    stack = stack_rows(page_props(text("stack.html"), "stack"))
    assert {(r["bank"], r["portal"]) for r in stack} == {("amex", "Rakuten"), ("chase", "Rakuten"), ("citi", "Rakuten"),
                                                        ("simplymiles", "AmericanAirlines"), ("amex", "Rove"),
                                                        ("chase", "Rove")}
    adidas = next(r for r in stack if r["merchant"] == "adidas" and r["bank"] == "amex" and r["portal"] == "Rakuten")
    assert (adidas["card"], adidas["portal_rate"], adidas["expires"]) == ("6 points/$", "11%", "2026-12-31")
    gifts = gift_rows(page_props(text("gift_cards.html"), "gifts"))
    assert gifts[0]["discount"] == "50%" and any(g["brand"] == "Home Depot" for g in gifts)


def test_gift_cards_in_stock_first_then_discount_then_miles():
    props = {"giftCardOffers": [
        {"provider": "CardDepot", "provider_brand": "Gone", "discount_bps": 9000, "in_stock": False},
        {"provider": "Rove", "provider_brand": "Miles", "miles_multiplier": 4, "miles_is_up_to": True, "in_stock": True},
        {"provider": "Rove", "provider_brand": "More miles", "miles_multiplier": 17, "miles_is_up_to": False,
         "in_stock": True},
        {"provider": "CardDepot", "provider_brand": "Shown", "discount_bps": 425, "in_stock": True}]}
    rows = gift_rows(props)
    assert [r["brand"] for r in rows] == ["Shown", "More miles", "Miles", "Gone"]
    assert [(r["discount"], r["miles"]) for r in rows[:3]] == [("4.25%", ""), ("", "17 miles/$"), ("", "up to 4 miles/$")]


def test_a_changed_page_or_answer_is_named_without_its_content():
    with pytest.raises(PageChanged, match="no __NEXT_DATA__"):
        page_props("<html>secret</html>", "/gift-cards")
    for parse, bad in ((merchant_offers, {"data": {}}), (card_rows, {}), (top_rows, {}), (stack_rows, {}),
                       (gift_rows, {})):
        with pytest.raises(PageChanged):
            parse(bad)


def test_rates_send_an_anonymous_token_to_the_api_only(action_runner, api, savewise):
    api.routes[OFFERS] = json_response(OFFERS, data("merchant_offers.json"))
    out = run(action_runner, savewise, "rates", {"merchant": "walmart.com"})
    assert "rates[9]{portal,rate,bonus,details}:" in out and "BeFrugal,up to 11%,16.5%," in out
    assert "esum savewise cards --merchant walmart.com" in out
    sent = {r.url: r for r in api.requests}
    assert json.loads(sent[f"{API}/auth/challenge"].content) == {"client_id": "chrome_ext"}
    assert json.loads(sent[f"{API}/auth/authenticate"].content) == {"client_id": "chrome_ext",
                                                                   "challenge_token": "challenge-not-real"}
    assert sent[OFFERS].credential.headers == {"Authorization": f"Bearer {TOKEN}"}
    assert all(r.credential is None for r in api.requests if r.url != OFFERS)
    assert TOKEN not in out


def test_a_store_name_is_looked_up_on_its_page(action_runner, api, savewise):
    url = f"{SITE}/merchant/walmart"
    api.routes[url] = page(url, "merchant_page.html")
    api.routes[CARDS] = json_response(CARDS, data("card_offers.json"))
    rows = run(action_runner, savewise, "cards", {"merchant": "Walmart", "bank": "amex"}, json_out=True)["cards"]
    assert [r["rate"] for r in rows] == ["$49", "$10"]
    assert api.requests[0].url == url and api.requests[0].credential is None


def test_an_unknown_store_says_how_to_name_it(action_runner, api, savewise, capsys):
    url = f"{SITE}/merchant/no%20such%20store"
    api.routes[url] = page(url, "merchant_missing.html")
    assert action_runner.cli(["savewise", "rates", "--merchant", "No Such Store"], registry=Registry.discover()) == 1
    out = capsys.readouterr().out
    assert "Savewise has no store named 'No Such Store'" in out and "--merchant nosuchstore.com" in out
    bad = f"{API}/offers/today/merchant/url?url=nosuch.example"
    api.routes[bad] = json_response(bad, {"error": "Merchant with submitted URL not found"}, status=404)
    assert action_runner.cli(["savewise", "rates", "--merchant", "nosuch.example"], registry=Registry.discover()) == 1
    assert "Savewise has no store at 'nosuch.example'" in capsys.readouterr().out


def test_a_refused_token_shows_its_status_not_its_body(action_runner, fake_http, savewise, capsys):
    url = f"{API}/auth/challenge"
    fake_http.routes[url] = json_response(url, {"error": "body-should-not-show"}, status=403)
    assert action_runner.cli(["savewise", "rates", "--merchant", "walmart.com"], registry=Registry.discover()) == 1
    out = capsys.readouterr().out
    assert "403" in out and "body-should-not-show" not in out


def test_top_stack_and_giftcards_filter_and_page(action_runner, fake_http, savewise):
    for path, name in (("/top-offers/cashback", "top_cashback.html"), ("/top-offers/stack", "stack.html"),
                       ("/gift-cards", "gift_cards.html")):
        fake_http.routes[SITE + path] = page(SITE + path, name)
    rak = run(action_runner, savewise, "top", {"portal": "rakuten"}, json_out=True)["top"]
    assert rak and all(r["portal"] == "Rakuten" for r in rak)
    out = run(action_runner, savewise, "top", {"limit": 2})
    assert "top[2]{merchant,portal,rate,domain}:" in out and "--offset 2 --limit 2" in out
    rove = run(action_runner, savewise, "stack", {"portal": "rove", "bank": "chase"}, json_out=True)["stack"]
    assert rove and {(r["bank"], r["portal"]) for r in rove} == {("chase", "Rove")}
    assert "no nosuch card listed" in run(action_runner, savewise, "giftcards", {"brand": "nosuch"})
    hd = run(action_runner, savewise, "giftcards", {"brand": "home depot"}, json_out=True)["giftcards"]
    assert [r["brand"] for r in hd] == ["Home Depot"]
    lowes = run(action_runner, savewise, "giftcards", {"brand": "lowe's"}, json_out=True)["giftcards"]
    assert [(r["seller"], r["discount"], r["miles"]) for r in lowes] == [("CardDepot", "6%", ""), ("Rove", "", "1 miles/$")]
    rove = run(action_runner, savewise, "giftcards", {"seller": "rove"}, json_out=True)["giftcards"]
    assert {r["seller"] for r in rove} == {"Rove"} and len(rove) == 2
    assert all(r.credential is None for r in fake_http.requests)    # pages never get the token


def test_per_dollar_awards_rank_per_one_dollar():
    def item(portal, rate, unit):
        return {"merchant": {}, "portal": {"name": portal},
                "offer": {"offer_rate": rate, "offer_type_enum": "MPD", "offer_type_symbol": "Miles/$",
                          "unit_spent": unit}}
    _, rows = merchant_offers({"data": {"merchant": {}, "offers": [
        item("FlyingBlue", 2, 10), item("BritishAirways", 1, 2), item("CathayPacific", 0.25, 1)]}})
    assert [(r["portal"], r["rate"]) for r in rows] == [
        ("BritishAirways", "1 miles/$2"), ("CathayPacific", "0.25 miles/$"), ("FlyingBlue", "2 miles/$10")]


def test_stack_portal_flag_finds_the_aadvantage_block(action_runner, fake_http, savewise):
    url = f"{SITE}/top-offers/stack"
    fake_http.routes[url] = page(url, "stack.html")
    rows = run(action_runner, savewise, "stack", {"portal": "aadvantage"}, json_out=True)["stack"]
    assert rows and {(r["bank"], r["portal"]) for r in rows} == {("simplymiles", "AmericanAirlines")}
    assert all("_portal" not in r for r in rows)
