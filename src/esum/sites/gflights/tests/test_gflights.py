"""Google Flights: offline tests on scrubbed search-page payloads."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from esum import Options
from support import require_site
from esum.core.registry import Registry
from esum.sites.gflights.parse import (Leg, Query, Segment, booking_options, booking_url, flights, handle, history_text, insight,
                                       insight_text, page_error, page_url, parse_handle, payload, pin, selected,
                                       tfs)
from esum.transport import Response

FIX = Path(__file__).parent / "fixtures"
ONE_WAY = Query((Segment(("SFO",), ("JFK",), "2026-12-10"),))
ROUND = Query((Segment(("SFO",), ("LHR",), "2026-12-10"), Segment(("LHR",), ("SFO",), "2026-12-17")), one_way=False)
UA901 = [Leg("SFO", "2026-12-10", "LHR", "UA", "901")]


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def page(data) -> str:
    """The fixture payload as the search page carries it."""
    return ("<html><script>AF_initDataCallback({key: 'ds:0', hash: '1', data:[1], sideChannel: {}});</script>"
            f"<script>AF_initDataCallback({{key: 'ds:1', hash: '2', data:{json.dumps(data)}, sideChannel: {{}}}});"
            "</script></html>")


def html(url: str, data) -> Response:
    return Response(200, url, {"content-type": "text/html; charset=utf-8"}, page(data).encode())


@pytest.fixture(scope="module")
def gf():
    return Registry.discover().get("gflights")


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    """The fixtures' dates are in December 2026; validation compares them with today."""
    import esum.sites.gflights as mod

    class Today(date):
        @classmethod
        def today(cls):
            return date(2026, 11, 6)

    monkeypatch.setattr(mod, "date", Today)


def run(action_runner, target, action: str, kwargs: dict, *, json_out: bool = False) -> str:
    out = io.StringIO()
    action_runner(target, target.all_actions[action], kwargs, Options(json=json_out), stream=out)
    return out.getvalue()


# ---- tokens ----------------------------------------------------------------------------------

def test_tfs_matches_googles_own_url():
    assert tfs(ONE_WAY) == "CBwQAhoeEgoyMDI2LTEyLTEwagcIARIDU0ZPcgcIARIDSkZLQAFIAXABmAEC"
    assert page_url(ONE_WAY).endswith("&hl=en&gl=US")
    assert page_url(Query(ONE_WAY.segments, currency="EUR")).endswith("&curr=EUR")


def test_booking_url_is_the_one_google_opened():
    legs = [Leg("SFO", "2026-12-10", "LAX", "AA", "6266"), Leg("LAX", "2026-12-10", "JFK", "AA", "4")]
    assert booking_url(pin(ONE_WAY, 0, legs)) == (
        "https://www.google.com/travel/flights/booking?tfs=CBwQAhpfEgoyMDI2LTEyLTEwIiAKA1NGTxIKMjAyNi0xMi0xMBoD"
        "TEFYKgJBQTIENjI2NiIdCgNMQVgSCjIwMjYtMTItMTAaA0pGSyoCQUEyATRqBwgBEgNTRk9yBwgBEgNKRktAAUgBcAGCAQsI______"
        "______AZgBAg&hl=en&gl=US")


def test_handle_round_trips():
    q = Query((Segment(("SFO", "OAK"), ("NYC",), "2026-12-10", max_stops=0),
               Segment(("NYC",), ("SFO", "OAK"), "2026-12-17", max_stops=0)), adults=2, cabin=3, currency="EUR",
              one_way=False)
    q = pin(q, 0, UA901)
    assert ".EUR." in handle(q)
    assert parse_handle(handle(q)) == q
    assert parse_handle(handle(ONE_WAY)) == ONE_WAY


@pytest.mark.parametrize("bad", ["", "hello", "CBwQAg", "CBwQAhoeEgoyMDI2LTEyLTEwagcIARIDU0ZPcgcIARIDSkZLQAFIAXABmAEC.eur",
                                 "AAAA"])
def test_bad_handles_are_rejected(bad):
    with pytest.raises(ValueError):
        parse_handle(bad)


# ---- the page --------------------------------------------------------------------------------

def test_payload_extraction():
    assert payload(page([1, 2])) == [1, 2]
    assert payload("<html>consent</html>") is None
    err = ("AF_initDataCallback({key: 'ds:1',  data:[3,null,[[\"type.googleapis.com/travel.frontend.flights."
           "ErrorResponse\",[[null],2]]]],errorHasStatus: true,});")
    assert page_error(payload(err)) == 2
    assert page_error(load("error_past.json")) == 2
    assert page_error(load("oneway.json")) is None
    assert flights(load("empty.json")) == []


def test_one_way_rows():
    rows = flights(load("oneway.json"))
    assert [r["id"] for r in rows] == ["AA148", "B6434+B6717", "AS42", "AA6266+AA4", "AA2614+AA2882", "AA6316+AA28"]
    assert [r["best"] for r in rows] == [True] * 3 + [False] * 3
    first = rows[0]
    assert (first["price"], first["currency"], first["stops"]) == (174, "USD", 0)
    assert first["itinerary"] == "SFO 12-10 16:05 → JFK 12-11 00:30 · 5h25m · nonstop · AA148"
    lax = rows[3]
    assert lax["itinerary"] == "SFO 12-10 10:44 → JFK 12-10 22:08 · 8h24m · via LAX · AA6266 AA4"
    assert lax["layovers"] == ["1h28m LAX"]
    assert lax["warnings"] == ["AA6266 operated by SkyWest Airlines as American Eagle"]
    assert (lax["co2_kg"], lax["co2_typical_kg"]) == (393, 362)
    leg = lax["legs"][0]
    assert leg["aircraft"] == "Embraer 175" and leg["legroom"] == "30 inches" and leg["cabin"] == "economy"
    assert leg["amenities"] == ["power and USB", "stream to device", "free Wi-Fi"]
    assert leg["seat"] == "average legroom"
    assert (leg["carrier"], leg["number"], leg["depart"]) == ("AA", "6266", "2026-12-10 10:44")


def test_insight():
    info = insight(load("oneway.json"))
    assert (info["level"], info["lowest"], info["usual"], info["typical_low"], info["typical_high"]) == \
        ("typical", 174, 181, 105, 295)
    assert insight_text(info, "USD") == "typical: lowest USD174 (USD7 under the usual USD181); typical range USD105–USD295"
    assert history_text(info, "USD") == ("2026-09-07 USD195 → 2026-11-06 USD174; low USD129 on 2026-10-28, "
                                         "high USD199 on 2026-09-26")
    assert insight(load("roundtrip.json")) is None     # Google leaves the block out on some pages


@pytest.mark.parametrize("code,level", [(1, "low"), (2, "typical"), (3, "typical"), (5, "high")])
def test_insight_levels(code, level):
    data = [None] * 6
    data[5] = [code, [None, 24], [None, 29], [None, 5], [None, 25], [None, 105]]
    assert insight(data)["level"] == level


def test_returns_page_names_the_outbound():
    data = load("returns.json")
    assert selected(data)["id"] == "UA901"
    assert selected(load("oneway.json")) is None
    lh = next(r for r in flights(data) if r["id"] == "LH2485+LH458")
    assert lh["price"] == 985 and lh["layovers"] == ["7h10m MUC"]


# ---- actions ---------------------------------------------------------------------------------

def test_search_one_way(action_runner, fake_http, gf):
    url = page_url(ONE_WAY)
    fake_http.routes[url] = html(url, load("oneway.json"))
    out = run(action_runner, gf, "search", {"origin": "sfo", "dest": "JFK", "depart": "2026-12-10", "limit": 3})
    assert "AA148,USD174" in out and "AA2614+AA2882" in out and "AS42" not in out   # cheapest three, tie by time
    assert "price_insight:" in out and "USD7 under the usual USD181" in out
    assert "prices: one way for 1 adult" in out and "count: 3 of 6 total" in out
    assert f"esum gflights detail --search {handle(ONE_WAY)} --id AA148" in out
    require_site("matrix")
    assert "esum matrix search --origin sfo --dest JFK --depart 2026-12-10 --thorough` to look for carriers" in out


def test_search_round_trip_offers_returns(action_runner, fake_http, gf):
    url = page_url(ROUND)
    fake_http.routes[url] = html(url, load("roundtrip.json"))
    out = run(action_runner, gf, "search", {"origin": "SFO", "dest": "LHR", "depart": "2026-12-10",
                                            "return_": "2026-12-17", "sort": "duration"})
    assert "prices: round-trip total for 1 adult" in out and "price_insight" not in out
    assert out.index("VS20") < out.index("SK936+SK505")     # shortest first
    assert f"esum gflights returns --search {handle(ROUND)} --id VS20" in out


def test_returns_then_detail(action_runner, fake_http, gf):
    pinned = pin(ROUND, 0, UA901)
    fake_http.routes[page_url(ROUND)] = html(page_url(ROUND), load("roundtrip.json"))
    fake_http.routes[page_url(pinned)] = html(page_url(pinned), load("returns.json"))
    out = run(action_runner, gf, "returns", {"search": handle(ROUND), "id": "ua 901"})
    assert 'outbound: "UA901: SFO 12-10 12:50 → LHR 12-11 07:25 · 10h35m · nonstop · UA901"' in out
    assert out.index("LH2485+LH458,USD985") < out.index("UA900")
    assert f"esum gflights detail --search {handle(pinned)} --id LH2485+LH458" in out

    out = run(action_runner, gf, "detail", {"search": handle(pinned), "id": "LH2485+LH458"})
    assert "Itinerary USD985 round trip: UA901 | LH2485+LH458 (economy)" in out
    assert "Outbound: SFO 12-10 12:50 → LHR 12-11 07:25" in out and "Return: LHR 12-17 06:35 → SFO" in out
    assert "  layover 7h10m MUC" in out and "also sold as LH9375 OS7856 SN9105" in out
    both = pin(pinned, 1, [Leg("LHR", "2026-12-17", "MUC", "LH", "2485"), Leg("MUC", "2026-12-17", "SFO", "LH", "458")])
    assert f"Book (airline and agency fares): {booking_url(both)}" in out


def test_detail_of_an_outbound_points_to_returns(action_runner, fake_http, gf):
    fake_http.routes[page_url(ROUND)] = html(page_url(ROUND), load("roundtrip.json"))
    out = run(action_runner, gf, "detail", {"search": handle(ROUND), "id": "SK936+SK505"})
    assert "Itinerary USD743 round trip from: SK936+SK505" in out and "Book" not in out
    assert f"esum gflights returns --search {handle(ROUND)} --id SK936+SK505" in out


def test_gone_itinerary(action_runner, fake_http, capsys):
    fake_http.routes[page_url(ONE_WAY)] = html(page_url(ONE_WAY), load("oneway.json"))
    assert action_runner.cli(["gflights", "detail", "--search", handle(ONE_WAY), "--id", "UA1"]) == 1
    out = capsys.readouterr().out
    assert "no itinerary 'UA1' on this search's first page" in out
    assert "`esum gflights search --origin SFO --dest JFK --depart 2026-12-10`" in out
    from esum.sites.gflights import _relist
    assert _relist(pin(ROUND, 0, UA901)) == f"esum gflights returns --search {handle(ROUND)} --id SFO-LHR:2026-12-10:UA901"


@pytest.mark.parametrize("argv,text", [
    (["returns", "--search", handle(ONE_WAY), "--id", "AA148"],
     "one-way search; it has no returns"),
    (["detail", "--search", "nonsense", "--id", "AA148"], "is not a search value"),
    (["search", "--origin", "SFO", "--dest", "JFK", "--depart", "2026-10-10"], "is in the past"),
    (["search", "--origin", "SF", "--dest", "JFK", "--depart", "2026-12-10"], "3-letter airport or city codes"),
    (["search", "--origin", "SFO", "--dest", "JFK", "--depart", "2026-12-10", "--adults", "0"], "1 to 9"),
    (["dates", "--origin", "SFO", "--dest", "JFK", "--start", "2026-12-01", "--end", "2027-01-15"], "most is 31"),
])
def test_bad_input_exits_2(action_runner, capsys, argv, text):
    assert action_runner.cli(["gflights", *argv]) == 2
    assert text in capsys.readouterr().out


def test_google_errors_and_empty_pages(action_runner, fake_http, gf, capsys):
    far = Query((Segment(("SFO",), ("JFK",), "2027-12-10"),))
    err = load("error_past.json")
    err[2][0][1][1] = 3
    fake_http.routes[page_url(far)] = html(page_url(far), err)
    assert action_runner.cli(["gflights", "search", "--origin", "SFO", "--dest", "JFK", "--depart", "2027-12-10"]) == 2
    assert "too far ahead" in capsys.readouterr().out

    empty = Query((Segment(("SFO",), ("XQZ",), "2026-12-10"),))
    fake_http.routes[page_url(empty)] = html(page_url(empty), load("empty.json"))
    out = run(action_runner, gf, "search", {"origin": "SFO", "dest": "XQZ", "depart": "2026-12-10"})
    no_route = out
    assert "knows no route between these codes" in out
    assert "esum gflights dates" not in out

    fake_http.routes[page_url(ONE_WAY)] = Response(200, page_url(ONE_WAY), {"content-type": "text/html"},
                                                   b"<html>Before you continue</html>")
    assert action_runner.cli(["gflights", "search", "--origin", "SFO", "--dest", "JFK", "--depart", "2026-12-10"]) == 1
    assert "no ds:1 data" in capsys.readouterr().out
    require_site("matrix")
    assert "esum matrix airports" in no_route


def test_dates_keeps_going_when_one_date_fails(action_runner, fake_http, gf, monkeypatch):
    monkeypatch.setattr("esum.transport.TRANSIENT_WAITS", (0.0, 0.0))
    days = [date(2026, 12, 10) + timedelta(days=i) for i in range(3)]
    queries = [Query((Segment(("SFO",), ("JFK",), d.isoformat()),)) for d in days]
    for q in queries[:2]:
        fake_http.routes[page_url(q)] = html(page_url(q), load("oneway.json"))
    fake_http.routes[page_url(queries[2])] = httpx.ReadTimeout("timed out")
    out = run(action_runner, gf, "dates", {"origin": "SFO", "dest": "JFK", "start": "2026-12-10", "end": "2026-12-12"})
    assert '2026-12-10,USD174,typical,"SFO 12-10 16:05 → JFK 12-11 00:30 · 5h25m · nonstop · AA148"' in out
    assert '2026-12-12,search failed (ReadTimeout),' in out
    assert 'cheapest: "USD174 on 2026-12-10, 2026-12-11"' in out and "failed: 2026-12-12" in out
    assert f"esum gflights detail --search {handle(queries[0])} --id AA148" in out


# ---- dogfood round 1 -------------------------------------------------------------------------

def test_rows_carry_aircraft_and_seats():
    lax = next(r for r in flights(load("oneway.json")) if r["id"] == "AA6266+AA4")
    assert lax["aircraft"] == "Embraer 175 | Boeing 777"
    assert lax["seats"] == "average legroom | average legroom"
    assert lax["cabins"] == "economy | economy"


def test_json_has_the_insight_as_data(action_runner, fake_http, gf):
    fake_http.routes[page_url(ONE_WAY)] = html(page_url(ONE_WAY), load("oneway.json"))
    data = json.loads(run(action_runner, gf, "search", {"origin": "SFO", "dest": "JFK", "depart": "2026-12-10"},
                          json_out=True))["data"]
    assert data["insight"]["level"] == "typical" and data["insight"]["usual"] == 181
    assert data["insight"]["history"][0] == {"date": "2026-09-07", "price": 195}


def test_more_rows_than_the_limit_says_so(action_runner, fake_http, gf):
    fake_http.routes[page_url(ONE_WAY)] = html(page_url(ONE_WAY), load("oneway.json"))
    out = run(action_runner, gf, "search", {"origin": "SFO", "dest": "JFK", "depart": "2026-12-10", "limit": 3})
    assert "--limit 6" in out


def test_detail_says_operated_by_once(action_runner, fake_http, gf):
    fake_http.routes[page_url(ONE_WAY)] = html(page_url(ONE_WAY), load("oneway.json"))
    out = run(action_runner, gf, "detail", {"search": handle(ONE_WAY), "id": "AA6266+AA4"})
    assert out.count("SkyWest Airlines as American Eagle") == 1


def test_mixed_cabin_is_flagged(action_runner, fake_http, gf):
    data = load("oneway.json")
    for row in data[3][0]:
        if row[0][2][0][22][1] == "6266":
            row[0][2][0][16] = 4          # the short leg sold in first, as Google does on business searches
    fake_http.routes[page_url(ONE_WAY)] = html(page_url(ONE_WAY), data)
    out = run(action_runner, gf, "detail", {"search": handle(ONE_WAY), "id": "AA6266+AA4"})
    assert "note: mixed cabin: AA6266 first" in out


def test_dates_names_every_cheapest_day(action_runner, fake_http, gf):
    for d in ("2026-12-10", "2026-12-11"):
        q = Query((Segment(("SFO",), ("JFK",), d),))
        fake_http.routes[page_url(q)] = html(page_url(q), load("oneway.json"))
    out = run(action_runner, gf, "dates", {"origin": "SFO", "dest": "JFK", "start": "2026-12-10", "end": "2026-12-11"})
    assert 'cheapest: "USD174 on 2026-12-10, 2026-12-11"' in out


# ---- dogfood round 2 -------------------------------------------------------------------------

def test_rows_carry_amenities():
    lax = next(r for r in flights(load("oneway.json")) if r["id"] == "AA6266+AA4")
    assert lax["amenities"] == "power and USB, stream to device, free Wi-Fi | power and USB, on-demand video, paid Wi-Fi"


def test_columns_follow_the_search(action_runner, fake_http, gf):
    business = Query(ONE_WAY.segments, cabin=3)
    data = load("oneway.json")
    for row in data[3][0]:
        if row[0][2][0][22][1] == "6266":
            row[0][2][0][16] = 4
    fake_http.routes[page_url(business)] = html(page_url(business), data)
    out = run(action_runner, gf, "search", {"origin": "SFO", "dest": "JFK", "depart": "2026-12-10", "cabin": "business"})
    assert "itineraries[6]{id,price,itinerary,seats,warnings}:" in out
    fake_http.routes[page_url(ONE_WAY)] = html(page_url(ONE_WAY), load("oneway.json"))
    out = run(action_runner, gf, "search", {"origin": "SFO", "dest": "JFK", "depart": "2026-12-10", "limit": 2})
    assert "itineraries[2]{id,price,itinerary}:" in out      # no warnings on the rows shown


def test_dates_without_an_insight_leave_level_empty(action_runner, fake_http, gf):
    fake_http.routes[page_url(ROUND)] = html(page_url(ROUND), load("roundtrip.json"))
    out = run(action_runner, gf, "dates", {"origin": "SFO", "dest": "LHR", "start": "2026-12-10", "end": "2026-12-10",
                                           "nights": 7})
    assert "USD743" in out and "null" not in out


def test_detail_says_the_price_is_for_everyone(action_runner, fake_http, gf):
    two = Query(ONE_WAY.segments, adults=2)
    fake_http.routes[page_url(two)] = html(page_url(two), load("oneway.json"))
    out = run(action_runner, gf, "detail", {"search": handle(two), "id": "AA148"})
    assert "Itinerary USD174 one way: AA148 (total for 2 adults, economy)" in out


def test_past_date_names_the_window(action_runner, capsys):
    assert action_runner.cli(["gflights", "search", "--origin", "SFO", "--dest", "JFK", "--depart", "2026-10-10"]) == 2
    assert "from today (2026-11-06) to about 330 days ahead" in capsys.readouterr().out


# ---- dogfood round 3 -------------------------------------------------------------------------

def test_dates_flags_a_mixed_cabin_cheapest(action_runner, fake_http, gf):
    business = Query(ONE_WAY.segments, cabin=3)
    data = load("oneway.json")
    for row in data[2][0]:
        if row[0][2][0][22][1] == "148":
            row[0][2][0][16] = 1          # sold in economy on a business search
    fake_http.routes[page_url(business)] = html(page_url(business), data)
    out = run(action_runner, gf, "dates", {"origin": "SFO", "dest": "JFK", "start": "2026-12-10", "end": "2026-12-10",
                                           "cabin": "business"})
    assert "days[1]{date,price,level,flight,warnings}:" in out and "no business on this itinerary: sold as economy" in out


def test_known_codes_without_flights_offer_nearby_dates(action_runner, fake_http, gf):
    data = load("oneway.json")
    data[2] = data[3] = None              # Google knew both airports and had no flights that day
    fake_http.routes[page_url(ONE_WAY)] = html(page_url(ONE_WAY), data)
    out = run(action_runner, gf, "search", {"origin": "SFO", "dest": "JFK", "depart": "2026-12-10"})
    assert "esum gflights dates --origin SFO --dest JFK --start 2026-12-07 --end 2026-12-13" in out
    assert "(7 searches)" in out and "knows no route" not in out


# ---- dogfood round 4 -------------------------------------------------------------------------

def test_round_trip_dates_lead_to_returns(action_runner, fake_http, gf):
    fake_http.routes[page_url(ROUND)] = html(page_url(ROUND), load("roundtrip.json"))
    out = run(action_runner, gf, "dates", {"origin": "SFO", "dest": "LHR", "start": "2026-12-10", "end": "2026-12-10",
                                           "nights": 7})
    assert f"esum gflights returns --search {handle(ROUND)} --id SK936+SK505" in out


def test_stops_header_reads_as_a_ceiling(action_runner, fake_http, gf):
    q = Query((Segment(("SFO",), ("JFK",), "2026-12-10", max_stops=1),))
    fake_http.routes[page_url(q)] = html(page_url(q), load("oneway.json"))
    out = run(action_runner, gf, "search", {"origin": "SFO", "dest": "JFK", "depart": "2026-12-10", "stops": 1})
    assert "stops: at most 1" in out


def test_co2_of_a_party_says_so(action_runner, fake_http, gf):
    two = Query(ONE_WAY.segments, adults=2)
    fake_http.routes[page_url(two)] = html(page_url(two), load("oneway.json"))
    out = run(action_runner, gf, "detail", {"search": handle(two), "id": "AA6266+AA4"})
    assert "393 kg CO2e for all passengers (typical 362)" in out


# ---- codex review of b7811d2 -----------------------------------------------------------------

def test_dates_fails_when_every_date_failed(action_runner, fake_http, capsys):
    q = Query((Segment(("SFO",), ("JFK",), "2026-12-10"),))
    fake_http.routes[page_url(q)] = Response(200, page_url(q), {"content-type": "text/html"}, b"<html>consent</html>")
    argv = ["gflights", "dates", "--origin", "SFO", "--dest", "JFK", "--start", "2026-12-10", "--end", "2026-12-10"]
    assert action_runner.cli(argv) == 1
    out = capsys.readouterr().out
    assert "error:" in out and "no ds:1 data" in out and "help:" in out

    err = load("error_past.json")
    err[2][0][1][1] = 3
    fake_http.routes[page_url(q)] = html(page_url(q), err)
    assert action_runner.cli(argv) == 2
    assert "too far ahead" in capsys.readouterr().out


def test_empty_returns_keep_the_search_settings(action_runner, fake_http, gf):
    q = Query(tuple(replace(s, max_stops=1) for s in ROUND.segments), adults=2, cabin=3, currency="EUR",
              one_way=False)
    data = load("roundtrip.json")
    fake_http.routes[page_url(q)] = html(page_url(q), data)
    pinned = pin(q, 0, UA901)
    fake_http.routes[page_url(pinned)] = html(page_url(pinned), load("empty.json"))
    out = run(action_runner, gf, "returns", {"search": handle(q), "id": "UA901"})
    assert ("esum gflights search --origin SFO --dest LHR --depart 2026-12-10 --return 2026-12-17 --cabin business "
            "--stops 1 --adults 2 --currency EUR") in out


@pytest.mark.parametrize("bad", ["GgGAmAEC", "GgMiAQoYAQ", "GgVyA4oBCpgBAQ"])
def test_truncated_nested_fields_are_bad_handles(bad, action_runner, capsys):
    with pytest.raises(ValueError):
        parse_handle(bad)
    assert action_runner.cli(["gflights", "detail", "--search", bad, "--id", "AA148"]) == 2
    assert "is not a search value" in capsys.readouterr().out


# ---- dogfood round 5 (three parallel testers) ------------------------------------------------

def test_sort_best_points_at_the_first_row_shown(action_runner, fake_http, gf):
    pinned = pin(ROUND, 0, UA901)
    fake_http.routes[page_url(ROUND)] = html(page_url(ROUND), load("roundtrip.json"))
    fake_http.routes[page_url(pinned)] = html(page_url(pinned), load("returns.json"))
    out = run(action_runner, gf, "returns", {"search": handle(ROUND), "id": "UA901", "sort": "best"})
    assert out.index("LX317+LX38,") < out.index("LH2485+LH458,")      # Google's order, not the cheapest first
    assert "--id LX317+LX38` to see the whole trip" in out


def test_a_changed_search_value_is_rejected(action_runner, capsys):
    good = handle(ONE_WAY)
    flipped = good.replace("U0ZP", "T1JF")            # SFO → ORE: still a valid route
    assert parse_handle(good) == ONE_WAY
    with pytest.raises(ValueError):
        parse_handle(flipped)
    assert action_runner.cli(["gflights", "detail", "--search", flipped, "--id", "AA148"]) == 2
    assert "is not a search value" in capsys.readouterr().out


def test_a_failed_date_reads_short(action_runner, fake_http, gf, monkeypatch):
    days = ["2026-12-10", "2026-12-11"]
    q0, q1 = (Query((Segment(("SFO",), ("JFK",), d),)) for d in days)
    fake_http.routes[page_url(q0)] = html(page_url(q0), load("oneway.json"))
    err = load("error_past.json")
    err[2][0][1][1] = 3
    fake_http.routes[page_url(q1)] = html(page_url(q1), err)
    out = run(action_runner, gf, "dates", {"origin": "SFO", "dest": "JFK", "start": days[0], "end": days[1]})
    assert "2026-12-11,search failed (date too far ahead)," in out
    assert "ArgumentError" not in out and "null" not in out


def test_too_far_names_the_window(action_runner, fake_http, capsys):
    far = Query((Segment(("SFO",), ("JFK",), "2027-12-10"),))
    err = load("error_past.json")
    err[2][0][1][1] = 3
    fake_http.routes[page_url(far)] = html(page_url(far), err)
    assert action_runner.cli(["gflights", "search", "--origin", "SFO", "--dest", "JFK", "--depart", "2027-12-10"]) == 2
    assert "help: Give dates up to about 330 days ahead (about 2027-10-02)" in capsys.readouterr().out


def test_json_insight_has_its_currency(action_runner, fake_http, gf):
    fake_http.routes[page_url(ONE_WAY)] = html(page_url(ONE_WAY), load("oneway.json"))
    data = json.loads(run(action_runner, gf, "search", {"origin": "SFO", "dest": "JFK", "depart": "2026-12-10"},
                          json_out=True))["data"]
    assert data["insight"]["currency"] == "USD"


def test_returns_on_a_one_way_value_suggests_a_round_trip(action_runner, capsys):
    assert action_runner.cli(["gflights", "returns", "--search", handle(ONE_WAY), "--id", "AA148"]) == 2
    assert "--return <YYYY-MM-DD>" in capsys.readouterr().out


# ---- dogfood round 6 (three more testers) ----------------------------------------------------

def _set_leg(data, number: str, index: int, value) -> None:
    for block in (2, 3):
        for row in data[block][0]:
            for leg in row[0][2]:
                if leg[22][1] == number:
                    leg[index] = value


def test_operator_text_is_cleaned():
    data = load("oneway.json")
    _set_leg(data, "6266", 2, "Operated by Norse Atlantic Airways as")
    lax = next(r for r in flights(data) if r["id"] == "AA6266+AA4")
    assert lax["legs"][0]["operated_by"] == "Norse Atlantic Airways"
    assert lax["warnings"] == ["AA6266 operated by Norse Atlantic Airways"]


def test_a_flight_wholly_in_another_cabin_is_not_mixed(action_runner, fake_http, gf):
    data = load("oneway.json")
    _set_leg(data, "148", 16, 2)
    fake_http.routes[page_url(ONE_WAY)] = html(page_url(ONE_WAY), data)
    out = run(action_runner, gf, "detail", {"search": handle(ONE_WAY), "id": "AA148"})
    assert "note: no economy on this itinerary: sold as premium" in out and "mixed cabin" not in out


def test_price_ties_list_earliest_first(action_runner, fake_http, gf):
    fake_http.routes[page_url(ONE_WAY)] = html(page_url(ONE_WAY), load("oneway.json"))
    out = run(action_runner, gf, "search", {"origin": "SFO", "dest": "JFK", "depart": "2026-12-10"})
    tied = [x for x in ("AA2614+AA2882", "AA6266+AA4", "AA6316+AA28") if x in out]
    assert sorted(tied, key=out.index) == ["AA2614+AA2882", "AA6266+AA4", "AA6316+AA28"]   # 06:11, 10:44, 12:00


def test_an_airport_change_shows_in_the_row():
    data = load("oneway.json")
    _set_leg(data, "4", 3, "LGB")          # the second flight leaves from another airport than the first lands at
    lax = next(r for r in flights(data) if r["id"] == "AA6266+AA4")
    assert "via LAX/LGB" in lax["itinerary"] and "change of airport" in lax["warnings"]


def test_json_has_no_null_insight_and_dates_warnings_are_lists(action_runner, fake_http, gf):
    pinned = pin(ROUND, 0, UA901)
    fake_http.routes[page_url(ROUND)] = html(page_url(ROUND), load("roundtrip.json"))
    fake_http.routes[page_url(pinned)] = html(page_url(pinned), load("returns.json"))
    data = json.loads(run(action_runner, gf, "returns", {"search": handle(ROUND), "id": "UA901"}, json_out=True))["data"]
    assert "insight" not in data
    fake_http.routes[page_url(ONE_WAY)] = html(page_url(ONE_WAY), load("oneway.json"))
    data = json.loads(run(action_runner, gf, "dates", {"origin": "SFO", "dest": "JFK", "start": "2026-12-10",
                                                       "end": "2026-12-10"}, json_out=True))["data"]
    assert data["days"][0]["warnings"] == []


# ---- the booking page (page script) ------------------------------------------------------------

class BookingTab:
    def __init__(self, session, url):
        self.session, self.url, self.closed = session, url, False

    def evaluate(self, fn, arg=None):
        self.session.scripts.append((fn, arg))
        return self.session.page

    def close(self):
        self.closed = True


class Browser:
    """ESUM's browser session as a page script sees it: tabs opened on a URL, run in, closed."""

    def __init__(self, text: str | None = None):
        booking = (FIX / "booking_united.txt").read_text(encoding="utf-8")
        self.page = {"ready": text is None, "text": booking if text is None else text}
        self.tabs: list[BookingTab] = []
        self.scripts: list[tuple] = []

    def open_tab(self, url, cleared=None, *, site=""):
        self.tabs.append(BookingTab(self, url))
        return self.tabs[-1]


def fares(action_runner, fake_http, gf, browser, **kwargs) -> str:
    pinned = pin(ROUND, 0, UA901)
    fake_http.routes[page_url(ROUND)] = html(page_url(ROUND), load("roundtrip.json"))
    fake_http.routes[page_url(pinned)] = html(page_url(pinned), load("returns.json"))
    out = io.StringIO()
    action_runner(gf, gf.all_actions["fares"], {"search": handle(pinned), "id": "LH2485+LH458", **kwargs}, Options(),
                  stream=out, session=browser)
    return out.getvalue()


def test_fares_lists_the_airlines_brands_and_the_agencies_from_the_booking_page(action_runner, fake_http, gf):
    browser = Browser()
    out = fares(action_runner, fake_http, gf, browser)
    both = pin(pin(ROUND, 0, UA901), 1,
               [Leg("LHR", "2026-12-17", "MUC", "LH", "2485"), Leg("MUC", "2026-12-17", "SFO", "LH", "458")])
    assert [(tab.url, tab.closed) for tab in browser.tabs] == [(booking_url(both), True)]
    assert 'itinerary: "USD985 round trip: UA901 | LH2485+LH458"' in out and "prices: total for 1 adult" in out
    assert '  United (airline),Basic Economy,"$1,859",' in out and "Ticket changes for a fee; No refunds" in out
    assert '  United (airline),Economy Fully Refundable,"$2,489",' in out and "Full refunds" in out
    assert '"United, wego","","$3,209",' in out and "Separate tickets: The 2 tickets in your itinerary must be" in out
    assert "Bag fees may be higher" not in out and "Continue" not in out


def test_fares_says_when_the_page_lists_no_brands(action_runner, fake_http, gf):
    text = "\n".join(["Booking options", "Book with EVA AirAirline", "$1,253", "Continue", "View options",
                      "Book with Adam Vacations", "$1,170", "Continue", "View options", "17 more booking options"])
    out = fares(action_runner, fake_http, gf, Browser(text=text))
    assert 'brands: "none on Google\'s page for this trip: each seller\'s one price, the airline\'s too"' in out
    assert '  EVA Air (airline),"","$1,253",""' in out
    assert "brands:" not in fares(action_runner, fake_http, gf, Browser())


def test_fares_wants_a_whole_trip(action_runner, fake_http, gf):
    fake_http.routes[page_url(ROUND)] = html(page_url(ROUND), load("roundtrip.json"))
    browser = Browser()
    with pytest.raises(Exception) as e:
        action_runner(gf, gf.all_actions["fares"], {"search": handle(ROUND), "id": "SK936+SK505"}, Options(),
                      stream=io.StringIO(), session=browser)
    assert "SK936+SK505 is the outbound of a round trip" in str(e.value) and "gflights returns" in e.value.help
    assert browser.tabs == []


def test_fares_says_when_the_page_lists_no_options_and_closes_its_tab(action_runner, fake_http, gf):
    browser = Browser(text="Itinerary summary\nLoading results")
    with pytest.raises(Exception) as e:
        fares(action_runner, fake_http, gf, browser)
    assert "Google's booking page listed no booking options" in str(e.value)
    assert [tab.closed for tab in browser.tabs] == [True]


def test_detail_of_a_whole_trip_points_to_fares(action_runner, fake_http, gf):
    pinned = pin(ROUND, 0, UA901)
    fake_http.routes[page_url(ROUND)] = html(page_url(ROUND), load("roundtrip.json"))
    fake_http.routes[page_url(pinned)] = html(page_url(pinned), load("returns.json"))
    out = run(action_runner, gf, "detail", {"search": handle(pinned), "id": "LH2485+LH458"})
    assert f"esum gflights fares --search {handle(pinned)} --id LH2485+LH458" in out


def test_booking_options_reads_other_currencies_and_a_seller_with_one_price():
    text = "\n".join(["Booking options", "Book with LufthansaAirline", "Hide options", "Economy Light", "€1.234",
                      "No refunds", "Continue", "Book with Expedia", "CA$1,300", "Continue", "View options",
                      "2 more booking options", "$99", "Continue"])
    assert booking_options(text) == [
        {"seller": "Lufthansa", "airline": True, "brand": "Economy Light", "price": "€1.234", "terms": ["No refunds"]},
        {"seller": "Expedia", "airline": False, "brand": "", "price": "CA$1,300", "terms": []}]


def test_booking_options_keeps_the_brand_after_one_below_the_searched_cabin():
    """A business search lists Premium Economy first, closed by "Continue anyway", not "Continue"."""
    rows = booking_options((FIX / "booking_united_business.txt").read_text(encoding="utf-8"))
    united = [(r["brand"], r["price"]) for r in rows if r["seller"] == "United"]
    assert united == [("Premium Economy", "$4,556"), ("Business", "$8,149"), ("Business Fully Refundable", "$8,999")]
    assert rows[0]["terms"][-1] == "Not included: Business"
    assert rows[1]["terms"][:2] == ["Free change, possible fare difference", "No refunds"]
    assert [(r["seller"], r["price"]) for r in rows[3:]] == [
        ("Globehunters", "$7,429"), ("Booking.com", "$8,164"), ("business-class.com", "$7,511"), ("Expedia", "$8,164")]


def test_a_page_script_runs_on_its_own_built_in_site_only(gf):
    from esum.core.context import Context
    from esum.transport import HttpClient

    browser = Browser()
    ctx = Context(site=gf, action=gf.all_actions["fares"], options=Options(),
                  http=HttpClient(gf, Options(), dedupe=True, session=browser))
    with pytest.raises(ValueError, match="runs on its own site"):
        ctx.page_script("https://example.com/x", "() => 1")
    custom = replace(gf, builtin=False)
    ctx = Context(site=custom, action=gf.all_actions["fares"], options=Options(),
                  http=HttpClient(custom, Options(), dedupe=True, session=browser))
    with pytest.raises(Exception, match="page scripts run for built-in sites only"):
        ctx.page_script(booking_url(ROUND), "() => 1")
    assert browser.tabs == []


# ---- fares by the flights themselves -----------------------------------------------------------

OUT, BACK, NEXT_DAY = (str(date.today() + timedelta(days=n)) for n in (60, 70, 61))


def test_fares_takes_the_flights_themselves_without_a_search(action_runner, fake_http, gf):
    browser = Browser()         # fake_http has no routes: a search before the booking page would fail
    out = io.StringIO()
    action_runner(gf, gf.all_actions["fares"],
                  {"flights": [f"sfo-dub:{OUT}:ei60 + DUB-FCO:{NEXT_DAY}:EI0406", f"FCO-SFO:{BACK}:UA508"],
                   "cabin": "business", "adults": 2, "currency": "eur"}, Options(), stream=out, session=browser)
    trip = Query((Segment(("SFO",), ("FCO",), OUT, (Leg("SFO", OUT, "DUB", "EI", "60"),
                                                     Leg("DUB", NEXT_DAY, "FCO", "EI", "406"))),
                  Segment(("FCO",), ("SFO",), BACK, (Leg("FCO", BACK, "SFO", "UA", "508"),))),
                 adults=2, cabin=3, currency="EUR", one_way=False)
    assert [(tab.url, tab.closed) for tab in browser.tabs] == [(booking_url(trip), True)]
    text = out.getvalue()
    assert (f'itinerary: "round trip: SFO→DUB {OUT} EI60 + DUB→FCO {NEXT_DAY} EI406 | FCO→SFO {BACK} UA508"'
            in text)
    assert "cabin: business" in text and "prices: total for 2 adults" in text
    assert '  United (airline),Basic Economy,"$1,859",' in text
    assert (f"esum gflights search --origin SFO --dest FCO --depart {OUT} --return {BACK} --cabin business "
            f"--adults 2 --currency EUR`") in text


def test_fares_of_named_flights_with_no_options_points_to_search(action_runner, fake_http, gf):
    browser = Browser(text="Itinerary summary\nLoading results")
    with pytest.raises(Exception) as e:
        action_runner(gf, gf.all_actions["fares"], {"flights": [f"SFO-NRT:{OUT}:UA9999"]}, Options(),
                      stream=io.StringIO(), session=browser)
    assert f"listed no booking options for one way: SFO→NRT {OUT} UA9999" in str(e.value)
    assert f"`esum gflights search --origin SFO --dest NRT --depart {OUT}`" in e.value.help
    assert [tab.closed for tab in browser.tabs] == [True]


@pytest.mark.parametrize("argv,text", [
    ([], "name the trip: --flights, or --search with --id"),
    (["--search", handle(ONE_WAY)], "name the trip: --flights, or --search with --id"),
    (["--flights", f"SFO-NRT:{OUT}:UA837", "--search", handle(ONE_WAY), "--id", "AA148"], "not both"),
    (["--search", handle(ONE_WAY), "--id", "AA148", "--cabin", "business"], "--cabin with --search"),
    (["--flights", f"SFO-NRT:{OUT}"], f"'SFO-NRT:{OUT}' is not ORIGIN-DEST:YYYY-MM-DD:FLIGHT"),
    (["--flights", f"SFO:{OUT}:UA837"], "is not ORIGIN-DEST:YYYY-MM-DD:FLIGHT"),
    (["--flights", f"SFO-NRT:{OUT}:837"], "is not ORIGIN-DEST:YYYY-MM-DD:FLIGHT"),
    (["--flights", f"SFO-DUB:{OUT}:EI60+"], "a flight is missing around a `+`"),
    (["--flights", "SFO-NRT:2026-10-10:UA837"], "is in the past"),
    (["--flights", f"SFO-DUB:{NEXT_DAY}:EI60+DUB-FCO:{OUT}:EI406"], "departs before the flight ahead of it"),
    (["--flights", f"SFO-NRT:{OUT}:UA837", f"NRT-SIN:{OUT}:SQ11", f"SIN-SFO:{BACK}:SQ32"], "3 directions"),
    (["--flights", f"SFO-NRT:{OUT}:UA837", "--adults", "12"], "1 to 9"),
])
def test_fares_bad_input_exits_2_with_help(action_runner, capsys, argv, text):
    assert action_runner.cli(["gflights", "fares", *argv]) == 2
    out = capsys.readouterr().out
    assert text in out and "help: " in out


def test_a_code_error_names_no_matrix_command_when_matrix_is_not_installed(monkeypatch):
    """The help line names `esum matrix airports` (D-93); without matrix here the error stands alone."""
    import esum.sites.gflights as gflights
    from esum.core import registry
    here = registry.get_registry()
    monkeypatch.setattr(here, "sites", {k: v for k, v in here.sites.items() if k != "matrix"})
    with pytest.raises(Exception) as e:
        gflights._codes("San Francisco", "--origin")
    assert "give 3-letter airport or city codes" in str(e.value) and not e.value.help
