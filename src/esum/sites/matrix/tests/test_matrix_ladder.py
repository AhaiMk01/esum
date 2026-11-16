from __future__ import annotations

import io
import json
import re
from typing import NamedTuple

import httpx
import pytest

from conftest import FakeBackend, json_response
from esum import ArgumentError, Options
from support import needs_site
from esum.core.registry import Registry
from esum.sites.matrix import API
from esum.sites.matrix.parse import Climb, Fare, Rung, Skip, condition, penalty_sections, rung, short_note

PREFIX = "ABCDEFGHIJKLMNOPQRSTU"
NON_REF = "This ticket is non-refundable."
ECONOMY, PREMIUM, BUSINESS, FIRST = "COACH", "PREMIUM-COACH", "BUSINESS", "FIRST"


def filed(title: str, *, refundable: bool = False) -> dict:
    penalties = "CANCELLATIONS\n  BEFORE DEPARTURE\n    CANCELLATIONS PERMITTED.\n       NOTE -\n        FULLY REFUNDABLE." \
        if refundable else "CANCELLATIONS\n  ANY TIME\n    TICKET IS NON-REFUNDABLE."
    return {"fareRules": {"ruleSets": [{"rules": [
        {"category": 0, "type": "GENERAL-RULE", "blocks": [f"{title}      K"]},
        {"category": 16, "type": "GENERAL-RULE", "blocks": [penalties]}]}]}}


BASIC = filed("ECONOMY OW UNBUNDLED FARES")
SELL_UP = filed("ECONOMY SELL-UP RT NON-REF FARES")
STANDARD = filed("ECONOMY RT NON-REF FARES")
FLEXIBLE = filed("BUSINESS UNRESTRICTED FARES", refundable=True)


class Step(NamedTuple):
    n: int
    price: str
    flights: list           # a slice is one flight (booking class, cabin) or a list of them
    rules: list             # one fare a slice
    notes: list[str] = [NON_REF]
    codes: list[str] | None = None      # fare basis codes, else made from the class


def _flights(slice_) -> list[tuple[str, str]]:
    return slice_ if isinstance(slice_, list) else [slice_]


def _page(found) -> list[Step]:
    return found if isinstance(found, list) else [found]


def _search(found) -> dict:
    """A search's answer: its itineraries, cheapest first, in the session of the first."""
    page = _page(found)
    solutions = [{"id": f"{PREFIX}{step.n}001", "displayTotal": step.price,
                  "itinerary": {"carriers": [{"code": "UA"}], "slices": [
                      {"origin": {"code": "SFO"}, "destination": {"code": "NRT"},
                       "departure": "2027-03-10T10:45-08:00", "arrival": "2027-03-11T15:05+09:00", "duration": 680,
                       "flights": [f"UA{step.n}"], "stops": [], "cabins": [cabin for _, cabin in _flights(s)]}
                      for s in step.flights]}} for step in page]
    n = page[0].n
    return {"session": f"S{n}", "solutionSet": f"SET{n}", "solutionCount": len(page),
            "solutionList": {"solutions": solutions, "pages": {"count": 1, "current": 1},
                             "solutionCount": len(page)}}


def _detail(step: Step) -> dict:
    slices = [{"origin": {"code": "SFO"}, "destination": {"code": "NRT"}, "stopCount": 0, "segments": [{
        "carrier": {"code": "UA"}, "flight": {"number": 837}, "origin": {"code": f"A{i}{k}"},
        "destination": {"code": f"B{i}{k}"}, "duration": 680, "legs": [],
        "bookingInfos": [{"bookingCode": code, "cabin": cabin}]} for k, (code, cabin) in enumerate(_flights(s))]}
        for i, s in enumerate(step.flights)]
    fares = [{"key": f"0/{i}", "code": step.codes[i] if step.codes else f"{_flights(s)[-1][0]}LX{i}",
              "carrier": "UA", "originCity": ("SFO", "TYO")[i % 2],
              "destinationCity": ("TYO", "SFO")[i % 2], "displayAdjustedPrice": "USD100.00", "tag": "ROUND-TRIP",
              "bookingInfos": [{"bookingCode": code, "segment": {"origin": f"A{i}{k}", "destination": f"B{i}{k}"}}
                               for k, (code, _) in enumerate(_flights(s))]}
             for i, s in enumerate(step.flights)]
    return {"bookingDetails": {"displayTotal": step.price, "passengerCount": 1, "itinerary": {"slices": slices},
                               "tickets": [{"pricings": [{"fares": fares, "notes": step.notes}]}]}}


def _key(slices: list[dict]) -> tuple | None:
    lines = tuple(s.get("commandLine") for s in slices)
    return None if not any(lines) else lines


def without(*names: str) -> str:
    """The filter a slice gets for these fares, each named without a designator."""
    return "f ~(" + "|".join(f"({n}&~{n}/-)" for n in names) + ")"


class Ladder(FakeBackend):
    def __init__(self, steps: dict[str | None, Step | BaseException]):
        lookup = f"{API}/locationTypes/CITIES_AND_AIRPORTS/partialNames/"
        super().__init__({f"{lookup}{code}/locations?pageSize=10": json_response(
            lookup, {"locations": [{"code": code, "type": "airport", "cityCode": code}]}) for code in ("SFO", "NRT")})
        self.steps = steps
        self.searches: list[list[dict]] = []

    @property
    def filters(self) -> list[tuple | None]:
        """Each search's filter, slice by slice; None for a search with none."""
        return [_key(slices) for slices in self.searches]

    def send(self, request, *, before_send=lambda: None):
        if request.method != "POST":
            return super().send(request, before_send=before_send)
        body = json.loads(request.content)
        if request.url == f"{API}/search":
            slices = body["inputs"]["slices"]
            self.searches.append(slices)
            found = self.steps.get(_key(slices))
            if isinstance(found, BaseException):
                raise found
            if found is None:
                return json_response(request.url, {"session": "S0", "solutionSet": "SET0", "solutionCount": 0,
                                                   "solutionList": {"solutions": []}})
            return json_response(request.url, _search(found))
        asked = body["inputs"]["solution"].split("/")[-1]
        step = next(s for found in self.steps.values() if not isinstance(found, BaseException)
                    for s in _page(found) if f"{PREFIX}{s.n}001" == asked)
        if body["summarizers"] == ["fareRules"]:
            found = step.rules[int(body["inputs"]["fareKeys"][-1])]
            if isinstance(found, BaseException):
                raise found
            return json_response(request.url, found)
        return json_response(request.url, _detail(step))


UA = {"origin": "SFO", "dest": "NRT", "depart": "2027-03-10", "return_": "2027-03-20", "routing": "UA+"}
K_S = Step(1, "USD1472.09", [("K", ECONOMY), ("S", ECONOMY)], [BASIC, BASIC])
L_W = Step(2, "USD1732.09", [("L", ECONOMY), ("W", ECONOMY)], [BASIC, BASIC])
WITHOUT_K_S = (without("UA.SFO+TYO.KLX0"), without("UA.TYO+SFO.SLX1"))
K_S_LEFT_OUT = "1:UA.SFO+TYO.KLX0,2:UA.TYO+SFO.SLX1"


def climb(action_runner, backend, **kwargs) -> str:
    action_runner.backend = backend
    matrix = Registry.discover().get("matrix")
    out = io.StringIO()
    args = {k: v for k, v in {**UA, **kwargs}.items() if v is not None}
    action_runner(matrix, matrix.all_actions["ladder"], args, Options(), stream=out)
    return out.getvalue()


def rows_of(out: str) -> list[str]:
    return [line.strip() for line in out.splitlines() if line.startswith("  ") and ",USD" in line]


def test_each_rung_searches_without_the_fares_below_it(action_runner):
    backend = Ladder({None: K_S, WITHOUT_K_S: L_W})
    text = climb(action_runner, backend, searches=2)
    assert backend.filters == [None, WITHOUT_K_S]
    assert [len(s) for s in backend.searches] == [2, 2]
    rows = rows_of(text)
    assert rows[0].startswith("1,USD1472.09,K | S,KLX0 SLX1,basic,non-refundable,")
    assert rows[1].startswith("2,USD1732.09,L | W,LLX0 WLX1,basic,non-refundable,")
    assert "stopped:" not in text
    assert "esum matrix rules --search S2.SET2." in text and "--id 1 --category 0,16,33`" in text
    assert ("esum matrix ladder --origin SFO --dest NRT --depart 2027-03-10 --return 2027-03-20 --routing UA+ "
            f"--searches 2 --skip {K_S_LEFT_OUT},1:UA.SFO+TYO.LLX0,2:UA.TYO+SFO.WLX1`") in text


def test_until_not_basic_passes_a_higher_cabin_and_stops_at_the_first_standard_fare(action_runner):
    backend = Ladder({
        None: K_S,
        WITHOUT_K_S: Step(2, "USD2666.09", [("R", PREMIUM), ("B", ECONOMY)], [SELL_UP, SELL_UP]),
        (without("UA.SFO+TYO.KLX0", "UA.SFO+TYO.RLX0"), without("UA.TYO+SFO.SLX1")): Step(3, "USD2702.09", [("B", ECONOMY), ("B", ECONOMY)], [SELL_UP, SELL_UP]),
    })
    out = climb(action_runner, backend, searches=5, until="not-basic")
    assert backend.filters == [None, WITHOUT_K_S,
                               (without("UA.SFO+TYO.KLX0", "UA.SFO+TYO.RLX0"), without("UA.TYO+SFO.SLX1"))]
    rows = rows_of(out)
    assert rows[1].startswith('2,USD2666.09,R | B,RLX0 BLX1,"sell-up, R is premium",non-refundable,')
    assert rows[2].startswith("3,USD2702.09,B | B,BLX0 BLX1,sell-up,non-refundable,")
    assert 'stopped: "rung 3 has no basic fare by its rule titles: confirm it in its rules, notes included"' in out
    assert "esum matrix rules --search S3.SET3." in out


def test_until_not_basic_leaves_out_only_the_basic_fares(action_runner):
    backend = Ladder({
        None: Step(1, "USD100.00", [("K", ECONOMY), ("S", ECONOMY)], [STANDARD, BASIC]),
        (None, without("UA.TYO+SFO.SLX1")): Step(2, "USD150.00", [("K", ECONOMY), ("B", ECONOMY)], [STANDARD, STANDARD]),
    })
    out = climb(action_runner, backend, until="not-basic")
    assert backend.filters == [None, (None, without("UA.TYO+SFO.SLX1"))]
    assert rows_of(out)[1].startswith('2,USD150.00,K | B,KLX0 BLX1,"",non-refundable,')
    assert "rung 2 has no basic fare" in out
    assert "--skip 2:UA.TYO+SFO.SLX1,1:UA.SFO+TYO.KLX0,2:UA.TYO+SFO.BLX1 --until not-basic` to climb past rung 2" in out


def test_until_refundable_leaves_out_only_the_fares_whose_penalties_refuse_a_refund(action_runner):
    add_on = [("Z", FIRST), ("J", BUSINESS)]
    backend = Ladder({
        None: Step(1, "USD21315.69", [add_on, ("J", BUSINESS)], [STANDARD, FLEXIBLE]),
        (without("UA.SFO+TYO.JLX0"), None): Step(2, "USD21556.79", [("C", BUSINESS), ("J", BUSINESS)], [FLEXIBLE, FLEXIBLE], notes=[]),
    })
    out = climb(action_runner, backend, cabin="business", until="refundable")
    assert backend.filters == [None, (without("UA.SFO+TYO.JLX0"), None)]     # the flexible J fare coming back stays
    assert rows_of(out)[0].startswith('1,USD21315.69,Z J | J,JLX0 JLX1,"",non-refundable,')
    assert "rung 2 files no non-refundable cancellation statement: confirm it in its rules, notes included" in out


def test_until_refundable_judges_the_fares_and_reports_matrix_note_beside(action_runner):
    backend = Ladder({None: Step(1, "USD4767.69", [("I", BUSINESS), ("I", BUSINESS)], [FLEXIBLE, FLEXIBLE])})
    out = climb(action_runner, backend, cabin="business", until="refundable", searches=2)
    assert backend.filters == [None]
    assert ("rung 1 files no non-refundable cancellation statement: confirm it in its rules, notes included; "
            "Matrix's note calls it non-refundable") in out


def test_a_fare_with_no_cancellation_statement_is_not_refundable(action_runner):
    silent = {"fareRules": {"ruleSets": [{"rules": [
        {"category": 0, "type": "GENERAL-RULE", "blocks": ["BUSINESS FARES      I"]}]}]}}
    backend = Ladder({None: Step(1, "USD4767.69", [("I", BUSINESS), ("I", BUSINESS)], [silent, FLEXIBLE], notes=[])})
    climb(action_runner, backend, cabin="business", until="refundable", searches=2)
    assert backend.filters == [None, (without("UA.SFO+TYO.ILX0"), None)]


def test_a_business_slice_with_a_domestic_connection_in_first_is_business(action_runner):
    backend = Ladder({None: Step(1, "USD21556.79", [[("J", FIRST), ("J", BUSINESS)], [("J", BUSINESS), ("J", FIRST)]],
                                 [FLEXIBLE, FLEXIBLE], notes=[])})
    out = climb(action_runner, backend, cabin="business", until="refundable")
    assert backend.filters == [None]
    assert rows_of(out)[0].startswith('1,USD21556.79,J | J,JLX0 JLX1,"","",')
    assert ',"cancel: before departure: CANCELLATIONS PERMITTED.",1,S1.SET1.' in rows_of(out)[0]
    assert "rung 1 files no non-refundable cancellation statement" in out


def test_penalty_leaves_out_the_fares_that_dont_file_the_words(action_runner):
    backend = Ladder({
        None: Step(1, "USD2276.39", [("L", ECONOMY), ("E", ECONOMY)], [STANDARD, FLEXIBLE]),
        (without("UA.SFO+TYO.LLX0"), None): Step(2, "USD2490.89", [("E", ECONOMY), ("E", ECONOMY)], [FLEXIBLE, FLEXIBLE]),
    })
    out = climb(action_runner, backend, until="cancellations  permitted")
    assert backend.filters == [None, (without("UA.SFO+TYO.LLX0"), None)]     # the E fare that files the words stays
    assert 'stopped: "rung 2 files \\"CANCELLATIONS PERMITTED\\" on every fare: confirm it in its rules, notes included"' in out
    past = next(line for line in out.splitlines() if "to climb past rung 2" in line)
    assert "--skip 1:UA.SFO+TYO.LLX0,1:UA.SFO+TYO.ELX0,2:UA.TYO+SFO.ELX1 " in past and "--until " in past and "cancellations permitted" in past


def test_penalty_says_when_no_rung_fits_yet(action_runner):
    mixed = Step(1, "USD2276.39", [("L", ECONOMY), ("E", ECONOMY)], [STANDARD, FLEXIBLE])
    out = climb(action_runner, Ladder({None: mixed}), searches=1, until="CANCELLATIONS PERMITTED")
    assert "1 rungs climbed, none with" in out and "climb further with the last help line" in out
    assert "esum matrix rules" not in out


def test_penalty_words_no_fare_files_are_called_out(action_runner):
    out = climb(action_runner, Ladder({None: K_S}), searches=1, until="NO CHARGE")
    assert "no fare of 1 rungs files" in out and "check the words against the penalties column" in out


def test_a_rung_filing_what_the_row_above_files_says_so(action_runner):
    out = climb(action_runner, Ladder({None: K_S, WITHOUT_K_S: L_W}), searches=2)
    first, second = rows_of(out)
    assert '"cancel: any time: TICKET IS NON-REFUNDABLE."' in first and ",as above," in second


K_S_STANDARD = Step(7, "USD1732.09", [("K", ECONOMY), ("S", ECONOMY)], [STANDARD, STANDARD], codes=["KFG2", "SFO2"])
B_B = Step(8, "USD2702.09", [("B", ECONOMY), ("B", ECONOMY)], [SELL_UP, SELL_UP])


def test_one_search_gives_the_failing_fares_of_several_prices(action_runner):
    both = (without("UA.SFO+TYO.KLX0", "UA.SFO+TYO.LLX0"), without("UA.TYO+SFO.SLX1", "UA.TYO+SFO.WLX1"))
    backend = Ladder({None: [K_S, L_W], both: K_S_STANDARD})
    out = climb(action_runner, backend, until="not-basic")
    assert backend.filters == [None, both]
    assert [r.split(",")[1] for r in rows_of(out)] == ["USD1472.09", "USD1732.09", "USD1732.09"]
    assert "rung 3 has no basic fare by its rule titles" in out


def test_a_rung_that_fits_down_a_search_waits_for_the_search_that_puts_it_first(action_runner):
    backend = Ladder({None: [K_S, B_B], WITHOUT_K_S: [K_S_STANDARD, B_B]})
    out = climb(action_runner, backend, until="not-basic")
    assert backend.filters == [None, WITHOUT_K_S]
    assert [r.split(",")[:2] for r in rows_of(out)] == [["1", "USD1472.09"], ["2", "USD1732.09"], ["3", "USD2702.09"]]
    assert "rung 2 has no basic fare by its rule titles" in out      # cheaper than the first one that fit
    assert "esum matrix rules --search S7.SET7 " in out


def test_a_rung_that_fits_down_the_last_search_is_not_yet_the_cheapest(action_runner):
    out = climb(action_runner, Ladder({None: [K_S, B_B]}), until="not-basic", searches=1)
    assert "rung 2 fits (not basic) and may not be the cheapest: a fare behind a cheaper rung" in out
    assert "flights can still fit. Climb further with the last help line" in out
    assert f"--searches 1 --skip {K_S_LEFT_OUT} --until not-basic`" in out
    assert "esum matrix rules --search S1.SET1 " in out


def test_a_search_that_puts_the_candidate_first_confirms_it(action_runner):
    backend = Ladder({None: [K_S, B_B], WITHOUT_K_S: B_B})
    out = climb(action_runner, backend, until="not-basic")
    assert len(rows_of(out)) == 2 and "rung 2 has no basic fare by its rule titles" in out


def test_a_candidate_found_again_cheaper_is_answered_at_the_cheaper_itinerary(action_runner):
    cheaper = Step(9, "USD2500.00", [("B", ECONOMY), ("B", ECONOMY)], [SELL_UP, SELL_UP])
    out = climb(action_runner, Ladder({None: [K_S, B_B], WITHOUT_K_S: cheaper}), until="not-basic")
    answer = rows_of(out)[1]
    assert answer.startswith("2,USD2500.00,B | B,") and answer.endswith("S9.SET9.ABCDEFGHIJKLMNOPQRSTU9")
    assert "USD2702.09" not in out


def test_two_fares_with_one_code_are_judged_apart(action_runner):
    same_code = Step(1, "USD900.00", [("L", ECONOMY), ("E", ECONOMY)], [STANDARD, FLEXIBLE], codes=["SAME", "SAME"])
    out = climb(action_runner, Ladder({None: same_code}), searches=1, until="CANCELLATIONS PERMITTED")
    assert "1 rungs climbed, none with" in out and "no fare of" not in out


def test_refundable_reads_any_spelling_in_the_cancellation_statements(action_runner):
    spelled = {"fareRules": {"ruleSets": [{"rules": [
        {"category": 0, "type": "GENERAL-RULE", "blocks": ["ECONOMY RT FARES      K"]},
        {"category": 16, "type": "GENERAL-RULE",
         "blocks": [chr(10).join(["CANCELLATIONS", "  ANY TIME", "    FARE IS NONREFUNDABLE."])]}]}]}}
    backend = Ladder({None: Step(1, "USD900.00", [("L", ECONOMY), ("E", ECONOMY)], [spelled, FLEXIBLE])})
    climb(action_runner, backend, searches=2, until="refundable")
    assert backend.filters == [None, (without("UA.SFO+TYO.LLX0"), None)]


def test_until_says_when_no_rung_fits_yet(action_runner):
    backend = Ladder({None: K_S, WITHOUT_K_S: L_W})
    out = climb(action_runner, backend, searches=2, until="refundable")
    assert 'stopped: "2 rungs climbed, none refundable: climb further with the last help line"' in out
    assert "esum matrix rules" not in out
    assert f"--searches 2 --skip {K_S_LEFT_OUT},1:UA.SFO+TYO.LLX0,2:UA.TYO+SFO.WLX1 --until refundable`" in out


def test_skip_takes_fares_with_or_without_a_carrier_and_joins_the_users_extension(action_runner):
    every = "-AIRLINES NH; " + without("..KLX07JBH", "UA..SLW47FBH")
    backend = Ladder({(every, every): L_W})
    out = climb(action_runner, backend, searches=1, skip="klx07jbh, ua.slw47fbh", extension="-AIRLINES NH")
    assert backend.filters == [(every, every)]
    assert 'skipped: "KLX07JBH,UA.SLW47FBH"' in out and "--skip KLX07JBH,UA.SLW47FBH,1:UA.SFO+TYO.LLX0,2:UA.TYO+SFO.WLX1" in out
    assert ",USD1732.09,L | W," in out


def test_one_way_and_multi_city_trips(action_runner):
    one_way = Ladder({None: Step(1, "USD700.00", [("K", ECONOMY)], [BASIC])})
    climb(action_runner, one_way, searches=1, return_=None)
    assert [len(s) for s in one_way.searches] == [1]
    legs = Ladder({None: Step(1, "USD900.00", [("K", ECONOMY), ("S", ECONOMY), ("L", ECONOMY)], [BASIC] * 3)})
    out = climb(action_runner, legs, searches=1, origin=None, dest=None, depart=None, return_=None,
                legs=["SFO-NRT:2027-03-10", "NRT-HKG:2027-03-14", "HKG-SFO:2027-03-20"])
    assert [len(s) for s in legs.searches] == [3]
    assert ("--legs SFO-NRT:2027-03-10 NRT-HKG:2027-03-14 HKG-SFO:2027-03-20 --routing UA+ --searches 1 "
            f"--skip {K_S_LEFT_OUT},3:UA.SFO+TYO.LLX2`") in out


def test_a_failed_first_search_is_the_error(action_runner):
    with pytest.raises(httpx.ReadTimeout):
        climb(action_runner, Ladder({None: httpx.ReadTimeout("timed out")}))


def test_a_later_failure_keeps_the_rungs_found(action_runner):
    out = climb(action_runner, Ladder({None: K_S, WITHOUT_K_S: httpx.ReadTimeout("timed out")}))
    assert ",USD1472.09,K | S," in out
    assert 'stopped: "search 2 failed (ReadTimeout: timed out)"' in out
    assert f"--skip {K_S_LEFT_OUT}`" in out


def test_a_fare_whose_rules_fail_goes_unmarked_unless_until_needs_them(action_runner):
    broken = Step(1, "USD1472.09", [("K", ECONOMY), ("S", ECONOMY)], [httpx.ReadTimeout("timed out"), BASIC])
    assert rows_of(climb(action_runner, Ladder({None: broken}), searches=1))[0].startswith(
        "1,USD1472.09,K | S,KLX0 SLX1,basic,non-refundable,")
    with pytest.raises(httpx.ReadTimeout):
        climb(action_runner, Ladder({None: broken}), searches=1, until="not-basic")


def test_the_ladder_ends_when_nothing_is_left(action_runner):
    out = climb(action_runner, Ladder({None: K_S}))
    assert f'stopped: "nothing left without {K_S_LEFT_OUT}"' in out
    assert "esum matrix ladder" not in out


def test_a_rung_that_comes_back_ends_the_climb(action_runner):
    backend = Ladder({None: K_S, WITHOUT_K_S: K_S})
    out = climb(action_runner, backend)
    assert backend.filters == [None, WITHOUT_K_S]
    assert "the last search names no new fare to leave out, so the ladder can't climb further" in out
    assert ("Matrix listed UA.SFO+TYO.KLX0, UA.TYO+SFO.SLX1 where the filter left it out, so the filter didn't "
            "hold for it") in out
    assert len(rows_of(out)) == 1


def test_a_fare_that_fails_one_way_stays_in_the_other(action_runner):
    """The filter's city pair has no direction, but a slice is flown one way: each slice leaves
    out the fares that failed on it."""
    there_basic = Step(1, "USD900.00", [("K", ECONOMY), ("K", ECONOMY)], [BASIC, STANDARD], codes=["KSAME", "KSAME"])
    back_too = Step(2, "USD950.00", [("L", ECONOMY), ("K", ECONOMY)], [STANDARD, STANDARD], codes=["LOK", "KSAME"])
    backend = Ladder({None: there_basic, (without("UA.SFO+TYO.KSAME"), None): back_too})
    out = climb(action_runner, backend, until="not-basic")
    assert backend.filters == [None, (without("UA.SFO+TYO.KSAME"), None)]
    assert "rung 2 has no basic fare by its rule titles" in out and "the filter" not in out


def test_a_fare_is_left_out_with_its_designator(action_runner):
    designated = Step(1, "USD900.00", [("J", BUSINESS), ("J", BUSINESS)], [BASIC, BASIC],
                      codes=["JX2WC/CH", "CXU0O85J/PAWD"])
    backend = Ladder({None: designated})
    out = climb(action_runner, backend, cabin="business", until="not-basic", searches=2)
    # a code and designator over Matrix's 10 characters goes as every designator of the code
    assert backend.filters == [None, ("f ~(UA.SFO+TYO.JX2WC/CH)", "f ~(UA.TYO+SFO.CXU0O85J/-)")]
    assert "nothing left without 1:UA.SFO+TYO.JX2WC/CH,2:UA.TYO+SFO.CXU0O85J/PAWD" in out


def test_a_fit_the_filter_has_to_leave_out_with_a_failing_fare_is_called_out(action_runner):
    failing = Step(1, "USD900.00", [("J", BUSINESS), ("J", BUSINESS)], [BASIC, STANDARD], codes=["CXU0O85J/PAWD", "OK"])
    fits = Step(2, "USD1000.00", [("J", BUSINESS), ("J", BUSINESS)], [STANDARD, STANDARD], codes=["CXU0O85J/OTHR", "OK"])
    dearer = Step(3, "USD1200.00", [("C", BUSINESS), ("J", BUSINESS)], [STANDARD, STANDARD], codes=["COK", "OK"])
    backend = Ladder({None: [failing, fits], ("f ~(UA.SFO+TYO.CXU0O85J/-)", None): dearer})
    out = climb(action_runner, backend, cabin="business", until="not-basic")
    assert "rung 3 has no basic fare by its rule titles: confirm it in its rules, notes included" in out
    assert ("caution: \"a cheaper rung can exist: the filter can't name 1:UA.SFO+TYO.CXU0O85J/PAWD alone (over 10 characters), so the code's other "
            "fares on the slice went out with it, UA.SFO+TYO.CXU0O85J/OTHR that meets the condition among them\"") in out
    assert rows_of(out)[1].startswith("2,USD1000.00,")


def test_the_printed_continuation_searches_what_the_next_rung_would(action_runner):
    """A fare basis can be two characters; the help line's names go back in as they came out."""
    short = Step(1, "USD900.00", [("K", ECONOMY), ("S", ECONOMY)], [BASIC, BASIC], codes=["KX", "SY"])
    filters = (without("UA.SFO+TYO.KX"), without("UA.TYO+SFO.SY"))
    first = climb(action_runner, Ladder({None: short}), until="not-basic", searches=1)
    printed = re.search(r"--skip (\S+)", first)[1]
    assert printed == "1:UA.SFO+TYO.KX,2:UA.TYO+SFO.SY"
    backend = Ladder({filters: L_W})
    climb(action_runner, backend, until="not-basic", searches=1, skip=printed)
    assert backend.filters == [filters]


def test_a_fare_the_filter_cant_name_alone_is_called_out_before_any_other_is_read(action_runner):
    failing = Step(1, "USD900.00", [("J", BUSINESS), ("J", BUSINESS)], [BASIC, STANDARD], codes=["CXU0O85J/PAWD", "OK"])
    dearer = Step(3, "USD1200.00", [("C", BUSINESS), ("J", BUSINESS)], [STANDARD, STANDARD], codes=["COK", "OK"])
    wide = ("a cheaper rung can exist: the filter can't name 1:UA.SFO+TYO.CXU0O85J/PAWD alone (over 10 characters), "
            "so the code's other fares on the slice went out unread")
    narrowed = ("f ~(UA.SFO+TYO.CXU0O85J/-)", None)
    assert wide in climb(action_runner, Ladder({None: failing, narrowed: dearer}), cabin="business", until="not-basic")
    # and a later call, handed the fare by --skip, says it again
    again = climb(action_runner, Ladder({narrowed: dearer}), cabin="business", until="not-basic",
                  skip="1:UA.SFO+TYO.CXU0O85J/PAWD")
    assert "rung 1 has no basic fare by its rule titles" in again and wide in again


def test_every_fare_the_filter_cant_name_alone_is_called_out(action_runner):
    first = Step(1, "USD900.00", [("J", BUSINESS), ("J", BUSINESS)], [BASIC, STANDARD], codes=["CXU0O85J/PAWD", "OK"])
    second = Step(2, "USD950.00", [("J", BUSINESS), ("J", BUSINESS)], [BASIC, STANDARD], codes=["ABCDEFGH/LONG", "OK"])
    sibling = Step(3, "USD990.00", [("J", BUSINESS), ("J", BUSINESS)], [STANDARD, STANDARD], codes=["CXU0O85J/OTHR", "OK"])
    dearer = Step(4, "USD1200.00", [("C", BUSINESS), ("J", BUSINESS)], [STANDARD, STANDARD], codes=["COK", "OK"])
    backend = Ladder({None: [first, second, sibling],
                      ("f ~(UA.SFO+TYO.CXU0O85J/-|UA.SFO+TYO.ABCDEFGH/-)", None): dearer})
    out = climb(action_runner, backend, cabin="business", until="not-basic")
    assert "the filter can't name 1:UA.SFO+TYO.CXU0O85J/PAWD, 1:UA.SFO+TYO.ABCDEFGH/LONG alone" in out
    assert "UA.SFO+TYO.CXU0O85J/OTHR that meets the condition among them" in out


def test_a_designated_fare_that_comes_back_is_reported(action_runner):
    designated = Step(1, "USD900.00", [("J", BUSINESS), ("J", BUSINESS)], [BASIC, STANDARD], codes=["JX2WC/CH", "OK"])
    backend = Ladder({None: designated, ("f ~(UA.SFO+TYO.JX2WC/CH)", None): designated})
    out = climb(action_runner, backend, cabin="business", until="not-basic")
    assert "Matrix listed UA.SFO+TYO.JX2WC/CH where the filter left it out, so the filter didn't hold for it" in out
    assert "rung 1 has no basic fare" not in out


def test_nothing_above_the_skipped_fares(action_runner):
    out = climb(action_runner, Ladder({}), skip=K_S_LEFT_OUT)
    assert f"0 found: Matrix has nothing without {K_S_LEFT_OUT}" in out
    assert "widen the search" not in out


def test_an_empty_first_search_is_an_empty_answer(action_runner):
    assert "0 found: Matrix found no itineraries; widen the search" in climb(action_runner, Ladder({}))


@pytest.mark.parametrize("kwargs, message", [
    ({"searches": 7}, "--searches 7: run 1 to 6 searches a call"),
    ({"skip": "K"}, "--skip 'K': give fares as the ladder prints them (1:UA.SFO+TYO.KLX07JBH)"),
    ({"skip": "KS"}, "--skip 'KS': give fares as the ladder prints them (1:UA.SFO+TYO.KLX07JBH)"),
    ({"skip": "3:UA.SFO+TYO.KLX0"}, "this trip has 2 slices"),
    ({"dest": None}, "ladder needs --dest"),
    ({"until": "  "}, "--until needs not-basic, refundable, or the words to look for"),
])
def test_ladder_usage_errors(action_runner, kwargs, message):
    with pytest.raises(ArgumentError) as e:
        climb(action_runner, Ladder({}), **kwargs)
    assert message in str(e.value)


def test_a_rung_quotes_each_fares_first_penalty_statements(action_runner):
    out = climb(action_runner, Ladder({None: Step(1, "USD100.00", [("K", ECONOMY), ("S", ECONOMY)], [STANDARD, FLEXIBLE])}),
                searches=1)
    assert ',"cancel: any time: TICKET IS NON-REFUNDABLE. | cancel: before departure: CANCELLATIONS PERMITTED.",1,S1.SET1.' in out
    same = climb(action_runner, Ladder({None: K_S}), searches=1)
    assert ',"cancel: any time: TICKET IS NON-REFUNDABLE.",1,S1.SET1.' in same


def test_penalty_sections_keep_the_statements_and_drop_the_notes():
    def filed(*lines: str) -> dict[str, str]:
        return penalty_sections(chr(10).join(lines))
    assert filed(
        "     NOTE -",
        "      TICKET IS NON-REFUNDABLE/CHANGES ARE NOT PERMITTED",
        "  CHANGES",
        "    BEFORE DEPARTURE",
        "      PER TICKET CHARGE USD 299.00 FOR CANCEL/REISSUE.",
        "         NOTE -",
        "          ANY MODIFICATION TO THE ITINERARY IS CONSIDERED AS A",
        "          CANCEL.",
        "    AFTER DEPARTURE",
        "      CHANGES NOT PERMITTED.",
        "  CANCELLATIONS",
        "    TICKET IS NON-REFUNDABLE IN CASE OF CANCEL.",
        "    CHANGES PERMITTED FOR REISSUE/REVALIDATION/UNTICKETED",
        "      PTA.",
    ) == {"change": "before departure: PER TICKET CHARGE USD 299.00 FOR CANCEL/REISSUE. after departure: "
                    "CHANGES NOT PERMITTED.",
          "cancel": "TICKET IS NON-REFUNDABLE IN CASE OF CANCEL. CHANGES PERMITTED FOR REISSUE/REVALIDATION/"
                    "UNTICKETED PTA."}
    assert filed("CANCELLATIONS", "  BEFORE DEPARTURE", "    CANCELLATIONS PERMITTED.", "  AFTER DEPARTURE",
                 "       NOTE -", "        REFUND THE DIFFERENCE.") == {
        "cancel": "before departure: CANCELLATIONS PERMITTED. after departure: in the notes."}
    assert filed("CANCELLATIONS", "  BEFORE DEPARTURE", "    CHARGE USD 100.00.", "  AFTER DEPARTURE",
                 "    CHARGE USD 100.00.") == {
        "cancel": "before departure: CHARGE USD 100.00. after departure: CHARGE USD 100.00."}
    assert filed("CANCELLATIONS", "    TICKET IS NON-REFUNDABLE.") == {"cancel": "TICKET IS NON-REFUNDABLE."}
    assert filed("") == {}


def _fare(name: str, kind: str = "", *, market: str = "SFO-TYO", cancel: str = "", change: str = "",
          above: bool = False) -> Fare:
    return Fare(name=name, basis=name, market=market, kind=kind, cancel=cancel, change=change, above=above)


def test_a_fare_quotes_its_statements_cancel_first_and_cut():
    quoted = _fare("K", cancel="CHARGE USD 1.00. " * 20, change="any time: CHANGES PERMITTED.").quote()
    assert quoted.startswith("cancel: CHARGE USD 1.00.") and "… · change: any time: CHANGES PERMITTED." in quoted
    assert _fare("K").quote() == ""


def test_short_notes():
    assert short_note("Changes to this ticket will incur a penalty fee.") == "change fee"
    assert short_note("No changes may be made to this ticket after departure.") == "no changes after departure"
    assert short_note("Cancellation of this ticket will incur a penalty fee.") == "cancel fee"


def test_a_skip_names_a_fare_on_a_slice_and_its_filter_term():
    plain = Skip.of("2:UA.TYO+SFO.SLW47FBH")
    assert plain == Skip(2, "UA", "TYO+SFO", "SLW47FBH")
    assert plain.term == "(UA.TYO+SFO.SLW47FBH&~UA.TYO+SFO.SLW47FBH/-)" and plain.exact
    assert Skip.of("JX2WC/CH").term == "..JX2WC/CH" and Skip.of("UA.JX2WC").term == "(UA..JX2WC&~UA..JX2WC/-)"
    assert Skip.of("UA.CXU0O85J/PAWD").term == "UA..CXU0O85J/-"
    assert Skip.of("UA.ABCDEFGHI").term == "UA..ABCDEFGHI" and not Skip.of("UA.ABCDEFGHI").exact
    assert Skip.of("12:UA.SFO+TYO.KX") == Skip(12, "UA", "SFO+TYO", "KX")
    assert Skip.of("k") is None and Skip.of("UA.SFO-TYO.K1") is None


def test_a_skip_covers_the_fares_its_term_leaves_out():
    def fare(name: str, *slices: int) -> Fare:
        return Fare(name=name, basis=name.split(".")[-1], market="", kind="", cancel="", change="", above=False,
                    slices=slices)
    there = Skip.of("1:UA.SFO+TYO.KLX07JBH")
    assert there.covers(fare("UA.TYO+SFO.KLX07JBH", 1))          # the pair either way round
    assert not there.covers(fare("UA.SFO+TYO.KLX07JBH", 2))      # another slice
    assert not there.covers(fare("UA.SFO+TYO.KLX07JBH/CH", 1))   # a designator
    assert there.covers(fare("UA.SFO+TYO.KLX07JBH")) and not there.covers(fare("UA.SFO+TYO.KLX07JBH"), strict=True)
    long = Skip.of("UA.SFO+TYO.CXU0O85J/PAWD")
    assert long.covers(fare("UA.SFO+TYO.CXU0O85J/OTHR", 1)) and not long.covers(fare("UA.SFO+TYO.CXU0O85J", 1))
    assert Skip.of("UA.ABCDEFGHI").covers(fare("UA.SFO+TYO.ABCDEFGHI/X", 1))


def test_rung_marks_a_slice_flown_wholly_in_a_higher_cabin():
    def flight(code: str, cabin: str, leg: tuple[str, str]) -> dict:
        return {"origin": {"code": leg[0]}, "destination": {"code": leg[1]},
                "bookingInfos": [{"bookingCode": code, "cabin": cabin}]}
    add_on = rung({"slices": [{"segments": [flight("Z", FIRST, ("BOS", "EWR")), flight("J", BUSINESS, ("EWR", "HND"))]}],
                   "fares": [{"key": "0/0", "basis": "VAA7", "carrier": "UA", "flights": [("BOS", "EWR")]},
                             {"key": "0/1", "basis": "JX2WC/CH", "carrier": "UA", "flights": [("EWR", "HND")]}],
                   "notes": [NON_REF]}, "business", {"0/1": ("sell-up fare: x", "")})
    assert (add_on.classes, add_on.kind, add_on.notes, add_on.higher) == ("Z J", "sell-up", "non-refundable", {})
    assert [(f.skips, f.basis, f.above) for f in add_on.fares] == [(("1:UA.VAA7",), "VAA7", False),
                                                                   (("1:UA.JX2WC/CH",), "JX2WC/CH", False)]
    # premium out and economy back in the same class letter, on two carriers: only the outbound fare is above
    upgrade = rung({"slices": [{"segments": [flight("R", PREMIUM, ("SFO", "NRT"))]},
                               {"segments": [flight("R", ECONOMY, ("NRT", "SFO"))]}],
                    "fares": [{"key": "0/0", "basis": "RLX", "carrier": "UA", "flights": [("SFO", "NRT")]},
                              {"key": "0/1", "basis": "RLX", "carrier": "NH", "flights": [("NRT", "SFO")]}],
                    "notes": []}, "economy", {"0/1": ("unbundled (basic) fare: x", "")})
    assert (upgrade.classes, upgrade.kind, upgrade.skips) == ("R | R", "basic, R is premium", ("1:UA.RLX", "2:NH.RLX"))
    assert [f.above for f in upgrade.fares] == [True, False]
    assert condition("not-basic").leaves_out(upgrade) == ["1:UA.RLX"] and not condition("not-basic").meets(upgrade)


def _rung_of(*fares: tuple[str, str], higher: bool = False) -> Rung:
    """A rung of fares given as (name, kind)."""
    return Rung(classes="", notes="", higher={"R": "premium"} if higher else {},
                fares=[_fare(name, kind, above=higher) for name, kind in fares])


def _row(price: str) -> dict:
    return {"price": price, "itinerary": "", "id": 1, "search": "S"}


def test_climb_leaves_out_the_failing_fares_and_keeps_the_ones_that_pass():
    climb = Climb(condition("not-basic"), [])
    assert climb.take([(_row("USD100.00"), _rung_of(("K", ""), ("S", "basic")))])
    assert climb.left_out == ["S"] and climb.answer is None
    assert not climb.take([(_row("USD150.00"), _rung_of(("K", ""), ("B", "")))])
    assert climb.answer.row["price"] == "USD150.00"
    assert [(s.number, s.row["price"]) for s in climb.rungs()] == [(1, "USD100.00"), (2, "USD150.00")]


def test_climb_holds_a_rung_that_fits_down_a_search_until_one_puts_it_first():
    climb = Climb(condition("not-basic"), [])
    good, bad = _rung_of(("B", "")), _rung_of(("K", "basic"))
    assert climb.take([(_row("USD100.00"), bad), (_row("USD200.00"), good)])
    assert climb.answer is None and climb.candidate.row["price"] == "USD200.00"
    assert not climb.take([(_row("USD150.00"), good)])      # the same fares, first and cheaper
    assert climb.answer.row["price"] == "USD150.00" and climb.candidate is None


def test_climb_without_a_condition_leaves_out_every_fare_and_never_answers():
    climb = Climb(condition(None), [])
    assert climb.take([(_row("USD100.00"), _rung_of(("K", ""), ("S", "")))])
    assert climb.left_out == ["K", "S"] and climb.answer is None and climb.candidate is None


def test_climb_stops_when_a_search_names_no_new_fare():
    climb = Climb(condition("not-basic"), ["K"])
    assert not climb.take([(_row("USD100.00"), _rung_of(("K", "basic")))])
    assert climb.answer is None


def test_a_rung_in_a_higher_cabin_never_fits():
    goal = condition("not-basic")
    assert not goal.meets(_rung_of(("R", ""), higher=True))
    assert goal.leaves_out(_rung_of(("R", ""), higher=True)) == ["R"]


def test_two_fares_with_one_name_are_two_rungs_and_judged_apart():
    """The filter can't tell them apart; the ladder still judges each where it is read."""
    climb = Climb(condition("not-basic"), [])
    basic = Rung("", [_fare("UA.SAME", "basic", market="SFO-TYO")], "", {})
    standard = Rung("", [_fare("UA.SAME", "", market="TYO-SFO")], "", {})
    climb.take([(_row("USD100.00"), basic), (_row("USD150.00"), standard)])
    assert climb.candidate.row["price"] == "USD150.00" and len(climb.steps) == 2
    assert not climb.take([(_row("USD150.00"), standard)])
    assert climb.answer.row["price"] == "USD150.00"


def test_a_candidate_stays_when_a_cheaper_rung_of_the_same_fares_fails_for_its_cabin():
    climb = Climb(condition("not-basic"), [])
    climb.take([(_row("USD100.00"), _rung_of(("K", "basic"))), (_row("USD200.00"), _rung_of(("B", "")))])
    climb.take([(_row("USD150.00"), _rung_of(("B", ""), higher=True))])
    assert climb.candidate.row["price"] == "USD200.00" and climb.answer is None


def test_the_same_fares_in_another_order_are_one_rung_at_the_cheaper_itinerary():
    climb = Climb(condition(None), [])
    climb.take([(_row("USD150.00"), _rung_of(("A", ""), ("B", "")))])
    climb.take([(_row("USD100.00"), _rung_of(("B", ""), ("A", "")))])
    assert [s.row["price"] for s in climb.rungs()] == ["USD100.00"]


def test_refundable_needs_a_cancellation_statement_that_does_not_refuse():
    refundable = condition("refundable")

    def meets(cancel: str) -> bool:
        return refundable.meets(Rung("", [_fare("K", cancel=cancel)], "", {}))
    assert meets("before departure: CANCELLATIONS PERMITTED. after departure: in the notes.")
    assert not meets("any time: in the notes.")         # its terms are all notes
    assert not meets("")
    assert not meets("any time: TICKET IS NON REFUNDABLE.")
    assert not meets("any time: TICKET IS NON- REFUNDABLE.")     # wrapped onto a second line
    assert not meets("FARE IS NONREFUNDABLE.")
    assert condition("REFUNDABLE") is refundable


def test_a_higher_cabin_rung_leaves_out_the_fares_that_price_its_flights():
    def flight(code: str, cabin: str, leg: tuple[str, str]) -> dict:
        return {"origin": {"code": leg[0]}, "destination": {"code": leg[1]},
                "bookingInfos": [{"bookingCode": code, "cabin": cabin}]}
    through = rung({"slices": [{"segments": [flight("R", PREMIUM, ("A", "B"))]},
                               {"segments": [flight("K", ECONOMY, ("B", "C")), flight("K", ECONOMY, ("C", "D"))]}],
                    "fares": [{"key": "0/0", "basis": "THRU", "carrier": "UA", "flights": [("A", "B"), ("B", "C")]},
                              {"key": "0/1", "basis": "GOOD", "carrier": "UA", "flights": [("C", "D")]}],
                    "notes": []}, "economy")
    assert condition("not-basic").leaves_out(through) == ["1:UA.THRU", "2:UA.THRU"]
    unmapped = rung({"slices": [{"segments": [flight("R", PREMIUM, ("A", "B"))]}],
                     "fares": [{"key": "0/0", "basis": "X", "carrier": "UA"}], "notes": []}, "economy")
    assert condition("not-basic").leaves_out(unmapped) == ["UA.X"]


@needs_site("gflights")
def test_the_rung_that_fits_points_to_what_the_airline_sells_on_its_flights(action_runner, monkeypatch):
    import sys
    plain = _detail

    def dated(step: Step) -> dict:
        """As Matrix answers: each flight's departure, and who the pricing is for."""
        data = plain(step)
        details = data["bookingDetails"]
        for s, day in zip(details["itinerary"]["slices"], ("2027-03-10T10:45-08:00", "2027-03-20T17:00+09:00")):
            for seg in s["segments"]:
                seg["departure"] = day
        details["tickets"][0]["pricings"][0]["ext"] = {"pax": {"adults": 1}}
        return data

    monkeypatch.setattr(sys.modules[__name__], "_detail", dated)
    steps = {None: K_S, WITHOUT_K_S: Step(2, "USD2702.09", [("B", ECONOMY), ("B", ECONOMY)], [STANDARD, STANDARD])}
    out = climb(action_runner, Ladder(steps), searches=5, until="not-basic")
    assert ("esum gflights fares --flights A00-B00:2027-03-10:UA837 A10-B10:2027-03-20:UA837` to see what the "
            "airline sells on rung 2's flights now") in out
    assert "esum gflights" not in climb(action_runner, Ladder(steps), searches=2)     # no rung to point at
