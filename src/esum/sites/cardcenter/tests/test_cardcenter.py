"""CardCenter (D-80): the rates page's buy orders, the deals behind them and one deal, read-only.

Fixtures are the site's real `/Api/BuyOrders/v2` and `/Api/Deals/<id>` answers, trimmed to a few
orders; the account blocks (user ids, e-mails, permissions) are placeholders."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from conftest import json_response

from esum import Options
from esum.browser.login import login_problem
from esum.core.registry import Registry
from esum.sites.cardcenter.parse import (card_value, deal_detail, deal_rows, eastern, money, percent, rate_rows,
                                         takes)
from esum.transport import Response

FIX = Path(__file__).parent / "fixtures"
BASE = "https://cardcenter.cc"
RATES = f"{BASE}/Api/BuyOrders/v2?pageSize=0"


def data(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture
def cardcenter():
    return Registry.discover().get("cardcenter")


def run(action_runner, target, name, kwargs, json_out=False):
    output = io.StringIO()
    action_runner(target, target.all_actions[name], kwargs, Options(json=json_out), stream=output)
    return json.loads(output.getvalue())["data"] if json_out else output.getvalue()


def test_fixtures_hold_no_account():
    for name in ("rates.json", "deal.json"):
        text = (FIX / name).read_text(encoding="utf-8")
        assert "@" not in text.replace("@example.invalid", "") and "slackChannel" not in text


def test_numbers_read_as_the_site_shows_them():
    assert [money(v) for v in (2140.0, 79.99, 9, None)] == ["$2,140", "$79.99", "$9", ""]
    assert [percent(v) for v in (0.835, 1.0, -0.0818181, 0.6667, None)] == ["83.5%", "100%", "-8.18%", "66.67%", ""]
    assert eastern("2026-12-01T05:59:00Z") == "2026-12-01 00:59 ET"          # midnight ET the night before
    assert eastern("2026-11-02T17:36:34.404118Z") == "2026-11-02 12:36 ET" and eastern(None) == ""
    assert card_value({"value": 500, "lowValue": 450.0, "divisibleByValue": 5.0}) == "$450-500 by $5"
    assert card_value({"value": 100, "lowValue": 10.0}) == "$10-100" and card_value({"value": 25}) == "$25"


def test_rate_rows_keep_each_orders_deal_rates_and_limits():
    rows = rate_rows(data("rates.json"))
    assert len(rows) == 10
    first = {k: rows[0][k] for k in ("deal", "brand", "value", "cost", "pay", "profit", "margin", "cap", "terms",
                                     "per_seller", "deadline", "until", "vendor")}
    # profit is pay minus cost; margin is the site's own figure, relative to pay
    assert first == {"deal": 894500, "brand": "Ace Hardware", "value": "$10", "cost": "83.3%", "pay": "77%",
                     "profit": "-6.3%", "margin": "-8.18%", "cap": "$2,140", "terms": "14 days",
                     "per_seller": "$100 unfulfilled", "deadline": "within 2 days of approval",
                     "until": "2026-12-01 00:59 ET", "vendor": "Ace Hardware"}
    by_value = {(r["brand"], r["value"]): r for r in rows}
    assert by_value[("Amazon", "$40")]["per_seller"] == "$360 unfulfilled, $720 total"
    assert by_value[("Amazon", "$100")]["cap"] == ""                       # the site gives no cap there
    casa = by_value[("Casa Bar and Grill", "$10-100")]
    assert (casa["cost"], casa["pay"], casa["profit"], casa["margin"]) == ("", "69%", "", "")   # no deal price
    assert by_value[("Kroger Fuel Points", "$9")]["deadline"] == "by 2026-11-11 20:30 ET"   # a fixed deadline


def test_a_value_matches_its_own_line_and_ranges_on_their_step():
    rows = {(r["brand"], r["value"]): r for r in rate_rows(data("rates.json"))}
    airbnb = rows[("Airbnb", "$450-500 by $5")]
    assert takes(airbnb, 455) and takes(airbnb, 500) and not takes(airbnb, 452) and not takes(airbnb, 100)
    assert takes(rows[("Casa Bar and Grill", "$10-100")], 37.5) and takes(rows[("Amazon", "$100")], 100)
    assert not takes(rows[("Amazon", "$50")], 100)


def test_deal_rows_group_orders_and_take_the_best_line():
    deals = {d["id"]: d for d in deal_rows(rate_rows(data("rates.json")))}
    anywhere = deals[857604]
    assert (anywhere["vendor"], anywhere["title"], anywhere["lines"]) == ("Gift Cards", "From Anywhere", 3)
    assert (anywhere["brands"], anywhere["pay"], anywhere["profit"], anywhere["margin"]) == (
        "Airbnb, Amazon", "95%", "-5%", "-5.26%")
    assert (deals[857679]["profit"], deals[857679]["margin"]) == ("", "")   # none of its lines has one
    assert deals[914235]["pay"] == ""                                       # nor a rate at all
    assert anywhere["until"] == "2027-01-01 00:59 ET"                       # the last of its lines to close


def test_deal_detail_drops_the_account_and_keeps_every_rate():
    d = deal_detail(data("deal.json"))
    assert (d["id"], d["vendor"], d["where"], d["ends"]) == (894500, "Ace Hardware", "online", "2026-12-01 00:59 ET")
    assert d["url"].startswith("https://acehardware.gift.solutions/") and d["category"] == "Online (Other)"
    assert [(r["value"], r["pay"], r["profit"]) for r in d["rates"]] == [("$10", "77%", "-6.3%"),
                                                                         ("$100", "82.5%", "-0.8%")]
    standing = {**data("deal.json"), "endDate": None, "brand": None}       # a deal with no end or brand of its own
    assert deal_detail(standing)["ends"] == "2026-12-01 00:59 ET" and deal_detail(standing)["brand"] == "Ace Hardware"
    assert "example.invalid" not in json.dumps(d) and "user" not in d


def test_rates_filters_by_brand_sorts_and_names_the_next_call(action_runner, fake_http, cardcenter):
    fake_http.routes[RATES] = json_response(RATES, data("rates.json"))
    out = run(action_runner, cardcenter, "rates", {"brand": "amazon"})
    assert "rates[5]{deal,brand,value,cost,pay,profit,cap}:" in out and "sort: best profit first" in out
    rows = [line.strip() for line in out.splitlines() if line.startswith("  9") or line.startswith("  8")]
    assert rows[0].startswith("913856,Amazon,$15,66.67%,94.5%,27.83%") and rows[-1].startswith("857604,Amazon")
    hundred = run(action_runner, cardcenter, "rates", {"value": 100}, json_out=True)["rates"]
    assert [(r["brand"], r["value"]) for r in hundred] == [("Amazon", "$100"), ("Casa Bar and Grill", "$10-100")]
    assert "esum cardcenter deal --id <deal>" in out and "Ace Hardware" not in out
    assert "example.invalid" not in out


def test_rates_for_an_unknown_brand_is_an_empty_list_with_a_way_on(action_runner, fake_http, cardcenter):
    fake_http.routes[RATES] = json_response(RATES, data("rates.json"))
    out = run(action_runner, cardcenter, "rates", {"brand": "nosuchbrand"})
    # the way on drops the filter that matched nothing
    assert "CardCenter has no rate matching that" in out and "Run `esum cardcenter rates` to list" in out
    # a brand it does buy, at a value it doesn't: the way on keeps the brand
    out = run(action_runner, cardcenter, "rates", {"brand": "amazon", "value": 7})
    assert "Run `esum cardcenter rates --brand amazon` to see every value" in out
    assert "deal --id" not in out


def test_deals_lists_one_row_per_deal(action_runner, fake_http, cardcenter):
    fake_http.routes[RATES] = json_response(RATES, data("rates.json"))
    rows = run(action_runner, cardcenter, "deals", {"vendor": "bj"}, json_out=True)["deals"]
    assert [(r["id"], r["vendor"], r["brands"], r["pay"]) for r in rows] == [(893337, "BJ's", "Ace Hardware", "79%")]
    assert "deals[8]{id,vendor,title,brands,pay,profit}:" in run(action_runner, cardcenter, "deals", {})
    closing = run(action_runner, cardcenter, "deals", {"sort": "ending"}, json_out=True)["deals"]
    assert [d["id"] for d in closing[:2]] == [914235, 827751]                # 11-11 20:30, then 11-30 13:59
    assert [d["until"] for d in closing] == sorted(d["until"] for d in closing)
    best = run(action_runner, cardcenter, "deals", {"sort": "profit"}, json_out=True)["deals"]
    assert [d["id"] for d in best[:2]] == [913856, 861115] and best[-1]["profit"] == ""   # no figure goes last


def test_deal_prints_where_when_and_each_rate(action_runner, fake_http, cardcenter):
    url = f"{BASE}/Api/Deals/894500"
    fake_http.routes[url] = json_response(url, data("deal.json"))
    out = run(action_runner, cardcenter, "deal", {"id": 894500})
    assert "Buy $50 Ace Gift Card and Get $10 Ace Gift Card Free" in out and "where: online" in out
    assert "brand: Ace Hardware" in out and "ends: 2026-12-01 00:59 ET" in out
    assert ("Ace Hardware $100: cost 83.3% → pay 82.5%, profit -0.8% (site margin -0.97%); cap $21,700 "
            "($500 unfulfilled per seller); paid in 14 days; submit within 2 days of approval; "
            "bought until 2026-12-01 00:59 ET") in out
    assert "esum cardcenter rates --brand 'Ace Hardware'" in out and "example.invalid" not in out


def test_a_missing_deal_says_so(action_runner, fake_http, cardcenter, capsys):
    url = f"{BASE}/Api/Deals/1"
    fake_http.routes[url] = Response(404, url, {}, b"")
    assert action_runner.cli(["cardcenter", "deal", "--id", "1"], registry=Registry.discover()) == 1
    out = capsys.readouterr().out
    assert "CardCenter has no deal 1" in out and "help: Run `esum cardcenter deals`" in out


def test_a_line_without_a_rate_reads_as_one():
    from esum.sites.cardcenter import _rate_line
    rows = {r["brand"]: r for r in rate_rows(data("rates.json"))}
    assert _rate_line(rows["Kroger Fuel Points"]).startswith("  Kroger Fuel Points $9: no rate given; cap not given")
    assert "cost not given → pay 69%; cap $15,230;" in _rate_line(rows["Casa Bar and Grill"])


def test_a_logged_out_answer_is_login_needed(action_runner, fake_http, cardcenter, capsys):
    # the API answers 401 to a guest, or sends a page request to the login form
    assert login_problem(cardcenter, Response(401, RATES, {}, b""))
    assert login_problem(cardcenter, Response(302, RATES, {"location": "/Account/Login"}, b""))
    assert not login_problem(cardcenter, json_response(RATES, {"items": []}))
    for status in (401, 302):
        fake_http.routes[RATES] = Response(status, RATES, {"location": "https://elsewhere.example/x?u=me@example.invalid"}, b"")
        assert action_runner.cli(["cardcenter", "rates"], registry=Registry.discover()) == 3
        out = capsys.readouterr().out
        assert "LOGIN NEEDED" in out and "elsewhere.example" not in out and "@" not in out


def test_requests_never_follow_a_redirect(action_runner, fake_http, cardcenter):
    # D-80: only fixed paths are asked for, so a redirect is refused, not followed with the user's cookies
    url = f"{BASE}/Api/Deals/894500"
    fake_http.routes[RATES] = json_response(RATES, data("rates.json"))
    fake_http.routes[url] = json_response(url, data("deal.json"))
    run(action_runner, cardcenter, "rates", {})
    run(action_runner, cardcenter, "deals", {})
    run(action_runner, cardcenter, "deal", {"id": 894500})
    assert len(fake_http.requests) == 3
    assert all(r.method == "GET" and not r.follow_redirects and r.url.startswith(f"{BASE}/Api/") for r in fake_http.requests)


def test_a_failed_answer_never_prints_its_body(action_runner, fake_http, cardcenter, capsys):
    # an error from an API that carries the account could name it: only the status is shown
    body = b'{"error": "nope", "user": {"id": 123, "email": "person@example.invalid"}}'
    for argv, url in ((["cardcenter", "rates"], RATES), (["cardcenter", "deal", "--id", "5"], f"{BASE}/Api/Deals/5")):
        fake_http.routes[url] = Response(500, url, {"content-type": "application/json"}, body)
        assert action_runner.cli(argv, registry=Registry.discover()) == 1
        out = capsys.readouterr().out
        assert "HTTP 500" in out and "person@" not in out and "123" not in out


def test_a_deal_with_an_open_ended_line_has_no_closing_time():
    orders = data("rates.json")["items"][:2]
    one_deal = [{**o, "deal": orders[0]["deal"]} for o in orders]
    assert deal_rows(rate_rows({"items": one_deal}))[0]["until"] == "2026-12-01 00:59 ET"
    one_deal[1] = {k: v for k, v in one_deal[1].items() if k != "autoApproveOffersUntil"}
    row = deal_rows(rate_rows({"items": one_deal}))[0]
    assert (row["until"], row["_until"]) == ("", None)                       # not the other line's date
    standing = {**data("deal.json"), "endDate": None}
    standing["users"][1]["rates"][0].pop("autoApproveOffersUntil")
    assert deal_detail(standing)["ends"] == ""


def test_ending_sort_is_by_the_instant_not_the_shown_clock(action_runner, fake_http, cardcenter):
    # when DST ends, 01:50 ET comes before 01:10 ET: sorting the shown text would swap them
    first, second = (dict(o) for o in data("rates.json")["items"][:2])
    first["autoApproveOffersUntil"], second["autoApproveOffersUntil"] = "2026-11-01T06:10:00Z", "2026-11-01T05:50:00Z"
    fake_http.routes[RATES] = json_response(RATES, {"items": [first, second]})
    rows = run(action_runner, cardcenter, "rates", {"sort": "ending"}, json_out=True)["rates"]
    assert [r["until"] for r in rows] == ["2026-11-01 01:50 ET", "2026-11-01 01:10 ET"]
    assert eastern("2026-11-11T20:49:53+00:00") == "2026-11-11 15:49 ET" and eastern("not a time") == ""


def test_the_way_on_from_an_empty_page_starts_at_the_top(action_runner, fake_http, cardcenter):
    fake_http.routes[RATES] = json_response(RATES, data("rates.json"))
    out = run(action_runner, cardcenter, "rates", {"brand": "amazon", "value": 7, "offset": 50})
    assert "rates --brand amazon`" in out and "--offset 50" not in out
    # a brand it does not buy at all: keeping it would only lead to another empty list
    out = run(action_runner, cardcenter, "rates", {"brand": "nosuchbrand", "value": 7})
    assert "Run `esum cardcenter rates` to list every brand" in out
    out = run(action_runner, cardcenter, "deals", {"vendor": "nosuchvendor", "offset": 50})
    assert "Run `esum cardcenter deals` to list" in out


def test_no_action_flag_starts_with_no():
    # a flag named --no-<x> would be read as the off form of --<x> and filter nothing
    target = Registry.discover().get("cardcenter")
    assert not [p.flag for act in target.all_actions.values() for p in act.params if p.flag.startswith("--no-")]
