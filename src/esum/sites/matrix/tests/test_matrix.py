"""ITA Matrix: offline parser and action tests on scrubbed API responses."""

from __future__ import annotations

import io
import json
import re
from datetime import date
from pathlib import Path

import pytest
from conftest import json_response

from esum import Options
from support import needs_site, require_site
from esum.core.registry import Registry
from esum.sites.matrix import API
from esum.sites.matrix.parse import (api_error, booking, calendar_rows, fare_traits, full_id, location_rows,
                                     minutes, operators, rows_route, rule_sections, short_id, solution_page,
                                     split_token)

TOKEN = "SESSION0.SOLSET0.GVaDLw2ZdHCLfxaQ1oVkxE"   # session, solution set, id prefix

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def matrix():
    return Registry.discover().get("matrix")


def run(action_runner, target, action: str, kwargs: dict, *, json_out: bool = False) -> str:
    out = io.StringIO()
    action_runner(target, target.all_actions[action], kwargs, Options(json=json_out), stream=out)
    return out.getvalue()


def test_locations():
    rows = location_rows(load("locations.json"))
    assert [r["code"] for r in rows] == ["TYO", "NRT", "HND"]
    assert rows[0] == {"code": "TYO", "name": "Tokyo, Japan - All airports", "type": "city", "city": "TYO",
                       "timezone": "Asia/Tokyo"}


def test_minutes():
    assert (minutes(1170), minutes(45), minutes(None)) == ("19h30m", "45m", "")


def test_search_page():
    page = solution_page(load("search_roundtrip.json"))
    assert page.token == TOKEN
    assert (page.total, page.page, page.pages) == (30, 1, 2)
    first = page.rows[0]
    assert first["id"] == 9 and first["matrix_id"] == "GVaDLw2ZdHCLfxaQ1oVkxE009"
    assert first["price"] == "USD7212.99"
    assert first["itinerary"] == ("JFK 02-16 06:30 → NRT 02-17 16:00 · 19h30m · via BOS · AA4645 AA8475 | "
                                  "NRT 02-23 10:40 → JFK 02-23 13:59 · 17h19m · via ORD · AA8460 AA1529")
    assert first["stops"] == [1, 1] and first["carriers"] == "AA"
    assert "long layover" in first["warnings"]
    assert page.by_carrier == "AA USD7213.00 · multiple USD7217.00 · JL USD7221.00"   # by price
    assert page.currency == "USD"
    assert first["slices"][0]["cabins"] == ["first", "business"]
    assert page.by_stops == "1 stop USD7213.00"


def test_results_page_two():
    page = solution_page(load("results_page2.json"))
    assert (page.page, page.pages) == (2, 2)
    assert page.rows[0]["id"] == 27   # …00R


def test_token_round_trip():
    assert split_token("SESSION0.SOLSET0") == ("SESSION0", "SOLSET0", None)
    assert split_token(TOKEN) == ("SESSION0", "SOLSET0", "GVaDLw2ZdHCLfxaQ1oVkxE")
    assert split_token("nodot") is None and split_token("a.b.c.d") is None and split_token(".b") is None


def test_row_numbers_replace_case_sensitive_ids():
    prefix = "GVaDLw2ZdHCLfxaQ1oVkxE"
    lower, upper = short_id(prefix + "00c", prefix), short_id(prefix + "00C", prefix)
    assert (lower, upper) == ("38", "12")   # the two ids a case slip used to confuse
    assert full_id(lower, prefix) == prefix + "00c" and full_id(upper, prefix) == prefix + "00C"
    assert short_id("SomeOtherId", prefix) == "SomeOtherId" and full_id("12", None) is None


def test_detail_price_split_counts_every_passenger():
    data = load("detail.json")
    details = data["bookingDetails"]
    details["passengerCount"], details["displayTotal"] = 2, "USD14425.98"
    for ticket in details["tickets"]:
        for pricing in ticket["pricings"]:
            pricing["ext"]["pax"] = {"adults": 2}
    text = chr(10).join(booking(data).lines)
    assert ("Price split, all passengers: fares USD12644.00 (total less the charges below) · carrier-imposed "
            "(YQ/YR) USD1524.00 · government taxes and fees USD257.98") in text
    assert "Taxes and carrier charges (carrier-imposed first, per passenger):" in text
    assert "  ILX67IN4  AA NYC-TYO " in text and " a passenger · booking class I I" in text
    assert "  YR USD762.00  AA YR surcharge  [carrier-imposed]" in text


def test_detail():
    b = booking(load("detail.json"))
    assert b.title == "JFK→NRT→JFK · USD7212.99"
    text = "\n".join(b.lines)
    assert "AA8475 (operated as JL7)  BOS 02-16 12:00 → NRT 02-17 16:00 · 14h00m · Boeing 787 · class I business" in text
    assert "Operated by Republic Airways as American Eagle" in text
    assert "Operated by Jal" not in text   # the operating flight number already says it
    taxes = text.split("Taxes and carrier charges (carrier-imposed first):\n")[1].splitlines()
    assert taxes[0] == "  YR USD762.00  AA YR surcharge  [carrier-imposed]"
    assert "Price split: fares USD6322.00 (total less the charges below) · carrier-imposed (YQ/YR) USD762.00 · government taxes and fees USD128.99" in text
    assert "Connection at BOS: 4h05m, change of terminal" in text
    assert [f["basis"] for f in b.data["fares"]] == ["ILX67IN4", "ILX67IN4"]
    assert b.data["fares"][0]["market"] == "NYC-TYO" and b.data["fares"][0]["classes"] == "I I"
    assert b.data["fare_calculation"][0].startswith("NYC AA X/BOS AA TYO M 3161.00ILX67IN4")
    assert {"passengers": 1, "for": "1 adult", "code": "YR", "name": "AA YR surcharge", "amount": "USD762.00"} in b.data["taxes"]
    assert b.data["notes"] == ["This ticket is non-refundable."]


def test_detail_with_a_child_lists_each_passengers_fares_and_taxes():
    data = load("detail.json")
    details = data["bookingDetails"]
    details["passengerCount"] = 2
    pricing = details["tickets"][0]["pricings"][0]
    child = json.loads(json.dumps(pricing))
    pricing["ext"]["pax"], child["ext"]["pax"] = {"adults": 1}, {"children": 1}
    for fare in child["fares"]:
        fare["ptcs"] = ["CNN"]
    details["tickets"][0]["pricings"].append(child)
    text = chr(10).join(booking(data).lines)
    assert " a passenger · booking class I I · ADT" in text and " a passenger · booking class I I · CNN" in text
    assert "  1 adult:" + chr(10) + "    YR USD762.00  AA YR surcharge  [carrier-imposed]" in text
    assert "  1 child:" + chr(10) + "    YR USD762.00  AA YR surcharge  [carrier-imposed]" in text


def test_calendar_round_trip():
    rows = calendar_rows(load("calendar_roundtrip.json"))
    assert rows[0] == {"date": "2027-03-01", "price": "USD910.00", "solutions": 3, "nights": 7, "return": "2027-03-09",
                       "by_nights": "6n USD960.00 · 7n USD910.00 · 8n USD910.00"}
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)


def test_calendar_skips_neighbouring_months_padding():
    # March's last week lists Apr 1-3 with April's fares, and April's first week repeats Mar 28-31
    rows = calendar_rows(load("calendar_oneway.json"))
    by_date = {r["date"]: r["price"] for r in rows}
    assert by_date["2027-03-31"] == "USD359.00"
    assert by_date["2027-04-01"] == "USD776.00"
    assert "2027-03-01" not in by_date
    assert len(rows) == len(by_date)
    assert "nights" not in rows[0]


def test_api_error():
    assert api_error(load("error_airport.json")) == ("input", "QPX Warning. Unknown airport or city: QQQ")
    assert api_error(load("locations.json")) is None


def test_search_action_body_and_hints(action_runner, fake_http, matrix):
    url = f"{API}/search"
    fake_http.routes[url] = json_response(url, load("search_roundtrip.json"))
    out = run(action_runner, matrix, "search", {"origin": "jfk", "dest": "NRT", "depart": "2027-02-16",
                                                "return_": "2027-02-23", "cabin": "premium", "stops": 0,
                                                "routing": "AA+"})
    sent = json.loads(fake_http.requests[0].content)
    assert sent["name"] == "specificDatesSlice"
    inputs = sent["inputs"]
    assert inputs["cabin"] == "PREMIUM-COACH" and inputs["maxStopCount"] == 0
    assert [(s["origins"], s["destinations"], s["date"]) for s in inputs["slices"]] == [
        (["JFK"], ["NRT"], "2027-02-16"), (["NRT"], ["JFK"], "2027-02-23")]
    assert inputs["slices"][1]["routeLanguage"] == "AA+"
    assert "  9,USD7212.99," in out
    assert f"esum matrix detail --search {TOKEN} --id 9" in out
    assert f"esum matrix detail --search {TOKEN} --id <id>" in out
    assert f"esum matrix results --search {TOKEN} --page 2" in out
    assert f"esum matrix results --search {TOKEN} --limit 10 --operators" in out


def test_results_action_pages_without_searching(action_runner, fake_http, matrix):
    url = f"{API}/summarize"
    fake_http.routes[url] = json_response(url, load("results_page2.json"))
    out = run(action_runner, matrix, "results", {"search": "SESSION0.SOLSET0", "page": 2, "sort": "duration"})
    sent = json.loads(fake_http.requests[0].content)
    assert (sent["session"], sent["solutionSet"]) == ("SESSION0", "SOLSET0")
    assert sent["inputs"] == {"page": {"current": 2, "size": 25}, "sorts": "duration"}
    assert "page: 2 of 2" in out and "for the next page" not in out
    assert "route: JFK→NRT | NRT→JFK" in out and "dates: 2027-02-16 | 2027-02-23" in out


def test_calendar_action_round_trip(action_runner, fake_http, matrix):
    url = f"{API}/search"
    fake_http.routes[url] = [json_response(url, load("calendar_roundtrip.json")),
                             json_response(url, load("search_roundtrip.json"))]
    out = run(action_runner, matrix, "calendar", {"origin": "SFO", "dest": "LHR", "start": "2027-03-01",
                                                  "end": "2027-03-05", "nights": "6-8"})
    sent = json.loads(fake_http.requests[0].content)
    check = json.loads(fake_http.requests[1].content)
    assert check["name"] == "specificDatesSlice"   # the exact-date search, not the calendar's lighter one
    assert [(s["date"], s["dateModifier"]) for s in check["inputs"]["slices"]] == [
        ("2027-03-01", {"minus": 0, "plus": 0}), ("2027-03-09", {"minus": 0, "plus": 0})]
    assert len(fake_http.requests) == 2   # --check 1: only the cheapest date
    assert sent["name"] == "calendar" and sent["summarizerSet"] == "calendarRoundTrip"
    assert sent["inputs"]["layover"] == {"min": 6, "max": 8}
    # the trip may end past --end (8 nights + 1), so the last departures keep their returns
    assert (sent["inputs"]["startDate"], sent["inputs"]["endDate"]) == ("2027-03-01", "2027-03-15")
    assert "2027-03-01,2027-03-09,7,USD910.00,6n USD960.00 · 7n USD910.00 · 8n USD910.00,USD7212.99" in out
    assert "1 date searched for real; cheapest USD7212.99 on 2027-03-01 returning 2027-03-09" in out
    assert f"esum matrix detail --search {TOKEN} --id 9" in out
    assert f"esum matrix results --search {TOKEN}`" in out
    assert "2027-03-06" not in out   # past --end
    assert 'cheapest: "USD910.00 on 2027-03-01 (and 2 more dates at this price: 2027-03-02, 2027-03-03) (estimate)"' in out
    assert "esum matrix search --origin SFO --dest LHR --depart <date> --return <return>" in out


def test_calendar_action_names_dates_without_fares(action_runner, fake_http, matrix):
    url = f"{API}/search"
    fake_http.routes[url] = json_response(url, load("calendar_oneway.json"))
    out = run(action_runner, matrix, "calendar", {"origin": "SFO", "dest": "LHR", "start": "2027-03-18",
                                                  "end": "2027-04-08", "check": 0})
    sent = json.loads(fake_http.requests[0].content)
    assert sent["inputs"]["endDate"] == "2027-04-08" and "layover" not in sent["inputs"]
    assert 'no_fare: "2027-03-18 – 2027-03-19, 2027-04-06 – 2027-04-08"' in out
    assert 'cheapest: "USD330.00 on 2027-03-21 (and 3 more dates at this price: 2027-03-22,' in out
    assert "at this price: 2027-03-22, 2027-03-23, 2027-03-24) (estimate)" in out
    assert "suspect" not in out


def test_duration_sort_shows_durations_and_json_keeps_slices(action_runner, fake_http, matrix):
    url = f"{API}/summarize"
    fake_http.routes[url] = json_response(url, load("results_page2.json"))
    out = run(action_runner, matrix, "results", {"search": "SESSION0.SOLSET0", "sort": "duration"})
    assert "itineraries[2]{id,price,duration,itinerary}:" in out and "currency: USD" in out
    assert "slices" not in out
    data = json.loads(run(action_runner, matrix, "results", {"search": "SESSION0.SOLSET0"}, json_out=True))["data"]
    assert data["itineraries"][0]["slices"][0]["origin"] == "JFK"


def test_unknown_id_says_ids_are_case_sensitive(action_runner, fake_http, capsys):
    url = f"{API}/summarize"
    fake_http.routes[url] = json_response(url, {"error": {"message": "Unknown solution id SET/ABC", "type": "input"}})
    assert action_runner.cli(["matrix", "detail", "--search", "S.SET", "--id", "ABC"]) == 2
    out = capsys.readouterr().out
    assert "no itinerary 'ABC' in this search" in out and "row number" in out
    assert "Unknown solution id SET/ABC" in out
    assert "esum matrix results --search S.SET" in out


@pytest.mark.parametrize("argv, message", [
    (["search", "--origin", "JFK"], "search needs --dest, --depart"),
    (["search", "--origin", "New York", "--dest", "NRT", "--depart", "2027-02-16"], "3-letter airport"),
    (["search", "--origin", "JFK", "--dest", "NRT", "--depart", "16/02/2027"], "YYYY-MM-DD"),
    (["search", "--origin", "JFK", "--dest", "NRT", "--depart", "2027-02-30"], "is not a calendar date"),
    (["search", "--origin", "JFK", "--dest", "NRT", "--depart", "2027-02-16", "--flex", "3"], "0 to 2 days"),
    (["search", "--legs", "JFK-NRT"], "ORIGIN-DEST:YYYY-MM-DD"),
    (["calendar", "--origin", "SFO", "--dest", "LHR", "--start", "2027-03-01", "--nights", "8-6"], "--nights"),
    (["detail", "--search", "nodot", "--id", "x"], "ids joined by dots"),
])
def test_bad_input_is_a_usage_error(action_runner, capsys, argv, message):
    assert action_runner.cli(["matrix", *argv]) == 2
    assert message in capsys.readouterr().out


def test_matrix_error_object_is_a_usage_error(action_runner, fake_http, capsys):
    url = f"{API}/search"
    fake_http.routes[url] = json_response(url, load("error_airport.json"))
    assert action_runner.cli(["matrix", "search", "--origin", "QQQ", "--dest", "LHR", "--depart", "2027-04-01"]) == 2
    assert "Unknown airport or city: QQQ" in capsys.readouterr().out


def test_expired_search_says_to_search_again(action_runner, fake_http, capsys):
    url = f"{API}/summarize"
    fake_http.routes[url] = json_response(url, {"error": {"message": "Invalid session id.", "type": "input"}})
    assert action_runner.cli(["matrix", "detail", "--search", "OLD.SET", "--id", "x"]) == 2
    out = capsys.readouterr().out
    assert "Invalid session id." in out and "run `esum matrix search` again" in out


def test_detail_skips_operated_by_the_marketing_airline():
    data = load("detail.json")
    seg = data["bookingDetails"]["itinerary"]["slices"][1]["segments"][1]   # AA1529, no operating flight
    seg.setdefault("ext", {})["operationalDisclosure"] = "OPERATED BY AMERICAN"
    assert "Operated by American" not in "\n".join(booking(data).lines)


def test_airports_hint_uses_the_top_code_and_stops_read_per_slice(action_runner, fake_http, matrix):
    url = f"{API}/locationTypes/CITIES_AND_AIRPORTS/partialNames/tokyo/locations?pageSize=10"
    fake_http.routes[url] = json_response(url, load("locations.json"))
    out = run(action_runner, matrix, "airports", {"query": "tokyo"})
    assert "esum matrix search --origin TYO --dest <code> --depart '<YYYY-MM-DD>'" in out
    url = f"{API}/summarize"
    fake_http.routes[url] = json_response(url, load("results_page2.json"))
    target = matrix
    out = io.StringIO()
    action_runner(target, target.all_actions["results"], {"search": "SESSION0.SOLSET0"}, Options(fields="stops"),
                  stream=out)
    assert 'JL7453",1 | 1' in out.getvalue()

EMPTY_SEARCH = {"solutionList": {"solutions": [], "pages": {"count": 0, "current": 1}, "solutionCount": 0},
                "session": "SESSION0", "solutionSet": "SOLSET0", "solutionCount": 0}


def _lookup(fake_http, code: str, data: dict) -> None:
    url = f"{API}/locationTypes/CITIES_AND_AIRPORTS/partialNames/{code}/locations?pageSize=10"
    fake_http.routes[url] = json_response(url, data)


def test_empty_search_names_an_unknown_code(action_runner, fake_http, capsys):
    url = f"{API}/search"
    fake_http.routes[url] = json_response(url, EMPTY_SEARCH)
    _lookup(fake_http, "ZZQ", {})
    _lookup(fake_http, "NRT", load("locations.json"))
    assert action_runner.cli(["matrix", "search", "--origin", "ZZQ", "--dest", "NRT", "--depart", "2027-02-16"]) == 2
    out = capsys.readouterr().out
    assert "no airport or city with the code ZZQ" in out and "esum matrix airports" in out


def test_empty_search_with_known_codes_is_an_empty_list(action_runner, fake_http, capsys):
    url = f"{API}/search"
    fake_http.routes[url] = json_response(url, EMPTY_SEARCH)
    _lookup(fake_http, "TYO", load("locations.json"))
    _lookup(fake_http, "NRT", load("locations.json"))
    assert action_runner.cli(["matrix", "search", "--origin", "TYO", "--dest", "NRT", "--depart", "2027-02-16"]) == 0
    assert "0 found: Matrix found no itineraries" in capsys.readouterr().out


def test_raw_invalid_solution_error_reads_as_unknown_id(action_runner, fake_http, capsys):
    url = f"{API}/summarize"
    fake_http.routes[url] = json_response(url, {"error": {
        "message": "SET/NOSUCH is not a valid value for x.solution.", "type": "input"}})
    assert action_runner.cli(["matrix", "detail", "--search", "S.SET", "--id", "NOSUCH"]) == 2
    out = capsys.readouterr().out
    assert "no itinerary 'NOSUCH' in this search" in out
    assert "SET/NOSUCH is not a valid value for x.solution." in out


def test_page_past_the_end_names_the_last_page(action_runner, fake_http, capsys):
    url = f"{API}/summarize"
    fake_http.routes[url] = json_response(url, load("results_page2.json"))
    assert action_runner.cli(["matrix", "results", "--search", "S.T", "--page", "5"]) == 2
    assert "esum matrix results --search S.T --page 2" in capsys.readouterr().out

def test_empty_search_points_to_the_calendar_and_keeps_currency(action_runner, fake_http, capsys):
    url = f"{API}/search"
    fake_http.routes[url] = json_response(url, EMPTY_SEARCH)
    _lookup(fake_http, "LAX", {"locations": [{"code": "LAX", "cityCode": "LAX"}]})
    _lookup(fake_http, "SYD", {"locations": [{"code": "SYD", "cityCode": "SYD"}]})
    assert action_runner.cli(["matrix", "search", "--origin", "LAX", "--dest", "SYD", "--depart", "2027-03-08",
                              "--return", "2027-03-18", "--cabin", "first", "--currency", "usd"]) == 0
    out = capsys.readouterr().out
    assert "currency: USD" in out
    assert ("esum matrix calendar --origin LAX --dest SYD --start 2027-03-01 --end 2027-03-15 --nights 8-10 "
            "--cabin first --currency usd") in out


def test_detail_echoes_the_id(action_runner, fake_http, matrix):
    url = f"{API}/summarize"
    fake_http.routes[url] = json_response(url, load("detail.json"))
    out = run(action_runner, matrix, "detail", {"search": "S.T", "id": "GVaDLw2ZdHCLfxaQ1oVkxE009"})
    assert "Itinerary GVaDLw2ZdHCLfxaQ1oVkxE009: JFK→NRT→JFK · USD7212.99" in out

def test_detail_takes_a_row_number(action_runner, fake_http, matrix):
    url = f"{API}/summarize"
    fake_http.routes[url] = json_response(url, load("detail.json"))
    out = run(action_runner, matrix, "detail", {"search": TOKEN, "id": "38"})
    sent = json.loads(fake_http.requests[0].content)
    assert sent["inputs"]["solution"] == "SOLSET0/GVaDLw2ZdHCLfxaQ1oVkxE00c"
    assert "Itinerary 38: JFK→NRT→JFK" in out


def test_operators_summary():
    assert operators(load("detail.json")) == ("AA4645 by Republic Airways as American Eagle · AA8475 by JL7 · "
                                              "AA8460 by JL56")


def test_results_with_operators_reads_each_row(action_runner, fake_http, matrix):
    url = f"{API}/summarize"
    fake_http.routes[url] = [json_response(url, load("results_page2.json")), json_response(url, load("detail.json"))]
    out = run(action_runner, matrix, "results", {"search": TOKEN, "operators": True})
    assert len(fake_http.requests) == 3   # the page, then one detail per row
    assert json.loads(fake_http.requests[1].content)["inputs"]["solution"] == "SOLSET0/GVaDLw2ZdHCLfxaQ1oVkxE00R"
    assert "itineraries[2]{id,price,itinerary,operators}:" in out and "AA8475 by JL7" in out
    assert "--operators`" not in out




def test_rows_route():
    rows = solution_page(load("search_roundtrip.json")).rows
    assert rows_route(rows) == ("JFK→NRT | NRT→JFK", "2027-02-16 | 2027-02-23")

def test_a_failed_check_keeps_the_calendar(action_runner, fake_http, matrix):
    url = f"{API}/search"
    from esum.transport import Response
    fake_http.routes[url] = [json_response(url, load("calendar_roundtrip.json")),
                             Response(502, url, {"content-type": "text/html"}, b"<html>Error 502</html>")]
    out = run(action_runner, matrix, "calendar", {"origin": "SFO", "dest": "LHR", "start": "2027-03-01",
                                                  "end": "2027-03-05", "nights": "6-8"})
    assert "USD910.00,search failed (HTTP 502)" in out
    assert "the real searches failed" in out and "found nothing" not in out
    assert "2027-03-01,2027-03-09,7,USD910.00" in out
    assert "esum matrix search --origin SFO --dest LHR --depart 2027-03-01 --return 2027-03-09" in out

def test_calendar_flags_overpriced_dates_as_suspect(action_runner, fake_http, matrix):
    cal = load("calendar_roundtrip.json")
    for week in cal["calendar"]["months"][0]["weeks"]:
        for day in week["days"]:
            if day["date"] in (4, 5):
                day["minPrice"] = "USD6876.00"   # the calendar's own error, seen live
    url = f"{API}/search"
    fake_http.routes[url] = json_response(url, cal)
    out = run(action_runner, matrix, "calendar", {"origin": "SFO", "dest": "LHR", "start": "2027-03-01",
                                                  "end": "2027-03-05", "nights": "6-8", "check": 0})
    assert 'suspect: "2027-03-04 – 2027-03-05: over 3 times the range\'s median estimate' in out
    assert "esum matrix search --origin SFO --dest LHR --depart 2027-03-04 --return 2027-03-11" in out
    assert "price a suspect date for real (the calendar says USD6876.00)" in out


def test_checks_run_side_by_side(action_runner, matrix):
    import threading
    from conftest import FakeBackend
    url = f"{API}/search"
    both_in_flight = threading.Barrier(2, timeout=10)   # breaks unless two checks overlap

    class Backend(FakeBackend):
        def send(self, request, *, before_send=lambda: None):
            if json.loads(request.content)["name"] == "specificDatesSlice":
                both_in_flight.wait()
            return super().send(request, before_send=before_send)

    action_runner.backend = Backend({url: [json_response(url, load("calendar_roundtrip.json")),
                                           json_response(url, load("search_roundtrip.json"))]})
    out = run(action_runner, matrix, "calendar", {"origin": "SFO", "dest": "LHR", "start": "2027-03-01",
                                                  "end": "2027-03-05", "nights": "6-8", "check": 2})
    assert "2 dates searched for real" in out
    assert out.count(",USD7212.99") == 2

def test_a_check_that_times_out_keeps_the_calendar(action_runner, fake_http, matrix):
    import httpx
    url = f"{API}/search"
    fake_http.routes[url] = [json_response(url, load("calendar_roundtrip.json")),
                             httpx.ReadTimeout("timed out")]
    out = run(action_runner, matrix, "calendar", {"origin": "SFO", "dest": "LHR", "start": "2027-03-01",
                                                  "end": "2027-03-05", "nights": "6-8"})
    assert "2027-03-01,2027-03-09,7,USD910.00" in out
    assert "search failed (ReadTimeout: timed out)" in out


def test_no_return_to_check_keeps_the_calendar(action_runner, fake_http, matrix):
    cal = load("calendar_roundtrip.json")
    for week in cal["calendar"]["months"][0]["weeks"]:
        for day in week["days"]:
            for option in (day.get("tripDuration") or {}).get("options") or []:
                option.pop("solution", None)   # Matrix may leave out the sample itinerary
    url = f"{API}/search"
    fake_http.routes[url] = json_response(url, cal)
    out = run(action_runner, matrix, "calendar", {"origin": "SFO", "dest": "LHR", "start": "2027-03-01",
                                                  "end": "2027-03-05", "nights": "6-8"})
    assert len(fake_http.requests) == 1
    assert "USD910.00" in out and "no date had a return date to search" in out


def test_session_in_a_search_error_is_not_an_expired_search(action_runner, fake_http, capsys):
    url = f"{API}/search"
    fake_http.routes[url] = json_response(url, {"error": {"message": "QPX Warning. Too many sessions open.",
                                                          "type": "input"}})
    assert action_runner.cli(["matrix", "search", "--origin", "JFK", "--dest", "NRT", "--depart", "2027-02-16"]) == 2
    out = capsys.readouterr().out
    assert "Too many sessions open." in out
    assert "no longer has this search" not in out and "--search None" not in out


def test_a_large_number_is_not_a_row_of_this_search(action_runner, capsys):
    assert action_runner.cli(["matrix", "detail", "--search", TOKEN, "--id", "300000"]) == 2
    out = capsys.readouterr().out
    assert "--id 300000 is not a row number of this search" in out and "carries no row numbers" not in out


def test_legs_route_shows_normalised_codes(action_runner, fake_http, matrix):
    url = f"{API}/search"
    fake_http.routes[url] = json_response(url, load("search_roundtrip.json"))
    out = run(action_runner, matrix, "search", {"legs": ["jfk-nrt:2027-02-16", "nrt-jfk:2027-02-23"]})
    assert "route: JFK→NRT | NRT→JFK" in out and "dates: 2027-02-16 | 2027-02-23" in out


def test_third_party_notice_fences_open_cleanly():
    notices = (Path(__file__).parents[5] / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    fences = [line for line in notices.splitlines() if line.startswith("```")]
    assert fences and all(line == "```" for line in fences)


# ---------------------------------------------------------------- thorough search (D-93)

PREFIX = "ABCDEFGHIJKLMNOPQRSTU"   # 21 characters; each answer adds one to make Matrix's 22


def _answer(key: str, rows: list[tuple[str, str, str, str]], *, carriers: dict[str, str] | None = None) -> dict:
    """A one-way search answer from HNL: rows of (carrier, flight, price, destination airport)."""
    prefix = PREFIX + key[1]
    solutions = [{"id": f"{prefix}00{n + 1}", "displayTotal": price,
                  "itinerary": {"carriers": [{"code": carrier}], "slices": [{
                      "origin": {"code": "HNL"}, "destination": {"code": dest}, "departure": "2026-11-16T10:00-10:00",
                      "arrival": "2026-11-17T16:00+08:00", "duration": 720, "flights": [flight],
                      "stops": [{"code": "ICN"}] if carrier == "KE" else [], "cabins": ["COACH"]}]}}
                 for n, (carrier, flight, price, dest) in enumerate(rows)]
    groups = carriers if carriers is not None else {r[0]: r[2] for r in rows}
    return {"session": f"S{key}", "solutionSet": f"SET{key}", "solutionCount": len(solutions),
            "solutionList": {"solutions": solutions, "pages": {"count": 1, "current": 1},
                             "solutionCount": len(solutions)},
            "itineraryCarrierList": {"groups": [{"label": {"code": c}, "minPrice": p} for c, p in groups.items()]},
            "itineraryStopCountList": {"groups": [{"label": 0, "minPrice": p} for p in list(groups.values())[:1]]}}


JL = ("JL", "JL789", "USD795.00")
KE = ("KE", "KE54", "USD568.00")


def _thorough_backend(answers: dict, seen: list):
    """Answers each search by its airports, extension and routing, whatever order the threads send them."""
    from conftest import FakeBackend

    class Backend(FakeBackend):
        def send(self, request, *, before_send=lambda: None):
            if request.url == f"{API}/search":
                s = json.loads(request.content)["inputs"]["slices"][0]
                key = (",".join(s["destinations"]), s.get("commandLine"), s.get("routeLanguage"))
                seen.append(key)
                found = answers[key]
                if isinstance(found, BaseException):
                    raise found
                return json_response(request.url, found)
            return super().send(request, before_send=before_send)

    lookups = {"HNL/locations?pageSize=10": {"locations": [{"code": "HNL", "type": "airport", "cityCode": "HNL"}]},
               "BJS/locations?pageSize=10": {"locations": [{"code": "BJS", "type": "city", "cityCode": "BJS",
                                                  "cityName": "Beijing"}]},
               "Beijing/locations?pageSize=50": {"locations": [
                   {"code": "BJS", "type": "city", "cityCode": "BJS"},
                   {"code": "PEK", "type": "airport", "cityCode": "BJS"},
                   {"code": "PKX", "type": "airport", "cityCode": "BJS"},
                   {"code": "NAY", "type": "airport", "cityCode": "NAY"},     # its own city code: not Beijing's
                   {"code": "QQB", "type": "rail", "cityCode": "BJS"}]}}
    base = f"{API}/locationTypes/CITIES_AND_AIRPORTS/partialNames/"
    return Backend({base + k: json_response(base + k, v) for k, v in lookups.items()})


def _beijing(pkx_round_two):
    """HNL to Beijing as measured on 2026-11-13: each airport and each exclusion found carriers the
    open city search left out."""
    pr, ac, cx = ("PR", "PR100", "USD440.00"), ("AC", "AC8", "USD900.00"), ("CX", "CX890", "USD572.00")
    return {
        ("PEK", None, None): _answer("P1", [(*JL, "PEK"), (*KE, "PEK")]),
        ("PEK", "-AIRLINES JL KE", None): _answer("P2", [(*pr, "PEK")]),
        ("PEK", "-AIRLINES JL KE PR", None): _answer("P3", [(*ac, "PEK")]),   # finds AC, but 3 is the cap
        ("PKX", None, None): _answer("X1", [(*cx, "PKX")]),
        ("PKX", "-AIRLINES CX", None): pkx_round_two,
        ("BJS", None, "KE+"): _answer("K1", [(*KE, "PEK")]),                   # the PEK row again
    }


def test_thorough_splits_the_city_and_leaves_out_found_carriers(action_runner, matrix):
    import httpx
    seen = []
    action_runner.backend = _thorough_backend(_beijing(httpx.ReadTimeout("timed out")), seen)
    out = run(action_runner, matrix, "search", {"origin": "HNL", "dest": "BJS", "depart": "2026-11-16",
                                                "thorough": True, "carriers": "ke"})
    assert sorted(seen, key=str) == sorted([("PEK", None, None), ("PEK", "-AIRLINES JL KE", None),
                                            ("PEK", "-AIRLINES JL KE PR", None), ("PKX", None, None),
                                            ("PKX", "-AIRLINES CX", None), ("BJS", None, "KE+")], key=str)
    assert 'airports: "HNL to PEK, PKX"' in out
    assert re.search(r'searches: "6 in \d+ s, 1 failed: HNL-PEK, HNL-PEK -AIRLINES JL KE, '
                     r'HNL-PEK -AIRLINES JL KE PR, HNL-PKX, HNL-PKX -AIRLINES CX, KE\+"', out)
    assert 'failed: "HNL-PKX -AIRLINES CX: search failed (ReadTimeout: timed out)"' in out
    rows = [line for line in out.splitlines() if line.startswith("  ") and ",USD" in line]
    assert [r.split(",")[1] for r in rows] == ["USD440.00", "USD568.00", "USD572.00", "USD795.00", "USD900.00"]
    assert rows[0].endswith(f",HNL-PEK -AIRLINES JL KE,SP2.SETP2.{PREFIX}2")
    ke = next(r for r in rows if "KE54" in r)
    assert ke.endswith(f",HNL-PEK,SP1.SETP1.{PREFIX}1")          # kept with the first search that found it
    assert 'by_carrier: PR USD440.00 · KE USD568.00 · CX USD572.00 · JL USD795.00 · AC USD900.00' in out
    assert f"esum matrix detail --search SP2.SETP2.{PREFIX}2 --id 1`" in out
    assert f"esum matrix results --search SP2.SETP2.{PREFIX}2` to page through the search that found" in out
    assert "--carriers '<codes>'" not in out   # it searched its carriers already
    require_site("flightconnections")
    assert "esum flightconnections route --origin HNL --dest PEK" in out
    assert "esum flightconnections connections --origin HNL --dest PEK" in out


def test_thorough_stops_a_pair_when_a_round_finds_no_new_carrier(action_runner, matrix):
    seen = []
    action_runner.backend = _thorough_backend(_beijing(_answer("X2", [], carriers={})), seen)
    out = run(action_runner, matrix, "search", {"origin": "HNL", "dest": "BJS", "depart": "2026-11-16",
                                                "thorough": True})
    assert [k for k in seen if k[0] == "PKX"] == [("PKX", None, None), ("PKX", "-AIRLINES CX", None)]
    assert len([k for k in seen if k[0] == "PEK"]) == 3                # the cap, though round 3 found AC
    assert "failed" not in out and "count: 5" in out


def test_thorough_adds_the_exclusion_to_the_users_extension(action_runner, matrix):
    seen = []
    answers = {(d, "f bc=K" + (f"; {x}" if x else ""), r): v
               for (d, x, r), v in _beijing(_answer("X2", [], carriers={})).items() if r is None}
    action_runner.backend = _thorough_backend(answers, seen)
    run(action_runner, matrix, "search", {"origin": "HNL", "dest": "BJS", "depart": "2026-11-16",
                                          "thorough": True, "extension": "f bc=K"})
    assert ("PEK", "f bc=K; -AIRLINES JL KE", None) in seen


def test_detail_opens_a_merged_row_in_its_own_search(action_runner, fake_http, matrix):
    url = f"{API}/summarize"
    fake_http.routes[url] = json_response(url, load("detail.json"))
    run(action_runner, matrix, "detail", {"search": f"SP2.SETP2.{PREFIX}2", "id": "1"})
    sent = json.loads(fake_http.requests[0].content)
    assert (sent["session"], sent["solutionSet"]) == ("SP2", "SETP2")
    assert sent["inputs"]["solution"] == f"SETP2/{PREFIX}2001"


def test_every_search_failing_is_an_error(action_runner, matrix, capsys):
    import httpx
    action_runner.backend = _thorough_backend({k: httpx.ReadTimeout("timed out") for k in _beijing(None)}, [])
    assert action_runner.cli(["matrix", "search", "--origin", "HNL", "--dest", "BJS", "--depart", "2026-11-16",
                              "--thorough"]) == 1
    assert "all 2 searches failed" in capsys.readouterr().out


@pytest.mark.parametrize("argv, message", [
    (["--carriers", "KE,PR"], "add --thorough"),
    (["--thorough", "--carriers", "KE", "--routing", "KE+"], "can't take --routing"),
    (["--thorough", "--carriers", "KOREAN"], "2-character airline codes"),
])
def test_thorough_usage_errors(action_runner, capsys, argv, message):
    base = ["matrix", "search", "--origin", "HNL", "--dest", "PEK", "--depart", "2026-11-16"]
    assert action_runner.cli([*base, *argv]) == 2
    out = capsys.readouterr().out
    assert message in out
    if message == "add --thorough":
        assert "esum matrix search --origin HNL --dest PEK --depart 2026-11-16 --thorough --carriers KE,PR" in out


def test_thorough_takes_no_legs(action_runner, capsys):
    assert action_runner.cli(["matrix", "search", "--legs", "HNL-PEK:2026-11-16", "--thorough"]) == 2
    assert "not --legs" in capsys.readouterr().out


@needs_site("flightconnections")
def test_a_thin_search_suggests_thorough_and_flightconnections(action_runner, fake_http, matrix):
    url = f"{API}/search"
    fake_http.routes[url] = json_response(url, _answer("B1", [(*JL, "PEK")]))
    out = run(action_runner, matrix, "search", {"origin": "HNL", "dest": "BJS", "depart": "2026-11-16"})
    assert ("esum matrix search --origin HNL --dest BJS --depart 2026-11-16 --thorough` to look for the carriers "
            "Matrix left out: it found only 1 itinerary") in out
    assert "esum flightconnections route --origin HNL --dest PEK` to see which airlines fly HNL-PEK nonstop" in out
    assert "esum flightconnections connections --origin HNL --dest PEK" in out
    assert "esum matrix search --origin HNL --dest BJS --depart 2026-11-16 --thorough --carriers '<codes>'" in out


@needs_site("flightconnections")
def test_a_full_search_names_flightconnections_but_not_thorough(action_runner, fake_http, matrix):
    url = f"{API}/search"
    fake_http.routes[url] = json_response(url, load("search_roundtrip.json"))
    out = run(action_runner, matrix, "search", {"origin": "JFK", "dest": "NRT", "depart": "2027-02-16"})
    assert "--thorough` to look for" not in out
    assert "esum flightconnections route --origin JFK --dest NRT" in out


@needs_site("aerolopa")
def test_detail_points_to_aerolopa_for_long_flights_in_its_window(action_runner, fake_http, matrix, monkeypatch):
    import esum.sites.matrix as m
    url = f"{API}/summarize"
    fake_http.routes[url] = json_response(url, load("detail.json"))
    monkeypatch.setattr(m, "_today", lambda: date(2026, 12, 1))
    out = run(action_runner, matrix, "detail", {"search": TOKEN, "id": "9"})
    hints = [line for line in out.splitlines() if "esum aerolopa" in line]
    assert hints[0] == ("  Run `esum aerolopa schedule --route BOS-NRT --date 2027-02-16 --flight JL7` to see which "
                        "seat configuration JL7 (BOS-NRT) is scheduled on, and its seats")
    assert len(hints) == 2 and "--flight JL56" in hints[1]                # operating flights, two at most
    monkeypatch.setattr(m, "_today", lambda: date(2026, 9, 1))            # over 120 days ahead
    assert "esum aerolopa" not in run(action_runner, matrix, "detail", {"search": TOKEN, "id": "9"})


def test_rule_sections_and_basic_dual_rbd_fare():
    sections = rule_sections(load("rules_basic.json"))
    assert [s["category"] for s in sections][:4] == [0, 2, 4, 5]
    assert sections[1] == {"category": 2, "name": "days and times", "kind": "general rule",
                           "text": "  PERMITTED TUE/WED."}
    assert fare_traits(sections) == ("unbundled (basic) fare: economy ow unbundled fares · dual booking class: "
                                     "UA N also needs K open (the fare sells only while both have seats)")


def test_sell_up_fare_without_a_chart():
    assert fare_traits(rule_sections(load("rules_sellup.json"))) == \
        "sell-up fare: first class sell-up ow non-ref fares"
    assert fare_traits([]) == ""


def test_basic_season_is_a_season_not_a_basic_fare():
    def traits(title: str) -> str:
        return fare_traits([{"category": 0, "text": title + "   B"}])
    assert traits("BASIC SEASON MIDWEEK ECONOMY SELL-UP RT NON-REF FARES") == (
        "sell-up fare: basic season midweek economy sell-up rt non-ref fares")
    assert traits("BASIC SEASON ECONOMY RT UNBUNDLED FARES") == (
        "unbundled (basic) fare: basic season economy rt unbundled fares")


def test_a_fare_title_wrapped_onto_a_second_line():
    def traits(*lines: str) -> str:
        return fare_traits([{"category": 0, "text": chr(10).join(lines)}])
    rest = ("Between AREA 1 and AREA 3 FOR ROUND TRIP FARES", "FOR ADULT")
    assert traits("BASIC SEASON MIDWEEK ECONOMY RT           K", "UNBUNDLED FARES", *rest) == (
        "unbundled (basic) fare: basic season midweek economy rt unbundled fares")
    assert traits("BASIC SEASON MIDWEEK ECONOMY SELL-UP RT   B", "NON-REF FARES", *rest) == (
        "sell-up fare: basic season midweek economy sell-up rt non-ref fares")
    assert traits("BASIC SEASON MIDWEEK ECONOMY RT           K", " APPLIES FOR ROUND TRIP FARES") == ""


def test_detail_marks_each_fare_from_its_rules(action_runner, fake_http, matrix):
    url = f"{API}/summarize"
    fake_http.routes[url] = [json_response(url, load("detail.json")), json_response(url, load("rules_basic.json")),
                             json_response(url, load("rules_sellup.json"))]
    out = run(action_runner, matrix, "detail", {"search": TOKEN, "id": "9"})
    sent = [json.loads(r.content) for r in fake_http.requests]
    assert [(s["summarizerSet"], s["inputs"].get("fareKeys")) for s in sent] == [
        ("viewDetails", None), ("viewRules", "0/0"), ("viewRules", "0/1")]
    assert "      unbundled (basic) fare: economy ow unbundled fares · dual booking class: UA N also needs K" in out
    assert "      sell-up fare: first class sell-up ow non-ref fares" in out
    assert f"esum matrix rules --search {TOKEN} --id 9" in out


def test_detail_stands_when_the_rules_fail(action_runner, fake_http, matrix):
    url = f"{API}/summarize"
    fake_http.routes[url] = [json_response(url, load("detail.json")),
                             json_response(url, {"error": {"message": "Internal server error.", "type": "internal"}})]
    out = run(action_runner, matrix, "detail", {"search": TOKEN, "id": "9"})
    assert "ILX67IN4  AA NYC-TYO" in out and "unbundled" not in out


def test_rules_action_prints_chosen_categories(action_runner, fake_http, matrix):
    url = f"{API}/summarize"
    fake_http.routes[url] = [json_response(url, load("detail.json")), json_response(url, load("rules_basic.json"))]
    out = run(action_runner, matrix, "rules", {"search": TOKEN, "id": "9", "fare": "1", "category": "2,16,6"})
    assert len(fake_http.requests) == 2   # one fare asked for: one rules request
    assert "Fare ILX67IN4 · AA NYC-TYO · booking class I I" in out
    filed = next(line for line in out.splitlines() if line.startswith("  also filed: "))
    assert filed.startswith("  also filed: 0 general notes and booking classes (")
    assert " · 31 voluntary changes (" in filed and " lines) · 33 voluntary refunds (" in filed
    assert " 2 days and times" not in filed and " 16 penalties" not in filed
    assert "  [2] days and times (general rule)\n      PERMITTED TUE/WED." in out
    assert "[16] penalties" in out and "[5] advance" not in out
    assert "  not filed: 6" in out
    assert f"esum matrix rules --search {TOKEN} --id 9 --fare 1 --category all" in out


def test_rules_rejects_an_unknown_fare(action_runner, fake_http, capsys):
    url = f"{API}/summarize"
    fake_http.routes[url] = json_response(url, load("detail.json"))
    assert action_runner.cli(["matrix", "rules", "--search", TOKEN, "--id", "9", "--fare", "ZZZ"]) == 2
    assert "this itinerary's fares are 1 ILX67IN4, 2 ILX67IN4" in capsys.readouterr().out


# ---- the bridge to what the airline sells on the flights (gflights fares) ----------------------

@needs_site("gflights")
def test_detail_points_to_gflights_fares_for_its_flights(action_runner, fake_http, matrix):
    url = f"{API}/summarize"
    fake_http.routes[url] = json_response(url, load("detail.json"))
    out = run(action_runner, matrix, "detail", {"search": TOKEN, "id": "9"})
    # codeshares by their operating numbers (JL7 for AA8475); the cabin of each slice's longest flight
    assert ("  Run `esum gflights fares --flights JFK-BOS:2027-02-16:AA4645+BOS-NRT:2027-02-16:JL7 "
            "NRT-ORD:2027-02-23:JL56+ORD-JFK:2027-02-23:AA1529 --cabin business` to see what the airline") in out


def test_gflights_fares_arguments_follow_the_party_and_the_currency():
    from esum.sites.matrix.parse import google_fares
    data = load("detail.json")
    details = data["bookingDetails"]
    details["displayTotal"] = "JPY1100000"
    details["tickets"][0]["pricings"][0]["ext"]["pax"] = {"adults": 2}
    del details["itinerary"]["slices"][1]
    for seg in details["itinerary"]["slices"][0]["segments"]:
        seg["bookingInfos"][0]["cabin"] = "COACH"
    assert google_fares(booking(data).data) == {
        "flights": ["JFK-BOS:2027-02-16:AA4645+BOS-NRT:2027-02-16:JL7"], "cabin": None, "adults": 2,
        "currency": "JPY"}


def _with_a_child(details: dict) -> None:
    pricing = details["tickets"][0]["pricings"][0]
    child = json.loads(json.dumps(pricing))
    child["ext"]["pax"] = {"children": 1}
    details["tickets"][0]["pricings"].append(child)


def _economy_home(details: dict) -> None:
    for seg in details["itinerary"]["slices"][1]["segments"]:
        seg["bookingInfos"][0]["cabin"] = "COACH"


def _third_slice(details: dict) -> None:
    details["itinerary"]["slices"].append(details["itinerary"]["slices"][0])


def _stop_on_the_way(details: dict) -> None:
    seg = details["itinerary"]["slices"][0]["segments"][1]
    seg["legs"] = [{"destination": {"code": "ANC"}}, {"destination": {"code": "NRT"}}]


@pytest.mark.parametrize("change", [_with_a_child, _economy_home, _third_slice, _stop_on_the_way])
def test_detail_leaves_gflights_fares_out_for_a_trip_it_cannot_name(action_runner, fake_http, matrix, change):
    data = load("detail.json")
    change(data["bookingDetails"])
    url = f"{API}/summarize"
    fake_http.routes[url] = json_response(url, data)
    out = run(action_runner, matrix, "detail", {"search": TOKEN, "id": "9"})
    assert "esum matrix rules" in out and "esum gflights" not in out


def test_detail_stands_without_the_sites_its_steps_point_to(action_runner, fake_http, matrix, monkeypatch):
    """A distribution can leave aerolopa or gflights out: their steps are dropped, the detail prints."""
    import esum.sites.matrix as m
    from esum.core import registry
    url = f"{API}/summarize"
    fake_http.routes[url] = json_response(url, load("detail.json"))
    monkeypatch.setattr(m, "_today", lambda: date(2026, 12, 1))
    here = registry.get_registry()
    monkeypatch.setattr(here, "sites", {k: v for k, v in here.sites.items() if k not in ("aerolopa", "gflights")})
    out = run(action_runner, matrix, "detail", {"search": TOKEN, "id": "9"})
    assert "Itinerary 9: JFK" in out and "esum matrix rules" in out
    assert "esum aerolopa" not in out and "esum gflights" not in out
