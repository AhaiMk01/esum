"""Aligned Incentives (D-80): the seller portal's deals page, read-only.

deals.html is the real page cut down to its embedded deals block with four deals (three line items
each at most); the logged-in account is a placeholder. dashboard.html is a portal page without it."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from esum import Options
from esum.browser.login import login_problem
from esum.core.registry import Registry
from esum.sites.alignedincentives.parse import (PageChanged, deal_detail, deal_row, deals_json, eastern, percent,
                                                rate_rows)
from esum.transport import Response

FIX = Path(__file__).parent / "fixtures"
BASE = "https://portal.alignedincentiv.es"
DEALS = f"{BASE}/Seller/Deals"
CITI = "b6e64b5c-1bcf-4f0e-bb7f-2b1f18249409"


def page(name: str = "deals.html") -> str:
    return (FIX / name).read_text(encoding="utf-8")


def html(url: str, name: str = "deals.html") -> Response:
    return Response(200, url, {"content-type": "text/html; charset=utf-8"}, page(name).encode("utf-8"))


@pytest.fixture
def site():
    return Registry.discover().get("alignedincentives")


def run(action_runner, target, name, kwargs, json_out=False):
    output = io.StringIO()
    action_runner(target, target.all_actions[name], kwargs, Options(json=json_out), stream=output)
    return json.loads(output.getvalue())["data"] if json_out else output.getvalue()


def test_fixture_holds_no_account():
    assert "@" not in page().replace("user@example.invalid", "")


def test_the_page_carries_its_deals_and_other_pages_none():
    deals = deals_json(page())
    assert [d["title"] for d in deals] == ["Google Play at Newegg", "Citi Offer In-Store Multideal",
                                           "Shell at Publix In-Store", "HEB In-Store Multideal"]
    with pytest.raises(PageChanged):
        deals_json(page("dashboard.html"))
    assert percent(94.53) == "94.53%" and percent(-5.0) == "-5%" and percent(None) == ""
    assert eastern("2026-11-11T20:49:53.8611246Z") == "2026-11-11 15:49 ET"   # seven fractional digits


def test_deal_row_spans_its_lines():
    citi = deal_row(next(d for d in deals_json(page()) if d["id"] == CITI))
    assert {k: citi[k] for k in ("title", "source", "brands", "pay", "profit", "cost", "cap", "ends", "needs",
                                 "lines")} == {
        "title": "Citi Offer In-Store Multideal", "source": "Citi Offer In-Store",
        "brands": "Walmart/Sam's Club, Amazon.com, Apple", "pay": "93% to 94.53%", "profit": "26.33% to 27.86%",
        "cost": "66.67%",
        "cap": "$270", "ends": "2026-12-01 00:59 ET", "needs": "receipt photo, card photos", "lines": 3}
    newegg = deal_row(deals_json(page())[0])
    assert (newegg["pay"], newegg["profit"], newegg["needs"]) == ("87%", "-5%", "")   # one line: no span


def test_rate_rows_are_one_per_brand_and_value():
    rows = rate_rows(deals_json(page()))
    assert len(rows) == 8
    amazon = next(r for r in rows if r["brand"] == "Amazon.com")
    assert {k: amazon[k] for k in ("deal", "value", "cost", "pay", "profit", "cap", "source", "cards", "left", "needs")} == {
        "deal": CITI, "value": "$15", "cost": "66.67%", "pay": "94%", "profit": "27.33%", "cap": "$90",
        "source": "Citi Offer In-Store", "cards": 6, "left": 986, "needs": "receipt photo, card photos"}


def test_deal_detail_keeps_both_notes_and_every_line():
    d = deal_detail(next(x for x in deals_json(page()) if x["id"] == CITI))
    assert d["notes"].startswith("$5 back on $15 of spend at CVS in-store") and d["hold"] == "72 hours"
    assert "Citi offers are targeted" in d["source_notes"] and "\r" not in d["source_notes"]
    assert d["total_left"] == "$44,505" and [r["brand"] for r in d["rates"]] == ["Walmart/Sam's Club", "Amazon.com", "Apple"]
    newegg = deal_detail(deals_json(page())[0])
    assert newegg["url"].startswith("https://www.newegg.com/") and newegg["category"] == "Online Shopping, Electronics"


def test_deals_lists_newest_first_and_filters(action_runner, fake_http, site):
    fake_http.routes[DEALS] = html(DEALS)
    out = run(action_runner, site, "deals", {})
    assert "deals[4]{id,title,source,pay,profit,cap,ends}:" in out and "example.invalid" not in out
    order = [r["title"] for r in run(action_runner, site, "deals", {}, json_out=True)["deals"]]
    assert order == ["Shell at Publix In-Store", "Google Play at Newegg", "Citi Offer In-Store Multideal",
                     "HEB In-Store Multideal"]
    assert "esum alignedincentives deal --id <id>" in out
    rows = run(action_runner, site, "deals", {"brand": "apple", "sort": "profit"}, json_out=True)["deals"]
    assert [r["id"] for r in rows] == [CITI]
    assert [r["title"] for r in run(action_runner, site, "deals", {"source": "heb"}, json_out=True)["deals"]] == [
        "HEB In-Store Multideal"]


def test_deals_filter_out_proof_and_sort_by_end(action_runner, fake_http, site):
    fake_http.routes[DEALS] = html(DEALS)
    easy = run(action_runner, site, "deals", {"photo_free": True}, json_out=True)
    assert [r["title"] for r in easy["deals"]] == ["Google Play at Newegg"] and easy["needs"] == "no receipt or card photos"
    ending = run(action_runner, site, "deals", {"sort": "ending"}, json_out=True)["deals"]
    assert [r["ends"][:10] for r in ending] == ["2026-11-16", "2026-11-18", "2026-11-19", "2026-12-01"]


def test_the_photo_free_flag_filters_from_the_command_line(action_runner, fake_http, site, capsys):
    # a flag named --no-<x> would be read as the off form of --<x> and filter nothing
    fake_http.routes[DEALS] = html(DEALS)
    assert action_runner.cli(["alignedincentives", "deals", "--photo-free", "--limit", "1"],
                             registry=Registry.discover()) == 0
    out = capsys.readouterr().out
    assert "count: 1" in out and "Google Play at Newegg" in out and "needs: no receipt or card photos" in out


def test_no_action_flag_starts_with_no():
    target = Registry.discover().get("alignedincentives")
    assert not [p.flag for act in target.all_actions.values() for p in act.params if p.flag.startswith("--no-")]


def test_deals_with_no_match_is_empty_with_a_way_on(action_runner, fake_http, site):
    fake_http.routes[DEALS] = html(DEALS)
    out = run(action_runner, site, "deals", {"source": "nosuchsource"})
    # the way on drops the filter that matched nothing
    assert "no current deal matches" in out and "Run `esum alignedincentives deals` to list" in out


def test_rates_sorts_by_profit_and_filters_by_brand(action_runner, fake_http, site):
    fake_http.routes[DEALS] = html(DEALS)
    rows = run(action_runner, site, "rates", {}, json_out=True)["rates"]
    assert [r["profit"] for r in rows[:3]] == ["27.86%", "27.33%", "26.33%"] and rows[-1]["profit"] == "-5%"
    out = run(action_runner, site, "rates", {"brand": "shell"})
    assert "rates[1]{deal,brand,value,cost,pay,profit,cap,source}:" in out and "Publix In-Store" in out
    assert "no current deal buys a card matching that" in run(action_runner, site, "rates", {"brand": "zzz"})
    out = run(action_runner, site, "rates", {"brand": "shell", "value": 7})
    assert "Run `esum alignedincentives rates --brand shell` to see every value" in out
    fifteen = run(action_runner, site, "rates", {"value": 15, "brand": "a"}, json_out=True)["rates"]
    assert [r["brand"] for r in fifteen] == ["Walmart/Sam's Club", "Amazon.com", "Apple"]


def test_deal_prints_notes_and_rates(action_runner, fake_http, site):
    fake_http.routes[DEALS] = html(DEALS)
    out = run(action_runner, site, "deal", {"id": CITI.upper()})                # ids match in any case
    assert "source: Citi Offer In-Store" in out and "a reservation is held for: 72 hours" in out
    assert "Amazon.com $15: 66.67% → 94%, 27.33%; $90 (6 cards; needs receipt photo, card photos)" in out
    assert "example.invalid" not in out


def test_a_deal_no_longer_listed_says_so(action_runner, fake_http, site, capsys):
    fake_http.routes[DEALS] = html(DEALS)
    assert action_runner.cli(["alignedincentives", "deal", "--id", "nope"], registry=Registry.discover()) == 1
    out = capsys.readouterr().out
    assert "lists no deal 'nope' now" in out and "ids are 36 characters, and at least the first 8" in out
    assert "help: Run `esum alignedincentives deals`" in out


def test_a_deal_id_may_be_cut_to_a_unique_start(action_runner, fake_http, site, capsys):
    fake_http.routes[DEALS] = html(DEALS)
    assert "source: Citi Offer In-Store" in run(action_runner, site, "deal", {"id": CITI[:8]})
    assert action_runner.cli(["alignedincentives", "deal", "--id", CITI[:5]], registry=Registry.discover()) == 1
    assert "lists no deal" in capsys.readouterr().out            # too short to be an id's start


def test_logged_out_is_login_needed(action_runner, fake_http, site, capsys):
    # a guest is sent to the portal's login page (which goes on to Google): the redirect is the answer
    login = f"{BASE}/Identity/Account/Login?ReturnUrl=%2FSeller%2FDeals&login_hint=me@example.invalid"
    assert login_problem(site, Response(302, DEALS, {"location": login}, b""))
    assert not login_problem(site, html(DEALS))
    fake_http.routes[DEALS] = Response(302, DEALS, {"location": login}, b"")
    assert action_runner.cli(["alignedincentives", "deals"], registry=Registry.discover()) == 3
    out = capsys.readouterr().out
    assert "LOGIN NEEDED" in out and "login_hint" not in out and "@" not in out
    (sent,) = fake_http.requests
    assert sent.method == "GET" and sent.url == DEALS and not sent.follow_redirects    # D-80: never followed


def test_a_page_without_its_deals_is_a_layout_change_not_a_login(action_runner, fake_http, site, capsys):
    # a logged-in page that lost the block must not ask the user to log in again, nor read as "no deals"
    fake_http.routes[DEALS] = html(DEALS, "dashboard.html")
    assert action_runner.cli(["alignedincentives", "deals"], registry=Registry.discover()) == 1
    out = capsys.readouterr().out
    assert "PageChanged" in out and "sellerDealsInitialDataJson" in out and "LOGIN NEEDED" not in out
    for block in ('{"deals": []}', "not json"):
        with pytest.raises(PageChanged):
            deals_json(f'<script type="application/json" id="sellerDealsInitialDataJson">{block}</script>')


def test_the_deals_block_is_read_raw_first_then_unescaped():
    wrap = '<script type="application/json" id="sellerDealsInitialDataJson">{}</script>'.format
    note = 'A &quot;quoted&quot; deal'                 # valid inside raw JSON: must not be unescaped into quotes
    assert deals_json(wrap(json.dumps([{"id": "x", "notes": note}])))[0]["notes"] == note
    encoded = json.dumps([{"id": "y"}]).replace('"', "&quot;")     # a block the server HTML-escaped whole
    assert deals_json(wrap(encoded)) == [{"id": "y"}]


def test_a_failed_answer_never_prints_its_body(action_runner, fake_http, site, capsys):
    fake_http.routes[DEALS] = Response(500, DEALS, {}, b"<p>Logged in as: person@example.invalid</p>")
    assert action_runner.cli(["alignedincentives", "rates"], registry=Registry.discover()) == 1
    out = capsys.readouterr().out
    assert "HTTP 500" in out and "person@" not in out


def test_cap_is_what_the_portal_lets_you_reserve_now():
    # HEB: your ceiling is 36 Scheels cards, but only 15 are left to pre-approve (the portal's own Cap: $1,500)
    heb = next(d for d in deals_json(page()) if d["title"] == "HEB In-Store Multideal")
    lines = {r["brand"]: (r["cards"], r["cap"], r["left"]) for r in rate_rows([heb])}
    assert lines == {"Scheels": (15, "$1,500", 15), "Nordstrom/Rack": (36, "$3,600", 432), "Ulta": (10, "$1,000", 10)}


def test_the_way_on_from_an_empty_page_starts_at_the_top(action_runner, fake_http, site):
    fake_http.routes[DEALS] = html(DEALS)
    out = run(action_runner, site, "rates", {"brand": "shell", "value": 7, "offset": 50})
    assert "rates --brand shell`" in out and "--offset 50" not in out
    out = run(action_runner, site, "rates", {"brand": "nosuchbrand", "value": 7})
    assert "Run `esum alignedincentives rates` to list every brand" in out
    out = run(action_runner, site, "deals", {"source": "nosuchsource", "offset": 50})
    assert "Run `esum alignedincentives deals` to list" in out


def test_an_id_start_shared_by_two_deals_asks_for_more(action_runner, fake_http, site, capsys):
    twin = page().replace("e054299f-a300", "b6e64b5c-a300")      # two ids now start b6e64b5c
    fake_http.routes[DEALS] = Response(200, DEALS, {"content-type": "text/html; charset=utf-8"}, twin.encode("utf-8"))
    assert action_runner.cli(["alignedincentives", "deal", "--id", "b6e64b5c"], registry=Registry.discover()) == 2
    assert "starts 2 current deals' ids" in capsys.readouterr().out
