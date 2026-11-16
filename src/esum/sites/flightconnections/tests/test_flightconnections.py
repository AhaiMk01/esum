"""FlightConnections: offline tests on scrubbed, trimmed pages captured 2026-11-13.

Expected values are what the rendered pages showed when they were captured (the route page's
summary box and timetable, the airport pages' lists and sentences, the airline page's lists)."""

from __future__ import annotations

import io
import json
import re
from datetime import date
from pathlib import Path

import pytest

import esum.sites.flightconnections as fc
from esum import Options
from support import needs_site
from esum.core.registry import Registry
from esum.sites.flightconnections.parse import (PageChanged, airline, airport, route, route_airlines,
                                                schedule_answer, weekdays)
from esum.transport import Response

FIX = Path(__file__).parent / "fixtures"
BASE = "https://www.flightconnections.com"


def text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def html(url: str, name: str, status: int = 200) -> Response:
    return Response(status, url, {"content-type": "text/html; charset=UTF-8"}, text(name).encode())


# ---------------------------------------------------------------- parsers


def test_route_summary_and_airlines():
    r = route(text("route-jfk-lhr.html"), f"{BASE}/flights-from-jfk-to-lhr")
    assert (r.origin.code, r.origin.city, r.origin.airport, r.origin.country) == (
        "JFK", "New York City", "John F. Kennedy International Airport", "United States")
    assert (r.dest.code, r.dest.city) == ("LHR", "London Heathrow")
    assert r.nonstop and r.distance == "3,440 miles · (5,536 km)" and r.time == "7 hours and 5 minutes"
    assert r.weekly == 131
    assert r.alliances == ["Oneworld", "SkyTeam"]            # Star Alliance is struck through
    assert r.classes == ["Economy", "Premium Economy", "Business Class", "First Class"]
    assert len(r.aircraft) == 11 and r.aircraft[0] == "Airbus A321neo" and r.aircraft[-1] == "Boeing 787-9"
    assert [(a["code"], a["name"]) for a in r.airlines] == [
        ("AA", "American Airlines"), ("BA", "British Airways"), ("DL", "Delta"), ("B6", "JetBlue"),
        ("VS", "Virgin Atlantic")]
    assert r.month == date(2026, 11, 1) and r.ids == ("3799", "173")


def test_route_timetable_row():
    r = route(text("route-jfk-lhr.html"), "u")
    first = r.flights[0]
    assert {k: first[k] for k in ("depart", "arrive", "airline", "airline_name", "alliance", "flight", "aircraft",
                                  "classes", "dates", "days")} == {
        "depart": "08:00", "arrive": "20:10", "airline": "VS", "airline_name": "Virgin Atlantic",
        "alliance": "SkyTeam", "flight": "VS 26", "aircraft": "Airbus A330-900neo",
        "classes": "economy premium business",                # First is struck through
        "dates": "1-30", "days": "daily"}
    ba = r.flights[1]
    assert (ba["flight"], ba["depart"], ba["arrive"], ba["aircraft"], ba["classes"]) == (
        "BA 178", "08:05", "20:00", "Boeing 777", "economy premium business first")
    jetblue = [f for f in r.flights if f["airline"] == "B6"][0]
    assert jetblue["dates"] == "1,6,8,13,15,20,22,27,29" and jetblue["days"] == "fr su"


def test_route_without_nonstop_lists_stopovers():
    r = route(text("route-aey-aga.html"), "u")
    assert not r.nonstop and not r.flights
    assert [(s.via, s.name, s.time) for s in r.stopovers] == [
        ("MAN", "Manchester", "6h 35"), ("LGW", "London Gatwick", "7h 00"), ("ZRH", "Zürich", "7h 10")]
    man = r.stopovers[0]
    assert man.codes == ["U2", "LS", "TOM", "RK"]
    one, two = man.legs
    assert (one.origin, one.dest, one.miles, one.time) == ("AEY", "MAN", 1007, "2h 35")
    assert [(a.name, a.code, a.days, a.note) for a in one.airlines] == [("easyJet", "U2", "mo th fr", "Ends in March")]
    assert (two.origin, two.dest, two.miles, two.time) == ("MAN", "AGA", 1630, "4h 00")
    assert [(a.name, a.code, a.days) for a in two.airlines][:3] == [
        ("easyJet", "U2", "daily"), ("Jet2", "LS", "tu th su"), ("TUI Airways", "TOM", "mo th")]
    assert two.airlines[0].note == "Ends in September"


def test_another_month_answer():
    month, rows = schedule_answer(text("schedule-jfk-lhr-2026-12.json"))
    assert month == date(2026, 12, 1)
    assert rows[0]["flight"] == "VS 26" and rows[0]["dates"] == "1-31" and rows[0]["days"] == "daily"
    assert {r["airline"] for r in rows} == {"VS", "B6"}
    with pytest.raises(PageChanged):
        schedule_answer("<html>not json</html>")


def test_airport_from_page():
    a = airport(text("airport-from-jfk.html"), "u", "from")
    assert (a.code, a.city, a.name) == ("JFK", "New York City", "John F. Kennedy International Airport")
    assert a.summary == "has non-stop passenger flights scheduled to 190 destinations in 79 countries"
    assert len(a.nonstop) == 190
    lax = a.nonstop[0]
    assert (lax["code"], lax["name"], lax["country"], lax["flights"], lax["miles"]) == (
        "LAX", "Los Angeles", "United States", 871, 2468)
    assert [(x["code"], x["alliance"]) for x in lax["airlines"]] == [("AA", "Oneworld"), ("DL", "SkyTeam"), ("B6", "")]
    by = {r["code"]: r for r in a.nonstop}
    assert [x["name"] for x in by["FRA"]["airlines"]] == ["Condor", "Delta", "Lufthansa", "Singapore Airlines"]
    assert [x["code"] for x in by["HND"]["airlines"]] == ["AA", "NH", "JL"]
    assert by["GDL"]["note"] == "This is a seasonal route that starts in December and ends in January"
    assert by["AGS"]["flights"] == 0 and by["AGS"]["note"].endswith("It will start in April 2027")
    assert by["BDL"]["miles"] == 106 and by["SIN"]["miles"] == 9526
    assert [(r["code"], r["country"]) for r in a.onestop[:2]] == [("DUS", "Germany"), ("ORY", "France")]


def test_airport_to_page_sentences_name_the_origin():
    a = airport(text("airport-to-lhr.html"), "u", "to")
    assert a.code == "LHR" and len(a.nonstop) == 224
    assert a.summary.startswith("there are 224 airports around the world that have direct flights to London Heathrow")
    jfk = a.nonstop[0]
    assert (jfk["code"], jfk["flights"], jfk["miles"]) == ("JFK", 565, None)
    by = {r["code"]: r for r in a.nonstop}
    # `From Cologne-Bonn, the only airline with direct flights is British Airways (Oneworld).`
    assert [(x["name"], x["code"], x["alliance"]) for x in by["CGN"]["airlines"]] == [
        ("British Airways", "BA", "Oneworld")]
    assert [x["code"] for x in by["FRA"]["airlines"]] == ["BA", "LH", "VL"]


def test_airport_page_names_the_airlines_of_every_sentence_form():
    """LAX words some sentences `Airlines that operate direct flights to <X> are A, B and C.`"""
    a = airport(text("airport-from-lax.html"), "u", "from")
    by = {r["code"]: r for r in a.nonstop}
    assert [(x["code"], x["alliance"]) for x in by["SFO"]["airlines"]] == [
        ("AS", "Oneworld"), ("AA", "Oneworld"), ("DL", "SkyTeam"), ("F9", ""), ("WN", ""), ("UA", "Star Alliance")]
    assert [x["name"] for x in by["LAS"]["airlines"]] == [
        "Alaska", "American Airlines", "Delta", "Frontier Airlines", "JetSuiteX", "Southwest Airlines",
        "United Airlines"]
    assert [x["name"] for x in by["YVR"]["airlines"]] == [
        "Air Canada", "American Airlines", "Delta", "Flair Airlines", "United Airlines", "WestJet"]
    assert (by["SFO"]["flights"], by["SFO"]["miles"]) == (1224, 337)
    into = {r["code"]: r for r in airport(text("airport-to-lax.html"), "u", "to").nonstop}
    # `From Vancouver, airlines that operate direct flights are Air Canada (Star Alliance), …`
    assert [x["code"] for x in into["YVR"]["airlines"]] == ["AC", "AA", "DL", "F8", "UA", "WS"]


def test_airport_page_tells_apart_destinations_with_the_same_name():
    """BOS flies to Orlando MCO and SFB, Tampa TPA and PIE; Allegiant's SFB and PIE routes are only
    announced. ATL flies to Panama City ECP (Florida) and PTY (Panama)."""
    bos = {r["code"]: r for r in airport(text("airport-from-bos.html"), "u", "from").nonstop}
    assert [x["code"] for x in bos["MCO"]["airlines"]] == ["AA", "DL", "F9", "B6", "WN"] and bos["MCO"]["note"] == ""
    assert [x["code"] for x in bos["TPA"]["airlines"]] == ["AA", "DL", "B6"] and bos["TPA"]["note"] == ""
    for idle in ("SFB", "PIE"):
        assert [x["name"] for x in bos[idle]["airlines"]] == ["Allegiant"]
        assert bos[idle]["note"].startswith("This route has been announced recently") and bos[idle]["flights"] == 0
    # Washington DCA and IAD: two sentences with nothing to tell which airport each is about
    for code in ("DCA", "IAD"):
        assert [x["code"] for x in bos[code]["airlines"]] == ["AA", "DL", "B6", "UA"]
        assert bos[code]["note"] == "airlines of DCA and IAD together: the page doesn't say which airport each flies"
    # ORD: Jackson MKL has 26 flights this month, JAC none; the route that `will start in December` is JAC's
    ord_ = {r["code"]: r for r in airport(text("airport-from-ord.html"), "u", "from").nonstop}
    assert [x["code"] for x in ord_["JAC"]["airlines"]] == ["AA", "UA"]
    assert ord_["JAC"]["note"] == "This route is not operational yet but will start in December"
    assert [x["name"] for x in ord_["MKL"]["airlines"]] == ["Key Lime Air"]
    atl = {r["code"]: r for r in airport(text("airport-from-atl.html"), "u", "from").nonstop}
    assert [x["code"] for x in atl["PTY"]["airlines"]] == ["CM", "DL"]
    assert [x["code"] for x in atl["ECP"]["airlines"]] == ["DL"]


def _pages(fake_http, pages: tuple[tuple[str, str], ...]) -> None:
    for path, name in pages:
        fake_http.routes[BASE + path] = html(BASE + path, name)


# the site has a LHR-LAX page; it is left out here, as for a pair the site has no page for
LHR_LAX = (("/flights-from-airport-lhr", "airport-from-lhr.html"), ("/flights-to-airport-lax", "airport-to-lax.html"),
           ("/flights-from-airport-lax", "airport-from-lax.html"))
JFK_LHR = (("/flights-from-jfk-to-lhr", "route-jfk-lhr.html"), ("/flights-from-airport-jfk", "airport-from-jfk.html"),
           ("/flights-to-airport-lhr", "airport-to-lhr.html"), ("/flights-from-airport-lhr", "airport-from-lhr.html"))


def test_paired_hubs_name_the_airlines_of_every_sentence_form(action_runner, fake_http, site, nov13):
    _pages(fake_http, LHR_LAX)
    rows = {r["via"]: r for r in json.loads(run(action_runner, site, "connections", {"origin": "LHR", "dest": "LAX"},
                                                json_out=True))["data"]["connections"]}
    assert rows["SFO"]["leg1"] == "BA UA VS" and rows["SFO"]["leg2"] == "AS AA DL F9 WN UA"
    star = json.loads(run(action_runner, site, "connections", {"origin": "LHR", "dest": "LAX", "alliance": "star"},
                          json_out=True))["data"]["connections"]
    assert {r["via"]: r["leg2"] for r in star if r["via"] in ("SFO", "YVR")} == {"SFO": "UA", "YVR": "AC UA"}


def test_paired_alliances_count_only_legs_flying_this_month(action_runner, fake_http, site, nov13):
    """VIE-LAX (Austrian) has 0 flights this month; LHR-VIE has Austrian too."""
    _pages(fake_http, LHR_LAX)
    data = json.loads(run(action_runner, site, "connections", {"origin": "LHR", "dest": "LAX"}, json_out=True))["data"]
    rows = {r["via"]: r for r in data["connections"]}
    assert rows["VIE"]["alliances"] == "" and rows["VIE"]["note"].startswith("leg 2: This is a seasonal route")
    assert rows["SFO"]["alliances"] == "Oneworld, SkyTeam, Star Alliance"
    assert data["alliances"].endswith("only where both legs fly this month")
    out = run(action_runner, site, "connections", {"origin": "LHR", "dest": "LAX"})
    above = [line for line in out.split("connections[")[0].splitlines() if ":" in line]
    assert any(line.startswith("alliances:") and line.rstrip('"').endswith("only where both legs fly this month")
               for line in above)
    assert not any("…" in line for line in above)               # every line above the rows prints whole


def test_a_leg_with_under_one_flight_a_week_gets_a_note(action_runner, fake_http, site, nov13):
    """ANU-LHR: 2 flights this month, and the site says nothing about it."""
    _pages(fake_http, JFK_LHR)
    rows = {r["via"]: r for r in json.loads(run(action_runner, site, "connections",
                                                {"origin": "JFK", "dest": "LHR", "detours": True, "limit": 300},
                                                json_out=True))["data"]["connections"]}
    assert rows["ANU"]["flights"] == "30/2" and rows["ANU"]["note"] == "leg 2: only 2 flights this month"
    fake_http.routes[BASE + "/flights-to-airport-lhr"] = html(BASE + "/flights-to-airport-lhr", "airport-to-lhr.html")
    into = {r["code"]: r for r in json.loads(run(action_runner, site, "destinations",
                                                 {"airport": "LHR", "direction": "to", "limit": 300},
                                                 json_out=True))["data"]["destinations"]}
    assert into["CSX"]["note"] == "only 1 flight this month" and into["JFK"]["note"] == ""


def test_paired_hubs_far_off_the_nonstop_are_dropped_unless_asked(action_runner, fake_http, site, nov13):
    """JFK-LHR nonstop is 3,440 mi; via LAX is 2,468 + 5,440 = 7,908 mi."""
    _pages(fake_http, JFK_LHR)
    data = json.loads(run(action_runner, site, "connections", {"origin": "JFK", "dest": "LHR", "limit": 300},
                          json_out=True))["data"]
    rows = {r["via"]: r for r in data["connections"]}
    assert "LAX" not in rows and rows["DUB"]["detour"] == f"{round(100 * (3169 + 278) / 3440) - 100}%"
    assert data["detour"].startswith("over the nonstop distance, 3,440 mi")
    assert "--detours" in data["dropped"]
    kept = {r["via"]: r for r in json.loads(run(action_runner, site, "connections",
                                                {"origin": "JFK", "dest": "LHR", "detours": True, "limit": 300},
                                                json_out=True))["data"]["connections"]}
    assert kept["LAX"]["detour"] == "130%"
    out = run(action_runner, site, "connections", {"origin": "JFK", "dest": "LHR"})
    assert "esum flightconnections connections --origin JFK --dest LHR --detours" in out


def test_paired_detour_without_a_nonstop_is_over_the_shortest_pair(action_runner, fake_http, site, nov13):
    """No LHR-LAX page here: the shortest pair is via LAS, 5,213 + 235 = 5,448 mi."""
    _pages(fake_http, LHR_LAX)
    data = json.loads(run(action_runner, site, "connections", {"origin": "LHR", "dest": "LAX"}, json_out=True))["data"]
    rows = {r["via"]: r for r in data["connections"]}
    assert data["detour"].startswith("over the shortest pair, via LAS, 5,448 mi")
    assert rows["LAS"]["detour"] == "0%" and rows["SFO"]["detour"] == "4%"
    assert "HND" not in rows                                  # 5,956 + 5,473 mi


def test_help_after_a_filter_fits_the_result(action_runner, fake_http, site, nov13):
    _pages(fake_http, LHR_LAX)
    # WestJet flies YVR-LAX only, so no hub has it on both legs
    out = run(action_runner, site, "connections", {"origin": "LHR", "dest": "LAX", "airline": "WS"})
    assert "esum flightconnections connections --origin LHR --dest LAX` to" in out     # the same, without the filter
    fake_http.routes[f"{BASE}/flights-from-airport-jfk"] = html(f"{BASE}/flights-from-airport-jfk", "airport-from-jfk.html")
    out = run(action_runner, site, "destinations", {"airport": "JFK", "airline": "B6"})
    assert "B6" in out and "airline --code B6" not in out    # rows matched: the code needs no checking


def test_sentence_forms():
    catalog = {"british airways": ["BA"], "delta": ["DL"], "jetblue": ["B6"], "trinidad and tobago air": ["TT"]}
    page = ('<div class="domestic-flights"><p>To <strong>Boston</strong> and <strong>Bozeman</strong>, you can fly '
            'with Delta (SkyTeam) and JetBlue.<br>The only airline with direct flights to <strong>Anchorage</strong>'
            ' is Trinidad and Tobago Air. This is a seasonal route that started in June and ends in August.</p></div>')
    found = route_airlines(page, catalog)
    assert found[0][0] == ["Boston", "Bozeman"] and [a["code"] for a in found[0][1]] == ["DL", "B6"]
    assert [a["code"] for a in found[1][1]] == ["TT"]
    assert found[1][2] == "This is a seasonal route that started in June and ends in August"


def test_airline_page():
    b = airline(text("airline-b6.html"), "u", "b6")
    assert (b.code, b.name) == ("B6", "JetBlue")
    assert b.serves == "serves 68 domestic destinations and 46 international destinations in 32 countries"
    assert b.departs == "departs from 114 airports in 32 countries"
    assert len(b.destinations) == 114 and len(b.origins) == 114
    assert b.destinations[0] == {"code": "AUA", "name": "Aruba", "country": "Aruba"}
    assert len(b.routes) == 200
    assert b.routes[0] == {"origin": "LAX", "dest": "JFK", "url": f"{BASE}/flights-from-lax-to-jfk",
                           "title": "Los Angeles to New York City"}


def test_weekdays():
    assert weekdays(date(2026, 11, 1), list(range(1, 31))) == "daily"
    assert weekdays(date(2026, 11, 1), [2, 4, 9]) == "mo we"
    assert weekdays(date(2026, 11, 1), list(range(1, 21))) == "daily"       # every day it is listed for
    # every weekday, but with gaps: not daily
    assert weekdays(date(2026, 11, 1), [1, 2, 3, 4, 5, 6, 7, 9, 11]) == "mo tu we th fr sa su"
    assert weekdays(None, [1]) == ""


# ---------------------------------------------------------------- actions


@pytest.fixture(autouse=True)
def no_spacing(monkeypatch):
    """The site spaces live requests 3 s apart; offline ones need no spacing."""
    monkeypatch.setitem(fc.flightconnections.extras, "min_interval", 0)


@pytest.fixture
def site():
    return Registry.discover(custom_root=None).get("flightconnections")


@pytest.fixture
def nov13(monkeypatch):
    monkeypatch.setattr(fc, "_today", lambda: date(2026, 11, 13))


def run(action_runner, target, action: str, kwargs: dict, *, json_out: bool = False, fields: str | None = None) -> str:
    out = io.StringIO()
    action_runner(target, target.all_actions[action], kwargs, Options(json=json_out, fields=fields), stream=out)
    printed = out.getvalue()
    if json_out and json.loads(printed).get("spilled"):        # a large result goes to a file
        return Path(json.loads(printed)["path"]).read_text(encoding="utf-8")
    return printed


def test_route_action_counts_from_today(action_runner, fake_http, site, nov13):
    url = f"{BASE}/flights-from-jfk-to-lhr"
    fake_http.routes[url] = html(url, "route-jfk-lhr.html")
    data = json.loads(run(action_runner, site, "route", {"origin": "jfk", "dest": "LHR"}, json_out=True))["data"]
    assert data["per_week"] == 131 and data["counted"] == "2026-11-13 to 2026-11-30"
    assert "note" not in data
    weekly = {r["code"]: r["per_week"] for r in data["airlines"]}
    assert set(weekly) == {"AA", "BA", "DL", "B6", "VS"}
    assert abs(sum(weekly.values()) - 131) <= 3          # the site's own count of the same timetable
    assert "Boeing 777" in next(r for r in data["airlines"] if r["code"] == "BA")["aircraft"]


def test_route_action_says_when_the_page_shows_a_later_month(action_runner, fake_http, site, monkeypatch):
    """The site shows a route's first month with flights; a sparse one must not round to 0 a week."""
    monkeypatch.setattr(fc, "_today", lambda: date(2026, 10, 5))
    url = f"{BASE}/flights-from-jfk-to-lhr"
    fake_http.routes[url] = html(url, "route-jfk-lhr.html")
    data = json.loads(run(action_runner, site, "route", {"origin": "JFK", "dest": "LHR"}, json_out=True))["data"]
    assert data["note"].startswith("no flights this month: the site shows 2026-11")
    assert data["counted"] == "2026-11-01 to 2026-11-30"
    assert all(r["per_week"] > 0 for r in data["airlines"])
    assert any(r["per_week"] != int(r["per_week"]) for r in data["airlines"])


def test_route_action_without_nonstop_points_to_connections(action_runner, fake_http, site):
    url = f"{BASE}/flights-from-aey-to-aga"
    fake_http.routes[url] = html(url, "route-aey-aga.html")
    out = run(action_runner, site, "route", {"origin": "AEY", "dest": "AGA"})
    assert "no nonstop flights from AEY to AGA; the site lists 3 one-stop options" in out
    assert "esum flightconnections connections --origin AEY --dest AGA" in out


def test_schedule_action_other_month_posts_the_calendar(action_runner, fake_http, site, nov13):
    url = f"{BASE}/flights-from-jfk-to-lhr"
    fake_http.routes[url] = html(url, "route-jfk-lhr.html")
    post = f"{BASE}/route_schedule.php"
    fake_http.routes[post] = Response(200, post, {"content-type": "application/json"},
                                      text("schedule-jfk-lhr-2026-12.json").encode())
    data = json.loads(run(action_runner, site, "schedule", {"origin": "JFK", "dest": "LHR", "date_": "2026-12-05",
                                                          "airline": "jetblue"}, json_out=True))["data"]
    sent = [r for r in fake_http.requests if r.method == "POST"][0]
    assert b"dep=3799" in sent.content and b"des=173" in sent.content and b"ym=2026-12" in sent.content
    assert data["month"] == "2026-12" and data["flights"]
    assert {r["airline"] for r in data["flights"]} == {"B6"}


def test_schedule_this_month_leaves_out_past_rows(action_runner, fake_http, site, nov13):
    url = f"{BASE}/flights-from-jfk-to-lhr"
    fake_http.routes[url] = html(url, "route-jfk-lhr.html")
    data = json.loads(run(action_runner, site, "schedule", {"origin": "JFK", "dest": "LHR", "limit": 200},
                          json_out=True))["data"]
    assert data["from"].startswith("2026-11-13")
    assert len(fake_http.requests) == 1
    assert all(any(int(d) >= 13 for d in r["dates"].replace("-", ",").split(",")) for r in data["flights"])


def test_connections_site_options(action_runner, fake_http, site):
    url = f"{BASE}/flights-from-aey-to-aga"
    fake_http.routes[url] = html(url, "route-aey-aga.html")
    data = json.loads(run(action_runner, site, "connections", {"origin": "AEY", "dest": "AGA"}, json_out=True))["data"]
    assert [r["via"] for r in data["connections"]] == ["MAN", "LGW", "ZRH"]
    assert data["connections"][0]["leg1"] == "AEY-MAN 1007 mi 2h 35: U2 (mo th fr, Ends in March)"


def test_connections_paired_from_airport_pages(action_runner, fake_http, site, nov13):
    for path, name in (("/flights-from-jfk-to-lhr", "route-jfk-lhr.html"),
                       ("/flights-from-airport-jfk", "airport-from-jfk.html"),
                       ("/flights-to-airport-lhr", "airport-to-lhr.html"),
                       ("/flights-from-airport-lhr", "airport-to-lhr.html")):   # miles back: none in this page
        fake_http.routes[BASE + path] = html(BASE + path, name)
    data = json.loads(run(action_runner, site, "connections", {"origin": "JFK", "dest": "LHR", "sort": "flights",
                                                             "limit": 200}, json_out=True))["data"]
    rows = {r["via"]: r for r in data["connections"]}
    assert data["source"].startswith("paired")
    assert "JFK" not in rows and "LHR" not in rows
    assert rows["FRA"]["leg1"] == "DE DL LH SQ" and rows["FRA"]["leg2"] == "BA LH VL"
    assert rows["FRA"]["alliances"] == "Star Alliance" and rows["FRA"]["flights"].startswith("92/")
    oneworld = json.loads(run(action_runner, site, "connections", {"origin": "JFK", "dest": "LHR",
                                                                 "alliance": "oneworld", "limit": 200},
                              json_out=True))["data"]["connections"]
    assert oneworld and all("Oneworld" in r["alliances"] for r in oneworld)


def test_destinations_by_airline_and_country(action_runner, fake_http, site):
    url = f"{BASE}/flights-from-airport-jfk"
    fake_http.routes[url] = html(url, "airport-from-jfk.html")
    data = json.loads(run(action_runner, site, "destinations", {"airport": "JFK", "airline": "Lufthansa",
                                                              "limit": 200}, json_out=True))["data"]
    assert [r["code"] for r in data["destinations"]] == ["FRA", "MUC"]
    every = json.loads(run(action_runner, site, "destinations", {"airport": "JFK", "limit": 300},
                           json_out=True))["data"]["destinations"]
    idle = [r for r in every if r["flights"] == 0]
    assert idle and all(r["note"] for r in idle)          # the site's reason, or that it doesn't fly now
    assert any(r["note"].startswith("no flights this month") for r in idle)
    germany = json.loads(run(action_runner, site, "destinations", {"airport": "JFK", "country": "germany",
                                                                 "sort": "distance"}, json_out=True))["data"]
    assert [r["code"] for r in germany["destinations"]] == ["FRA", "MUC", "BER"] or germany["total"] == 3


def test_airline_action_routes_from_one_airport(action_runner, fake_http, site):
    url = f"{BASE}/route-map-b6"
    fake_http.routes[url] = html(url, "airline-b6.html")
    data = json.loads(run(action_runner, site, "airline", {"code": "b6", "show": "routes", "origin": "JFK"},
                          json_out=True))["data"]
    assert data["routes"] and all(r["origin"] == "JFK" for r in data["routes"])


def test_airline_by_its_full_name_when_the_site_uses_a_shorter_one(action_runner, fake_http, site):
    """The site calls Alaska Airlines `Alaska`; JetBlue Airways stands in here."""
    catalog = (b'<a href="/route-map-jetblue-b6" class="checkbox-option airline-option btn" data-name="JetBlue" '
               b'data-iata="B6">JetBlue (B6)</a><a href="/route-map-jetblue-cargo-zz" class="checkbox-option '
               b'airline-option btn" data-name="JetBlue Cargo" data-iata="ZZ">JetBlue Cargo (ZZ)</a>')
    fake_http.routes[f"{BASE}/airlines"] = Response(200, f"{BASE}/airlines", {"content-type": "text/html"}, catalog)
    fake_http.routes[f"{BASE}/route-map-b6"] = html(f"{BASE}/route-map-b6", "airline-b6.html")
    data = json.loads(run(action_runner, site, "airline", {"code": "JetBlue Airways"}, json_out=True))["data"]
    assert data["airline"] == "JetBlue (B6)"


def test_errors(action_runner, fake_http, site, capsys, nov13):
    registry = Registry.discover(custom_root=None)
    assert action_runner.cli(["flightconnections", "route", "--origin", "JFKX", "--dest", "LHR"],
                             registry=registry) == 2
    assert "help:" in capsys.readouterr().out
    # a past month has no timetable on the site
    assert action_runner.cli(["flightconnections", "schedule", "--origin", "JFK", "--dest", "LHR", "--month",
                              "2026-10"], registry=registry) == 2
    assert "past month" in capsys.readouterr().out and not fake_http.requests
    # an unknown route page is a 404: no such item
    assert action_runner.cli(["flightconnections", "route", "--origin", "JFK", "--dest", "XXX"],
                             registry=registry) == 1
    out = capsys.readouterr().out
    assert "no page for JFK to XXX" in out and "help:" in out
    # the bot check's empty 202 is a refusal, not a layout change
    url = f"{BASE}/flights-from-jfk-to-cdg"
    fake_http.routes[url] = Response(202, url, {"x-amzn-waf-action": "challenge"}, b"")
    assert action_runner.cli(["flightconnections", "route", "--origin", "JFK", "--dest", "CDG"],
                             registry=registry) == 1
    assert "bot check" in capsys.readouterr().out



def test_route_pairs_each_flight_number_with_its_departures(action_runner, fake_http, site, nov13):
    url = f"{BASE}/flights-from-jfk-to-lhr"
    fake_http.routes[url] = html(url, "route-jfk-lhr.html")
    data = json.loads(run(action_runner, site, "route", {"origin": "JFK", "dest": "LHR"}, json_out=True))["data"]
    rows = {r["code"]: r for r in data["airlines"]}
    # B6 1107 leaves 08:45, 09:26 or 09:30 by date; B6 7 at 21:03 or 21:05 (Nov 13-30)
    assert rows["B6"]["departures"] == ["B6 1107 08:45/09:26/09:30", "B6 7 21:03/21:05"]
    assert rows["DL"]["departures"] == ["DL 1 20:00", "DL 3 22:15"]
    out = run(action_runner, site, "route", {"origin": "JFK", "dest": "LHR"}, fields="departures")
    assert "B6 1107 08:45/09:26/09:30; B6 7 21:03/21:05" in out


def test_results_say_how_to_read_them(action_runner, fake_http, site, nov13):
    for path, name in (("/flights-from-jfk-to-lhr", "route-jfk-lhr.html"),
                       ("/flights-from-airport-jfk", "airport-from-jfk.html"),
                       ("/flights-to-airport-lhr", "airport-to-lhr.html"),
                       ("/flights-from-airport-lhr", "airport-to-lhr.html"),
                       ("/flights-from-aey-to-aga", "route-aey-aga.html"),
                       ("/route-map-b6", "airline-b6.html")):
        fake_http.routes[BASE + path] = html(BASE + path, name)

    def note(action: str, **kwargs) -> str:
        return json.loads(run(action_runner, site, action, kwargs, json_out=True))["data"]["note"]

    route_ = json.loads(run(action_runner, site, "route", {"origin": "JFK", "dest": "LHR"}, json_out=True))["data"]
    assert "average flights a week" in route_["counts"]
    assert "at least once" in route_["counts"]
    assert "local times" in note("schedule", origin="JFK", dest="LHR")
    flights = note("destinations", airport="JFK")
    assert "all airlines" in flights and "whole month" in flights and "0: not flying" in flights
    for given in ({}, {"airline": "B6"}):
        out = run(action_runner, site, "destinations", {"airport": "JFK", **given})
        assert not any("…" in line for line in out.split("destinations[")[0].splitlines())
    assert "not every" in note("destinations", airport="JFK", stops=1)
    paired = note("connections", origin="JFK", dest="LHR")
    assert "not checked" in paired and "connection time" in paired
    options = note("connections", origin="AEY", dest="AGA")
    assert "not checked" in options and "without the connection" in options and "season" in options
    assert "past days" in json.loads(run(action_runner, site, "schedule", {"origin": "JFK", "dest": "LHR"},
                                         json_out=True))["data"]["from"]
    assert "200 most popular" in note("airline", code="B6", show="routes")


def test_destinations_by_airline_counts_its_countries(action_runner, fake_http, site):
    url = f"{BASE}/flights-from-airport-jfk"
    fake_http.routes[url] = html(url, "airport-from-jfk.html")
    data = json.loads(run(action_runner, site, "destinations", {"airport": "JFK", "airline": "B6", "limit": 300},
                          json_out=True))["data"]
    # 46 places the page names B6 for; for `Washington` and `Portland` it doesn't say which airport,
    # so both airports of each are listed, flagged in `note`
    unsure = [r["code"] for r in data["destinations"] if "together" in r["note"]]
    assert data["total"] == 48 and sorted(unsure) == ["DCA", "IAD", "PDX", "PWM"]
    assert data["countries"] == len({r["country"] for r in data["destinations"]}) > 1
    out = run(action_runner, site, "destinations", {"airport": "JFK", "airline": "B6"})
    assert f"countries: {data['countries']}" in out


def test_route_notes_print_whole_on_a_route_that_starts_later(action_runner, fake_http, site, monkeypatch):
    monkeypatch.setattr(fc, "_today", lambda: date(2026, 10, 5))
    url = f"{BASE}/flights-from-jfk-to-lhr"
    fake_http.routes[url] = html(url, "route-jfk-lhr.html")
    out = run(action_runner, site, "route", {"origin": "JFK", "dest": "LHR"})
    lines = [line for line in out.splitlines() if line.startswith(("note:", "counts:"))]
    assert len(lines) == 2 and not any("…" in line for line in lines)


def test_airline_filter_takes_the_airlines_own_longer_name(action_runner, fake_http, site, nov13):
    """The site calls JetBlue Airways `JetBlue`, as it calls Alaska Airlines `Alaska`."""
    url = f"{BASE}/flights-from-airport-jfk"
    fake_http.routes[url] = html(url, "airport-from-jfk.html")
    by_code = json.loads(run(action_runner, site, "destinations", {"airport": "JFK", "airline": "B6", "limit": 300},
                             json_out=True))["data"]
    by_name = json.loads(run(action_runner, site, "destinations", {"airport": "JFK", "airline": "JetBlue Airways",
                                                                  "limit": 300}, json_out=True))["data"]
    assert by_name["total"] == by_code["total"] == 48
    route_url = f"{BASE}/flights-from-jfk-to-lhr"
    fake_http.routes[route_url] = html(route_url, "route-jfk-lhr.html")
    flights = json.loads(run(action_runner, site, "schedule", {"origin": "JFK", "dest": "LHR",
                                                              "airline": "JetBlue Airways"}, json_out=True))["data"]
    assert flights["flights"] and {r["airline"] for r in flights["flights"]} == {"B6"}


def test_paired_connections_show_notes_and_the_filtered_airlines(action_runner, fake_http, site, nov13):
    for path, name in (("/flights-from-jfk-to-lhr", "route-jfk-lhr.html"),
                       ("/flights-from-airport-jfk", "airport-from-jfk.html"),
                       ("/flights-to-airport-lhr", "airport-to-lhr.html"),
                       ("/flights-from-airport-lhr", "airport-to-lhr.html")):
        fake_http.routes[BASE + path] = html(BASE + path, name)
    data = json.loads(run(action_runner, site, "connections", {"origin": "JFK", "dest": "LHR", "limit": 200},
                          json_out=True))["data"]
    assert any(r["note"] for r in data["connections"])
    out = run(action_runner, site, "connections", {"origin": "JFK", "dest": "LHR"})
    assert "{via,name,miles,detour,flights,leg1,leg2,alliances,note}" in out
    oneworld = json.loads(run(action_runner, site, "connections", {"origin": "JFK", "dest": "LHR",
                                                                 "alliance": "oneworld", "limit": 200},
                              json_out=True))["data"]["connections"]
    lax = next(r for r in oneworld if r["via"] == "LAX")    # leg 1 AA DL B6, leg 2 AA BA UA VS
    assert lax["alliances"] == "Oneworld, SkyTeam"
    assert (lax["leg1"], lax["leg2"]) == ("AA", "AA BA")


def test_airline_help_points_to_the_other_views(action_runner, fake_http, site):
    url = f"{BASE}/route-map-b6"
    fake_http.routes[url] = html(url, "airline-b6.html")
    out = run(action_runner, site, "airline", {"code": "B6"})
    assert "esum flightconnections airline --code B6 --show routes" in out
    out = run(action_runner, site, "airline", {"code": "B6", "show": "routes", "origin": "JFK"})
    assert "esum flightconnections destinations --airport JFK --airline B6" in out


def test_site_options_flag_a_leg_out_of_season(action_runner, fake_http, site, nov13):
    url = f"{BASE}/flights-from-aey-to-aga"
    fake_http.routes[url] = html(url, "route-aey-aga.html")
    rows = {r["via"]: r for r in json.loads(run(action_runner, site, "connections", {"origin": "AEY", "dest": "AGA"},
                                                json_out=True))["data"]["connections"]}
    assert rows["ZRH"]["note"] == "leg 1: no airline flies it this month (WK From Jun to Aug)"   # Edelweiss
    assert rows["MAN"]["note"] == ""                    # `Ends in March`: flying until then
    assert "{via,name,time,leg1,leg2,note}" in run(action_runner, site, "connections", {"origin": "AEY", "dest": "AGA"})


def test_an_unknown_airline_name_is_an_error_not_an_empty_list(action_runner, fake_http, site, capsys, nov13):
    fake_http.routes[f"{BASE}/flights-from-airport-jfk"] = html(f"{BASE}/flights-from-airport-jfk", "airport-from-jfk.html")
    catalog = (b'<a href="/route-map-jetblue-b6" class="checkbox-option airline-option btn" data-name="JetBlue" '
               b'data-iata="B6">JetBlue (B6)</a>')
    fake_http.routes[f"{BASE}/airlines"] = Response(200, f"{BASE}/airlines", {"content-type": "text/html"}, catalog)
    registry = Registry.discover(custom_root=None)
    assert action_runner.cli(["flightconnections", "destinations", "--airport", "JFK", "--airline", "Foo Air"],
                             registry=registry) == 1
    out = capsys.readouterr().out
    assert "no airline named 'Foo Air'" in out and "help:" in out


def test_destinations_count_what_flies_this_month(action_runner, fake_http, site):
    url = f"{BASE}/flights-from-airport-jfk"
    fake_http.routes[url] = html(url, "airport-from-jfk.html")
    data = json.loads(run(action_runner, site, "destinations", {"airport": "JFK", "limit": 300}, json_out=True))["data"]
    flying = [r for r in data["destinations"] if r["flights"]]
    assert data["flying"] == f"{len(flying)} of them in {len({r['country'] for r in flying})} countries"
    assert len(flying) < data["total"]


def test_site_options_point_to_the_paired_view_for_alliances(action_runner, fake_http, site, nov13):
    url = f"{BASE}/flights-from-aey-to-aga"
    fake_http.routes[url] = html(url, "route-aey-aga.html")
    out = run(action_runner, site, "connections", {"origin": "AEY", "dest": "AGA"})
    assert "esum flightconnections connections --origin AEY --dest AGA --derive" in out
    assert "alliances" in out.split("help")[-1] and "--alliance oneworld|skyteam|star" in out


def test_route_daily_means_every_day_counted(action_runner, fake_http, site, nov13):
    """DL flies every day from Nov 13 to 22 and not after: not `daily` over Nov 13-30."""
    page = re.sub(r'data-dates="[^"]*" data-airline="DL"', 'data-dates="' + ",".join(map(str, range(1, 23)))
                  + '" data-airline="DL"', text("route-jfk-lhr.html"))
    url = f"{BASE}/flights-from-jfk-to-lhr"
    fake_http.routes[url] = Response(200, url, {"content-type": "text/html; charset=UTF-8"}, page.encode())
    data = json.loads(run(action_runner, site, "route", {"origin": "JFK", "dest": "LHR"}, json_out=True))["data"]
    days = {r["code"]: r["days"] for r in data["airlines"]}
    assert days["DL"] == "mo tu we th fr sa su" and days["BA"] == "daily"
    assert data["counted"] == "2026-11-13 to 2026-11-30" and "`daily` means every day of it" in data["counts"]


def test_another_months_timetable_keeps_the_alliance(action_runner, fake_http, site, nov13):
    """The calendar's rows leave the alliance empty; the route page's rows of this month have it."""
    url = f"{BASE}/flights-from-jfk-to-lhr"
    fake_http.routes[url] = html(url, "route-jfk-lhr.html")
    post = f"{BASE}/route_schedule.php"
    fake_http.routes[post] = Response(200, post, {"content-type": "application/json"},
                                      text("schedule-jfk-lhr-2026-12.json").encode())
    data = json.loads(run(action_runner, site, "schedule", {"origin": "JFK", "dest": "LHR", "month": "2026-12"},
                          json_out=True, fields="all"))["data"]
    assert {r["airline"]: r["alliance"] for r in data["flights"]} == {"VS": "SkyTeam", "B6": ""}
    out = run(action_runner, site, "schedule", {"origin": "JFK", "dest": "LHR", "month": "2026-12"})
    assert "--fields airline_name,alliance,month" in out


def test_destinations_say_why_their_country_count_is_one_more(action_runner, fake_http, site):
    """The JFK page says `190 destinations in 79 countries`; its rows are in 80, the US included."""
    url = f"{BASE}/flights-from-airport-jfk"
    fake_http.routes[url] = html(url, "airport-from-jfk.html")
    data = json.loads(run(action_runner, site, "destinations", {"airport": "JFK", "limit": 300}, json_out=True))["data"]
    assert data["site"].endswith("in 79 countries") and data["countries"] == 80
    assert "the site's sentence leaves its own country out" in data["counts"]
    assert "not the airline's" not in data["note"]
    b6 = json.loads(run(action_runner, site, "destinations", {"airport": "JFK", "airline": "B6"}, json_out=True))["data"]
    assert "a row's note is the route's season, not the airline's: `route` shows" in b6["note"]


def test_filtered_destinations_count_a_same_named_pair_as_the_airports_it_names(action_runner, fake_http, site):
    """JFK's page names one airline list per airport for `Washington` (AA DL B6; DL) and `Portland`
    (AS DL; DL B6) without saying which airport each is: B6 flies to one of DCA and IAD and one of
    PWM and PDX, DL to all four."""
    url = f"{BASE}/flights-from-airport-jfk"
    fake_http.routes[url] = html(url, "airport-from-jfk.html")
    out = run(action_runner, site, "destinations", {"airport": "JFK", "airline": "B6", "limit": 300})
    assert ('airports: "B6 flies to 46 of these 48: 1 of DCA and IAD, 1 of PWM and PDX; the page doesn\'t say '
            'which (`route` settles it); every other row for sure"') in out
    assert "\ncount: 48\n" in out and "\nflying: 38 of them in 3 countries\n" in out     # 36 sure, 1 of each pair
    assert ("DCA,Washington,United States,208,212,AA DL B6,\"airlines of DCA and IAD together: the page doesn't "
            "say which airport each flies; B6 flies to 1 of them\"") in out
    assert "esum flightconnections route --origin JFK --dest DCA` to settle which airport of each pair B6 flies to" in out
    out = run(action_runner, site, "destinations", {"airport": "JFK", "airline": "DL", "limit": 300})
    assert "airports:" not in out and "DL flies to both" in out
    out = run(action_runner, site, "destinations", {"airport": "JFK", "alliance": "oneworld", "limit": 300})
    assert "Oneworld flies to 1 of them" in out and "1 of DCA and IAD, 1 of PWM and PDX" in out


def test_alliance_filter_leaves_out_hubs_with_a_leg_not_flying_this_month(action_runner, fake_http, site, nov13):
    """VIE-LAX (Austrian, Star Alliance) has 0 flights this month."""
    _pages(fake_http, LHR_LAX)
    out = run(action_runner, site, "connections", {"origin": "LHR", "dest": "LAX", "alliance": "star"})
    assert "\n  VIE," not in out
    assert ('dropped: "1 hub with a leg not flying this month (VIE): the paired view without the filter lists it, '
            'with its season') in out
    assert "esum flightconnections connections --origin LHR --dest LAX --derive` to see the hub" in out
    rows = json.loads(run(action_runner, site, "connections", {"origin": "LHR", "dest": "LAX", "alliance": "star",
                                                              "limit": 300}, json_out=True))["data"]["connections"]
    assert all("Star Alliance" in r["alliances"] for r in rows)


def test_a_leg_names_two_uncoded_carriers_of_one_name_once():
    # two Norwegian operators carry no IATA code in the site's catalog and print the same name
    legs = [{"code": "", "name": "Norwegian"}, {"code": "", "name": "Norwegian"}, {"code": "SK", "name": "SAS"}]
    assert fc._codes(legs) == "Norwegian SK"


BOS_LHR = (("/flights-from-airport-bos", "airport-from-bos.html"), ("/flights-to-airport-lhr", "airport-to-lhr.html"),
           ("/flights-from-airport-lhr", "airport-from-lhr.html"))


def test_filtered_connections_count_a_hub_of_a_same_named_pair_only_when_sure(action_runner, fake_http, site, nov13):
    """BOS's page names one airline list per Washington airport (AA DL B6; UA) without saying which
    is DCA and which IAD: UA and SkyTeam (DL) fly to only one of them, so a hub at IAD is unsure.
    JFK's lists for the pair (AA DL B6; DL) both name DL, so its SkyTeam hub at IAD is sure."""
    _pages(fake_http, BOS_LHR)
    out = run(action_runner, site, "connections", {"origin": "BOS", "dest": "LHR", "alliance": "skyteam"})
    assert ('hubs: "SkyTeam flies both legs via 2 of these 3 for sure; unsure: IAD (leg 1 goes to 1 of DCA and IAD); '
            'the airport page doesn\'t say which airport of each pair (`route` settles it)"') in out
    assert "\ncount: 3\n" in out
    assert ("IAD,Washington,null,\"\",124/146,DL,VS,\"Oneworld, SkyTeam, Star Alliance\",\"leg 1: airlines of DCA and "
            "IAD together: the page doesn't say which airport each flies; SkyTeam flies to 1 of them\"") in out
    assert ("`esum flightconnections route --origin BOS --dest DCA` to settle which airport of each pair SkyTeam "
            "flies to: one `route` a pair (BOS-DCA/IAD)") in out
    out = run(action_runner, site, "connections", {"origin": "BOS", "dest": "LHR", "airline": "UA"})
    assert 'hubs: "UA flies both legs via 0 of these 1 for sure; unsure: IAD (leg 1 goes to 1 of DCA and IAD)' in out
    out = run(action_runner, site, "connections", {"origin": "BOS", "dest": "LHR"})
    assert "hubs:" not in out and "flies to" not in out        # no filter, no count
    assert ("`esum flightconnections route --origin BOS --dest DCA` to settle which airlines fly to which airport "
            "of DCA and IAD (the airport page names the city, not the airport): one `route` a pair (BOS-DCA, "
            "BOS-IAD)") in out
    _pages(fake_http, JFK_LHR)
    out = run(action_runner, site, "connections", {"origin": "JFK", "dest": "LHR", "alliance": "skyteam"})
    assert "hubs:" not in out and "--dest DCA" not in out
    assert ("IAD,Washington,null,\"\",59/146,DL,VS,\"Oneworld, SkyTeam\",\"leg 1: airlines of DCA and IAD together: "
            "the page doesn't say which airport each flies; SkyTeam flies to both\"") in out


@needs_site("matrix")
def test_route_and_connections_hand_their_carriers_to_matrix(action_runner, fake_http, site, nov13):
    for path, name in (("/flights-from-jfk-to-lhr", "route-jfk-lhr.html"),
                       ("/flights-from-airport-jfk", "airport-from-jfk.html"),
                       ("/flights-to-airport-lhr", "airport-to-lhr.html"),
                       ("/flights-from-airport-lhr", "airport-to-lhr.html"),
                       ("/flights-from-aey-to-aga", "route-aey-aga.html")):
        fake_http.routes[BASE + path] = html(BASE + path, name)
    out = run(action_runner, site, "route", {"origin": "JFK", "dest": "LHR"})
    line = next(x for x in out.splitlines() if "esum matrix search" in x)
    assert re.search(r"esum matrix search --origin JFK --dest LHR --depart <date> --thorough --carriers "
                     r"[A-Z0-9,]+` to price the route on ITA Matrix", line)
    assert set(line.split("--carriers ")[1].split("`")[0].split(",")) == {"AA", "BA", "DL", "B6", "VS"}
    out = run(action_runner, site, "connections", {"origin": "JFK", "dest": "LHR", "derive": True, "detours": True,
                                                   "limit": 200})
    carriers = next(x for x in out.splitlines() if "esum matrix search" in x).split("--carriers ")[1].split("`")[0]
    assert "LH" in carriers.split(",") and len(carriers.split(",")) <= 10   # LH flies JFK-FRA and FRA-LHR
    out = run(action_runner, site, "connections", {"origin": "AEY", "dest": "AGA"})
    assert "esum matrix search --origin AEY --dest AGA --depart <date> --thorough --carriers" in out


def test_a_route_without_nonstop_has_no_carriers_for_matrix(action_runner, fake_http, site):
    url = f"{BASE}/flights-from-aey-to-aga"
    fake_http.routes[url] = html(url, "route-aey-aga.html")
    assert "esum matrix" not in run(action_runner, site, "route", {"origin": "AEY", "dest": "AGA"})
