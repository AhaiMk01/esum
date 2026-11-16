"""Google Flights `search --all`: the rows behind "View more flights", offline, on what the page
script returned for a live search and the pages Google served for it (scrubbed)."""

from __future__ import annotations

import io
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from esum import Options
from esum.browser.daemon import BrowserError
from esum.core.registry import Registry
from esum.sites.gflights.parse import (Leg, Query, Segment, flights, flights_name, handle, more_rows, named_flights,
                                       page_url, parse_handle, pin)
from esum.transport import Response

FIX = Path(__file__).parent / "fixtures"
ROUND = Query((Segment(("SFO",), ("NRT",), "2027-01-20"), Segment(("NRT",), ("SFO",), "2027-01-27")), one_way=False)
ONE_WAY = Query((Segment(("SFO",), ("NRT",), "2027-01-20"),))
OZ = [Leg("SFO", "2027-01-20", "ICN", "OZ", "211"), Leg("ICN", "2027-01-22", "NRT", "OZ", "102")]
OZ_NAME = "SFO-ICN:2027-01-20:OZ211+ICN-NRT:2027-01-22:OZ102"     # how a row behind the button is named
SEARCH = {"origin": "SFO", "dest": "NRT", "depart": "2027-01-20", "return_": "2027-01-27"}


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def html(url: str, data) -> Response:
    body = ("<html><script>AF_initDataCallback({key: 'ds:1', hash: '2', data:" + json.dumps(data)
            + ", sideChannel: {}});</script></html>")
    return Response(200, url, {"content-type": "text/html; charset=utf-8"}, body.encode())


class Tab:
    def __init__(self, session, url):
        self.session, self.url, self.closed = session, url, False

    def evaluate(self, fn, arg=None):
        self.session.scripts.append((fn, arg))
        page = self.session.page
        if isinstance(page, list):      # one answer per read, the last one from then on
            page = page.pop(0) if len(page) > 1 else page[0]
        if isinstance(page, Exception):
            raise page
        return page

    def close(self):
        self.closed = True


class Browser:
    """ESUM's browser session as a page script sees it: a tab opened on a URL, run in, closed.
    Every tab answers with `page`, what more.js returned for the live search; a list of pages
    answers one read each."""

    def __init__(self, page: dict | list | None = None):
        self.page = load("more_rows.json") if page is None else page
        self.tabs: list[Tab] = []
        self.scripts: list[tuple] = []

    def open_tab(self, url, cleared=None, *, site=""):
        self.tabs.append(Tab(self, url))
        return self.tabs[-1]


@pytest.fixture(scope="module")
def gf():
    return Registry.discover().get("gflights")


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    import esum.sites.gflights as mod

    class Today(date):
        @classmethod
        def today(cls):
            return date(2026, 11, 15)

    monkeypatch.setattr(mod, "date", Today)
    ticks = iter(range(0, 10 ** 6, 5))      # five seconds pass between looks at the clock: a read's worth
    monkeypatch.setattr(mod, "_clock", lambda: next(ticks))


@pytest.fixture
def pages(fake_http):
    """The first page of the round trip and of the one-way, and the returns page for OZ211+OZ102."""
    for q in (ROUND, ONE_WAY):
        fake_http.routes[page_url(q)] = html(page_url(q), load("more_page.json"))
    for q in (pin(ROUND, 0, OZ), pin(Query((ONE_WAY.segments[0], Segment(("NRT",), ("SFO",), "2027-01-27")),
                                           one_way=False), 0, OZ)):
        fake_http.routes[page_url(q)] = html(page_url(q), load("more_returns.json"))
    return fake_http


def run(action_runner, gf, action: str, kwargs: dict, browser: Browser | None = None, *, json_out: bool = False) -> str:
    out = io.StringIO()
    action_runner(gf, gf.all_actions[action], kwargs, Options(json=json_out), stream=out, session=browser)
    return out.getvalue()


# ---- the rows the page script returned ---------------------------------------------------------

def test_a_row_from_the_page_reads_like_the_same_row_from_the_first_page():
    first = {row["id"]: row for row in flights(load("more_page.json"))}
    again = {"+".join(leg["flight"] for leg in row["legs"]): row for row in more_rows(load("more_rows.json")["rows"])[0]}
    assert len(first) == 6 and set(first) <= set(again)
    same = ("price", "currency", "itinerary", "carriers", "stops", "duration", "minutes", "depart", "arrive",
            "layovers", "co2_kg", "co2_typical_kg", "warnings")
    for id, row in first.items():
        assert {k: again[id][k] for k in same} == {k: row[k] for k in same}
        assert set(again[id]) == set(row) | {"more", "missing"} and again[id]["missing"] == []
        assert [set(leg) for leg in again[id]["legs"]] == [set(leg) for leg in row["legs"]]


def test_a_row_from_the_page_has_its_flights_times_stops_and_price():
    rows = {"+".join(leg["flight"] for leg in row["legs"]): row for row in more_rows(load("more_rows.json")["rows"])[0]}
    row = rows["DL2721+DL197+KE705"]
    assert row["id"] == "SFO-SEA:2027-01-20:DL2721+SEA-ICN:2027-01-20:DL197+ICN-NRT:2027-01-21:KE705"
    assert row["itinerary"] == "SFO 01-20 06:05 → NRT 01-21 21:25 · 22h20m · via SEA ICN · DL2721 DL197 KE705"
    assert (row["price"], row["currency"], row["stops"], row["carriers"]) == (1222, "USD", 2, "DL KE")
    assert row["layovers"] == ["4h20m SEA", "1h10m ICN"]
    assert [(leg["from"], leg["to"], leg["flight"], leg["depart"][:10]) for leg in row["legs"]] == [
        ("SFO", "SEA", "DL2721", "2027-01-20"), ("SEA", "ICN", "DL197", "2027-01-20"),
        ("ICN", "NRT", "KE705", "2027-01-21")]
    assert row["aircraft"] == "? | ? | ?" and row["more"] is True
    assert rows["AS2358+JL65"]["warnings"] == ["a flight operated by Horizon Air as Alaska Horizon"]


def test_rows_without_flights_are_counted_repeats_are_one_row_and_the_year_turns():
    item = {"flights": "https://www.travelimpactmodel.org/lookup/flight?itinerary=JFK-CDG-AF-7-20261231",
            "label": "From 400 euros. Nonstop flight with Air France. Leaves John F. Kennedy International Airport at "
                     "11:55 PM on Thursday, December 31 and arrives at Paris Charles de Gaulle Airport at 12:30 PM "
                     "on Friday, January 1. Total duration 6 hr 35 min. Separate tickets booked together  Select flight",
            "price": "1,400 euros", "token": None, "co2": "300400", "co2_typical": None}
    rows, unreadable = more_rows([item, dict(item), {"flights": "about:blank", "label": ""}, None])
    assert len(rows) == 1 and unreadable == 2
    row = rows[0]
    assert (row["depart"], row["arrive"], row["minutes"]) == ("2026-12-31 23:55", "2027-01-01 12:30", 395)
    assert (row["price"], row["currency"], row["co2_kg"]) == (1400, None, 300)
    assert row["warnings"] == ["Separate tickets booked together"] and row["missing"] == ["currency"]


YVR = {"flights": "https://www.travelimpactmodel.org/lookup/flight?itinerary=SFO-YVR-UA-2259-20270310,"
                  "YVR-NRT-AC-3-20270310",
       "token": "CjRIZk5ZdmJJYzNVY0VBTDBmdGdCRy0tLS0tLS0tb3ljZ24xMUFBQUFBR3I1N2EwSGhoSTRBEgpVQTIyNTl8QUMzGgsIrccOEAIaA1VT"
                "RDgccK3HDg==",
       "price": "From 2386 US dollars round trip total", "co2": "518000", "co2_typical": "630000"}
YVR_SENTENCE = ("From 2386 US dollars round trip total. 1 stop flight with United and Air Canada. Leaves San Francisco "
                "International Airport at 8:33 AM on Wednesday, March 10 and arrives at Narita International Airport "
                "at 4:40 PM on Thursday, March 11. Total duration 15 hr 7 min.  Layover (1 of 1) is a 2 hr 40 min "
                "layover at Vancouver International Airport in Vancouver. Select flight")
YVR_TEXT = "\n".join(["8:33 AM", "\u00a0\u2013\u00a0", "4:40 PM+1", "United, Air CanadaANA", "15 hr 7 min",
                      "SFO\u2013NRT", "1 stop", "2 hr 40 min YVR", "518 kg CO2e", "-18% emissions", "$2,386",
                      "round trip"])
YVR_READ = ("2027-03-10 08:33", "2027-03-11 16:40", 907, ["2h40m YVR"], 2386, "USD", [])


def read(row: dict) -> tuple:
    return (row["depart"], row["arrive"], row["minutes"], row["layovers"], row["price"], row["currency"],
            row["missing"])


def test_the_description_is_found_among_the_rows_labels():
    labels = ["Departure time: 8:33 AM.", YVR_SENTENCE, "Arrival time: 4:40 PM."]
    assert read(more_rows([{**YVR, "labels": labels}])[0][0]) == YVR_READ


def test_the_rows_own_text_gives_what_an_unrecognized_description_does_not():
    row = more_rows([{**YVR, "labels": ["A flight. Pick it."], "text": YVR_TEXT}])[0][0]
    assert read(row) == YVR_READ
    assert row["itinerary"] == "SFO 03-10 08:33 \u2192 NRT 03-11 16:40 \u00b7 15h07m \u00b7 via YVR \u00b7 UA2259 AC3"


def test_a_row_neither_source_describes_is_listed_by_its_flights_with_the_rest_unknown():
    row = more_rows([{**YVR, "labels": ["A flight. Pick it."], "text": "a row", "price": None, "token": "junk"}])[0][0]
    assert read(row) == ("2027-03-10", "", None, ["YVR"], None, None,
                         ["times", "duration", "layovers", "price", "currency"])
    assert row["id"] == "SFO-YVR:2027-03-10:UA2259+YVR-NRT:2027-03-10:AC3"
    assert row["itinerary"] == ("SFO 03-10 \u2192 NRT \u00b7 time unknown \u00b7 duration unknown \u00b7 via YVR "
                                "\u00b7 UA2259 AC3")


def test_the_price_token_gives_the_price_when_the_label_does_not():
    assert read(more_rows([{**YVR, "label": YVR_SENTENCE, "price": None}])[0][0]) == YVR_READ


def test_an_arrival_a_day_before_the_departure_is_the_date_line_not_next_year():
    item = {"flights": "https://www.travelimpactmodel.org/lookup/flight?itinerary=NRT-HNL-JL-74-20270101",
            "label": "Nonstop flight with JAL. Leaves Narita International Airport at 9:00 PM on Friday, January 1 "
                     "and arrives at Daniel K. Inouye International Airport at 8:45 AM on Friday, January 1. Total "
                     "duration 6 hr 45 min. Select flight", "price": "500 US dollars", "token": None}
    assert more_rows([item])[0][0]["arrive"] == "2027-01-01 08:45"
    item["label"] = item["label"].replace("8:45 AM on Friday, January 1", "8:45 AM on Thursday, December 31")
    assert more_rows([item])[0][0]["arrive"] == "2026-12-31 08:45"


def test_an_itinerary_is_named_by_its_flights_in_full():
    assert flights_name(OZ) == OZ_NAME and named_flights(OZ_NAME.lower().replace("+", " + ")) == OZ
    assert named_flights("SFO-NRT:2027-01-20:ZG025") == [Leg("SFO", "2027-01-20", "NRT", "ZG", "25")]
    assert named_flights("OZ211+OZ102") is None and named_flights("SFO-ICN:2027-01-20:OZ211+OZ102") is None
    for q in (ROUND, Query(ONE_WAY.segments, currency="EUR")):
        assert parse_handle(handle(q)) == q


# ---- search --all ------------------------------------------------------------------------------

def test_a_search_names_the_command_that_lists_the_rest_and_opens_no_browser(action_runner, pages, gf):
    browser = Browser()
    out = run(action_runner, gf, "search", SEARCH, browser)
    assert "count: 6" in out and 'listed: "the first page\'s 6; Google has more behind' in out
    assert "esum gflights search --origin SFO --dest NRT --depart 2027-01-20 --return 2027-01-27 --all`" in out
    assert browser.tabs == []


def test_a_page_without_more_flights_offers_no_all(action_runner, fake_http, gf):
    data = load("more_page.json")
    data[3][1] = 0          # what Google sends when the page has no "View more flights"
    fake_http.routes[page_url(ROUND)] = html(page_url(ROUND), data)
    out = run(action_runner, gf, "search", SEARCH)
    assert "--all" not in out and "listed:" not in out


def test_all_adds_the_rows_behind_view_more_flights(action_runner, pages, gf):
    browser = Browser()
    out = run(action_runner, gf, "search", {**SEARCH, "all_": True}, browser)
    assert "12 on Google's expanded page, read 3 times: 6 from the first page, 6 from behind" in out
    assert "count: 12" in out
    assert f'  "{OZ_NAME}","USD1,154","SFO 01-20 23:00 → NRT 01-22 11:20 · 19h20m · via ICN · OZ211 OZ102"' in out
    ids = [line.split(",")[0].strip() for line in out.splitlines() if line.startswith("  ") and "USD" in line]
    assert ids[:3] == ["ZG25", "CX873+CX526", '"SFO-HKG:2027-01-20:CX873+HKG-NRT:2027-01-22:CX504"']     # one list, cheapest first
    assert [(tab.url, tab.closed) for tab in browser.tabs] == [(page_url(ROUND), True)] * 3    # until one adds none, 12 s in
    assert f"esum gflights returns --search {handle(ROUND)} --id ZG25" in out
    assert "--all` to list the flights behind" not in out


def part(*keep: int) -> dict:
    """What more.js returns when Google's page lists only some of the rows."""
    page = load("more_rows.json")
    return {**page, "rows": [page["rows"][i] for i in keep]}


def test_all_reads_the_page_until_a_read_adds_no_row_and_lists_every_reads_rows(action_runner, pages, gf):
    """Google hands the page what it has gathered so far: more a few seconds on, sometimes fewer."""
    browser = Browser([part(*range(8)), part(*range(11)), part(*range(6), 11), part(*range(9))])
    out = run(action_runner, gf, "search", {**SEARCH, "all_": True}, browser)
    assert "count: 12" in out and "read 4 times: 6 from the first page, 6 from behind" in out
    assert "still adding" not in out and len(browser.tabs) == 4


def test_all_says_when_the_reads_never_agreed(action_runner, pages, gf, monkeypatch):
    import esum.sites.gflights as mod
    monkeypatch.setattr(mod, "MORE_READS", 4)
    browser = Browser([part(*range(6 + i)) for i in range(1, 7)])
    out = run(action_runner, gf, "search", {**SEARCH, "all_": True}, browser)
    assert "count: 10" in out and len(browser.tabs) == 4
    assert "Google was still adding flights after 4 reads: run it again for more" in out


def test_a_read_that_adds_no_row_does_not_end_the_reading_while_google_may_still_be_gathering(action_runner, pages, gf):
    """Twelve seconds from the search's first request: a first-time search listed 26 rows twice, then 40."""
    browser = Browser([part(*range(8)), part(*range(8)), part(*range(12)), part(*range(12))])
    out = run(action_runner, gf, "search", {**SEARCH, "all_": True}, browser)
    assert "count: 12" in out and "read 4 times" in out and "still adding" not in out


def test_all_names_the_rows_google_lists_without_a_price(action_runner, pages, gf):
    page = part(*range(12))
    page["rows"][8] = {**page["rows"][8], "price": None, "token": None}
    out = run(action_runner, gf, "search", {**SEARCH, "all_": True}, Browser(page))
    assert "1 listed without a price (Google's row gave none)" in out and "not recognized" not in out


@pytest.mark.parametrize("second, said", [
    ({"button": True, "expanded": False, "rows": []}, "read 2 of the page failed (Google Flights' page did not list"),
    (BrowserError("tab evaluation timed out"), "read 2 of the page failed (tab evaluation timed out)"),
    ({"button": False, "expanded": False, "rows": []}, "read 2 of the page failed (Google Flights says more flights"),
])
def test_all_keeps_what_it_read_when_a_later_read_fails_and_says_which(action_runner, pages, gf, second, said):
    out = run(action_runner, gf, "search", {**SEARCH, "all_": True}, Browser([part(*range(9)), second]))
    assert "count: 9" in out and "read 1 times" in out
    assert said in out and ": run it again for more" in out and "still adding" not in out


def test_all_lists_a_row_as_the_latest_read_gave_it(action_runner, pages, gf):
    later = part(*range(12))
    later["rows"][8] = {**later["rows"][8], "price": "999 US dollars"}     # OZ211+OZ102, USD1,154 on the first read
    out = run(action_runner, gf, "search", {**SEARCH, "all_": True}, Browser([part(*range(12)), later]))
    assert f'  "{OZ_NAME}",USD999,' in out and "USD1,154" not in out


def test_a_flight_number_has_one_name_whichever_way_the_page_writes_it():
    item = {"flights": "https://www.travelimpactmodel.org/lookup/flight?itinerary=SFO-NRT-ZG-025-20270120",
            "label": "", "price": "706 US dollars", "token": None}
    row = more_rows([item])[0][0]
    assert row["id"] == "SFO-NRT:2027-01-20:ZG25" and row["legs"][0]["flight"] == "ZG25"
    assert flights_name([Leg("SFO", "2027-01-20", "NRT", "ZG", "025")]) == "SFO-NRT:2027-01-20:ZG25"


def test_all_as_json_marks_the_rows_from_behind_view_more_flights(action_runner, pages, gf):
    data = json.loads(run(action_runner, gf, "search", {**SEARCH, "all_": True, "sort": "best", "limit": 7}, Browser(),
                          json_out=True))["data"]
    rows = data["itineraries"]
    assert data["total"] == 12 and [bool(row.get("more")) for row in rows] == [False] * 6 + [True]
    assert rows[0]["aircraft"] == "Boeing 787" and rows[6]["aircraft"] == "? | ?"
    assert data["search"] == handle(ROUND) and rows[5]["id"].count(":") == 0 and rows[6]["id"].count(":") == 4


def test_all_on_a_page_without_the_button_is_the_first_page(action_runner, fake_http, gf):
    data = load("more_page.json")
    data[3][1] = 0          # the page's data says there is nothing more
    fake_http.routes[page_url(ROUND)] = html(page_url(ROUND), data)
    browser = Browser({"button": False, "expanded": False, "rows": load("more_rows.json")["rows"][:6]})
    out = run(action_runner, gf, "search", {**SEARCH, "all_": True}, browser)
    assert "count: 6" in out and "the page's 6: Google's page has no" in out
    assert [tab.closed for tab in browser.tabs] == [True]
    assert browser.scripts[0][1] == {"timeout_ms": 20000, "expect_more": False}


def test_all_says_when_the_page_has_more_and_shows_no_button(action_runner, pages, gf):
    browser = Browser({"button": False, "expanded": False, "rows": load("more_rows.json")["rows"][:6]})
    with pytest.raises(Exception) as e:
        run(action_runner, gf, "search", {**SEARCH, "all_": True}, browser)
    assert 'says more flights exist, but its "View more flights" button did not show within 20 s' in str(e.value)
    assert "esum gflights search --origin SFO --dest NRT --depart 2027-01-20 --return 2027-01-27`" in e.value.help
    assert browser.scripts[0][1] == {"timeout_ms": 20000, "expect_more": True}


def test_all_says_when_the_script_returns_no_rows_list(action_runner, pages, gf):
    with pytest.raises(Exception, match="gave ESUM's page script no rows to read"):
        run(action_runner, gf, "search", {**SEARCH, "all_": True}, Browser({}))


def test_all_lists_rows_it_cant_describe_and_says_what_is_unknown(action_runner, pages, gf):
    page = load("more_rows.json")
    for item in page["rows"][6:]:
        item["label"] = "A flight. Pick it."
    for sort in ("price", "departure", "arrival", "duration"):
        out = run(action_runner, gf, "search", {**SEARCH, "all_": True, "sort": sort}, Browser(page))
        assert "count: 12" in out
    assert ("6 from behind \\\"View more flights\\\"; times unknown on 6; duration unknown on 6; layover lengths "
            "unknown on 6 (the row's description was not recognized; `detail` reads the flights' own page)") in out
    assert OZ_NAME in out and "SFO 01-20 \u2192 NRT \u00b7 time unknown \u00b7 duration unknown" in out


def test_all_counts_the_rows_the_page_gave_no_flights_for(action_runner, pages, gf):
    page = {**load("more_rows.json"), "unread": 2}
    page["rows"].append({"flights": "about:blank", "label": ""})
    out = run(action_runner, gf, "search", {**SEARCH, "all_": True}, Browser(page))
    assert "count: 12" in out and "3 more rows left out: the page gave no flights to name them by" in out


def test_all_says_when_no_row_behind_the_button_can_be_read(action_runner, pages, gf):
    page = load("more_rows.json")
    page["rows"] = page["rows"][:6] + [{"flights": "about:blank", "label": ""}] * 4
    with pytest.raises(Exception) as e:
        run(action_runner, gf, "search", {**SEARCH, "all_": True}, Browser(page))
    assert 'none of the 4 rows behind "View more flights" gave flights ESUM could read' in str(e.value)


def test_all_says_when_the_page_did_not_list_the_rest_and_closes_its_tab(action_runner, pages, gf):
    browser = Browser({"button": True, "expanded": False, "rows": []})
    with pytest.raises(Exception) as e:
        run(action_runner, gf, "search", {**SEARCH, "all_": True}, browser)
    assert 'did not list the flights behind "View more flights" within 20 s' in str(e.value)
    assert "esum gflights search --origin SFO --dest NRT --depart 2027-01-20 --return 2027-01-27`" in e.value.help
    assert [tab.closed for tab in browser.tabs] == [True]


# ---- the other actions on a row from behind "View more flights" --------------------------------

def test_returns_takes_an_outbound_by_its_flights_and_opens_no_browser(action_runner, pages, gf):
    browser = Browser()
    out = run(action_runner, gf, "returns", {"search": handle(ROUND), "id": OZ_NAME}, browser)
    assert f'outbound: "{OZ_NAME}: SFO 01-20 23:00 → NRT 01-22 11:20 · 19h20m · via ICN · OZ211 OZ102"' in out
    assert '  OZ101+OZ212,"USD1,154",' in out
    assert f"search: {handle(pin(ROUND, 0, OZ))}" in out
    assert browser.tabs == []


def test_a_first_page_row_goes_by_its_flights_in_full_too(action_runner, pages, gf):
    browser = Browser()
    out = run(action_runner, gf, "detail", {"search": handle(ROUND), "id": "SFO-NRT:2027-01-20:ZG25"}, browser)
    assert "Itinerary USD706 round trip from: ZG25" in out and browser.tabs == []


def test_detail_of_an_outbound_named_by_its_flights_prices_the_cheapest_round_trip_with_it(action_runner, pages, gf):
    out = run(action_runner, gf, "detail", {"search": handle(ROUND), "id": OZ_NAME}, Browser())
    assert f"Itinerary USD1,154 round trip from: {OZ_NAME} (economy)" in out
    assert f"esum gflights returns --search {handle(ROUND)} --id {OZ_NAME}" in out


def test_detail_reads_a_one_way_named_by_its_flights_and_shows_no_price(action_runner, pages, gf):
    browser = Browser()
    out = run(action_runner, gf, "detail", {"search": handle(ONE_WAY), "id": OZ_NAME.lower()}, browser)
    assert f"Itinerary one way: {OZ_NAME} (economy)" in out
    assert "  OZ211 Asiana Airlines · SFO 2027-01-20 23:00 → ICN 2027-01-22 05:10 · 13h10m" in out
    assert "Boeing 787-10" in out and "Airbus A350" in out and "layover 3h50m ICN" in out
    assert "Book (airline and agency fares): https://www.google.com/travel/flights/booking?tfs=" in out
    assert f"esum gflights fares --search {handle(ONE_WAY)} --id {OZ_NAME}" in out
    assert browser.tabs == []


def test_an_id_the_first_page_lacks_says_how_such_a_row_is_named(action_runner, pages, gf):
    with pytest.raises(Exception) as e:
        run(action_runner, gf, "detail", {"search": handle(ONE_WAY), "id": "XX1"}, Browser())
    assert "no itinerary 'XX1' on this search's first page or behind \"View more flights\"" in str(e.value)
    assert "esum gflights search --origin SFO --dest NRT --depart 2027-01-20`" in e.value.help
    assert "(add `--all` for the rows behind" in e.value.help


def test_a_name_google_answers_with_other_flights_is_refused(action_runner, pages, gf):
    """Google pins what it can of the flights asked for; the row is the asked one only if they match."""
    asked = [OZ[0], Leg("LAX", "2027-01-22", "NRT", "OZ", "102")]
    pinned = pin(Query((ONE_WAY.segments[0], Segment(("NRT",), ("SFO",), "2027-01-27")), one_way=False), 0, asked)
    pages.routes[page_url(pinned)] = html(page_url(pinned), load("more_returns.json"))     # names OZ211 via ICN
    with pytest.raises(Exception) as e:
        run(action_runner, gf, "detail", {"search": handle(ONE_WAY), "id": flights_name(asked)}, Browser())
    assert f"answered {flights_name(asked)} with another itinerary ({OZ_NAME})" in str(e.value)
    assert "--all`" in e.value.help


def test_a_city_code_search_takes_a_row_named_by_its_airports(action_runner, pages, gf):
    tokyo = Query((Segment(("SFO",), ("TYO",), "2027-01-20"),))
    pages.routes[page_url(tokyo)] = html(page_url(tokyo), load("more_page.json"))
    pinned = pin(Query((tokyo.segments[0], Segment(("TYO",), ("SFO",), "2027-01-27")), one_way=False), 0, OZ)
    pages.routes[page_url(pinned)] = html(page_url(pinned), load("more_returns.json"))
    out = run(action_runner, gf, "detail", {"search": handle(tokyo), "id": OZ_NAME}, Browser())
    assert f"Itinerary one way: {OZ_NAME} (economy)" in out


def later(days: int) -> Query:
    """The round trip whose returns page `detail` reads for a one-way's flights: OZ out, back `days` later."""
    back = (date(2027, 1, 20) + timedelta(days=days)).isoformat()
    return pin(Query((ONE_WAY.segments[0], Segment(("NRT",), ("SFO",), back)), one_way=False), 0, OZ)


def test_detail_of_a_one_way_tries_a_later_return_when_google_sells_none_a_week_on(action_runner, pages, gf):
    pages.routes[page_url(later(7))] = html(page_url(later(7)), load("more_page.json"))    # names no outbound
    pages.routes[page_url(later(14))] = html(page_url(later(14)), load("more_returns.json"))
    out = run(action_runner, gf, "detail", {"search": handle(ONE_WAY), "id": OZ_NAME}, Browser())
    assert f"Itinerary one way: {OZ_NAME} (economy)" in out and "layover 3h50m ICN" in out


def test_detail_says_when_google_gives_no_details_for_the_flights(action_runner, pages, gf):
    for days in (7, 14, 21, 10):
        pages.routes[page_url(later(days))] = html(page_url(later(days)), load("more_page.json"))   # no outbound
    with pytest.raises(Exception) as e:
        run(action_runner, gf, "detail", {"search": handle(ONE_WAY), "id": OZ_NAME}, Browser())
    assert f"did not give the details of {OZ_NAME}" in str(e.value)
    assert "/travel/flights/booking?tfs=" in e.value.help and "--all`" in e.value.help


def test_a_layover_that_changes_airports_has_its_length_and_the_compact_layout_reads_too():
    item = {"flights": "https://www.travelimpactmodel.org/lookup/flight?itinerary=SFO-NRT-NH-7-20270310,"
                       "HND-SIN-NH-843-20270312", "price": "722 US dollars", "token": None,
            "label": "1 stop flight with ANA. Leaves San Francisco International Airport at 11:05 AM on Wednesday, "
                     "March 10 and arrives at Singapore Changi Airport at 6:55 AM on Friday, March 12. Total duration "
                     "27 hr 50 min.  Layover (1 of 1) is a 9 hr layover in Tokyo. Transfer here from Narita "
                     "International Airport to Haneda Airport. Select flight"}
    row = more_rows([item])[0][0]
    assert row["layovers"] == ["9h00m NRT→HND (change airports)"] and row["missing"] == ["currency"]
    compact = {**item, "label": "A flight.", "text": "11:05 AM\nSFO\n6:55 AM+2\nSIN\n$722\n1 stop27 hr 50 minANAUnited"}
    row = more_rows([compact])[0][0]
    assert (row["depart"], row["arrive"], row["minutes"]) == ("2027-03-10 11:05", "2027-03-12 06:55", 1670)
    assert row["missing"] == ["layovers", "currency"]


def test_a_flight_number_can_end_in_a_letter():
    item = {"flights": "https://www.travelimpactmodel.org/lookup/flight?itinerary=SFO-NRT-UA-0837A-20270120",
            "label": "", "price": "706 US dollars", "token": None}
    row = more_rows([item])[0][0]
    assert row["id"] == "SFO-NRT:2027-01-20:UA837A" and row["legs"][0]["flight"] == "UA837A"
    assert named_flights("sfo-nrt:2027-01-20:ua0837a") == [Leg("SFO", "2027-01-20", "NRT", "UA", "837A")]
    assert flights_name(named_flights(row["id"])) == row["id"]
    assert named_flights("SFO-NRT:2027-01-20:UA837AB") is None and named_flights("SFO-NRT:2027-01-20:UAA") is None


def test_a_row_from_behind_the_button_written_by_its_flight_numbers_is_found_in_the_list(action_runner, pages, gf):
    """The short id lacks the airports and dates: the expanded list is read again for them."""
    browser = Browser()
    out = run(action_runner, gf, "detail", {"search": handle(ONE_WAY), "id": "oz211+oz102"}, browser)
    assert f"Itinerary USD1,154 one way: {OZ_NAME} (economy)" in out and "layover 3h50m ICN" in out
    assert len(browser.tabs) == 3 and all(tab.closed for tab in browser.tabs)
    out = run(action_runner, gf, "returns", {"search": handle(ROUND), "id": "OZ211+OZ102"}, Browser())
    assert f'outbound: "{OZ_NAME}: SFO 01-20 23:00' in out


def test_flight_numbers_in_neither_list_are_no_itinerary(action_runner, pages, gf):
    browser = Browser()
    with pytest.raises(Exception, match="no itinerary 'XX1' on this search's first page"):
        run(action_runner, gf, "detail", {"search": handle(ONE_WAY), "id": "XX1"}, browser)
    assert len(browser.tabs) == 3       # the list behind the button was looked through
