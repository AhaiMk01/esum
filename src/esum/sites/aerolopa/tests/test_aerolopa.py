"""AeroLOPA (D-92): fleet pages, aircraft-type pages, a seat map page, Super Schedule route pages and
an airline's routes for one type, all read from the server-components payload the pages embed.

Fixtures are real pages captured logged out on 2026-11-13, trimmed to their <title>, the payload
rows the parsers read (plus one text row, to exercise the length-prefixed row reader) and, for the
two index pages, the HTML of their link lists. Per-seat data is emptied except in
seatmap_ba-781.html, which keeps 11 seats across its four cabins (the site shows their ratings and
notes only after sign-in; the page data carries them for everyone, D-92). Nothing in them belongs
to an account. Expected values are what the pages showed at capture time."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from esum import Options
from esum.core.registry import Registry
from esum.sites.aerolopa.parse import (PageChanged, airlines, days_label, fleet, match_type, routes, rsc_objects,
                                       schedule, seatmap, seats, type_fleet, type_slugs)
from esum.transport import Response

FIX = Path(__file__).parent / "fixtures"
BASE = "https://www.aerolopa.com"


def text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def page(path: str, name: str) -> Response:
    return Response(200, BASE + path, {"content-type": "text/html; charset=utf-8"}, text(name).encode())


@pytest.fixture
def aerolopa():
    return Registry.discover().get("aerolopa")


def run(action_runner, target, name, kwargs, json_out=False):
    output = io.StringIO()
    action_runner(target, target.all_actions[name], kwargs, Options(json=json_out), stream=output)
    return output.getvalue()


# --- parsers -----------------------------------------------------------------------------------

def test_payload_reader_skips_length_prefixed_text_rows():
    # The fixture's first row is a `T` text row with no newline after it; the object row after it
    # must still decode.
    html = text("seatmap_ua-77m.html")
    assert ':T' in html
    assert any(isinstance(o, list) for o in rsc_objects(html))
    assert seatmap(html)["code"] == "ua-77m"


def test_airlines_index():
    rows = airlines(text("airlines.html"))
    by_code = {r["code"]: r["name"] for r in rows}
    assert by_code["ua"] == "United Airlines"
    assert by_code["ei"] == "Aer Lingus"
    assert by_code["ar"] == "Aerolíneas Argentinas"
    assert len(rows) > 200


def test_type_index():
    slugs = type_slugs(text("aircraft.html"))
    assert {"boeing-787-9", "airbus-a350-900", "airbus-a350-1000", "boeing-777-200"} <= set(slugs)


def test_airline_fleet():
    name, rows = fleet(text("fleet_ua.html"))
    assert name == "United Airlines"
    assert len(rows) == 38
    assert len({r["code"] for r in rows}) == 38
    row = next(r for r in rows if r["code"] == "ua-77m")
    # the fleet tile: "77M Fleet size 10 28 336 Boeing 777-200"
    assert (row["type"], row["F"], row["J"], row["W"], row["Y"], row["seats"], row["fleet"]) == \
        ("Boeing 777-200", 0, 28, 0, 336, 364, 10)
    polaris = next(r for r in rows if r["code"] == "ua-76l")
    assert (polaris["variant"], polaris["J"], polaris["W"], polaris["Y"], polaris["fleet"]) == ("Polaris", 46, 22, 99, 24)
    assert polaris["airline"] == "United Airlines"


def test_type_across_airlines():
    name, rows = type_fleet(text("type_boeing-787-9.html"))
    assert name == "Boeing 787-9"
    row = next(r for r in rows if r["code"] == "ac-789")
    # the comparison table: "Air Canada · 298 · 30 21 247 · Better · 32 · 2026-01-28"
    assert (row["airline"], row["seats"], row["J"], row["W"], row["Y"], row["wifi"], row["fleet"], row["updated"]) == \
        ("Air Canada", 298, 30, 21, 247, "Better", 32, "2026-01-28")
    assert len({r["airline"] for r in rows}) == 53     # "Compare the Boeing 787-9 across 53 airlines"


def test_seatmap():
    m = seatmap(text("seatmap_ua-77m.html"))
    assert m["title"] == "United Airlines Boeing 777-200 (77M)"
    assert (m["airline"], m["airline_code"], m["type"], m["type_slug"]) == \
        ("United Airlines", "ua", "Boeing 777-200", "boeing-777-200")
    assert m["cabins"] == [{"cabin": "J", "name": "Business seats", "seats": 28, "rows": "1 to 4"},
                           {"cabin": "Y", "name": "Economy seats", "seats": 336, "rows": "16 to 53"}]
    assert m["wifi"] == [{"system": "Panasonic Ku-band", "rating": "Better", "speed": "10-40 Mbps"}]
    assert m["description"].startswith("United Airlines Boeing 777-200 (type 77M) is configured 28J 336M")
    top = {r["route"]: r for r in m["top_routes"]}
    assert top["LAX-HNL"]["share"] == 41 and top["LAX-HNL"]["schedule"] == "LAX-HNL"
    assert top["SFO-IAH"]["schedule"] == "SFO-HOU"       # the page links the Houston city schedule
    assert top["IAD-LAX"]["schedule"] is None            # shown without a link
    assert m["image"].startswith("https://d9k46j35fmriq.cloudfront.net/maptool/seatmaps/ua-77m.webp")
    # the page's seat legend: 83 good, 45 be aware, 32 avoid of 364 seats
    assert m["ratings"] == {"good": 83, "aware": 45, "avoid": 32, "unrated": 204}
    assert m["aisle_access"] == {"business": 50, "economy": 41}
    assert m["products"] == ['Economy Plus (economy, rows 16-26, 39-40, 102 seats): pitch 35" (standard 31"), '
                             'recline 4" (standard 3")']
    assert "recommend" not in json.dumps(m)              # no recommendation lists in the data


def test_seats_ratings_and_notes():
    info, rows = seats(text("seatmap_ba-781.html"))
    assert info == {"code": "ba-781", "title": "British Airways Boeing 787-10 (781) (2020 cabin iteration)"}
    by = {r["seat"]: r for r in rows}
    assert [r["seat"] for r in rows] == ["1A", "2A", "5A", "7A", "16A", "20A", "23K", "24A", "30A", "41A", "45A"]
    assert {k: by["1A"][k] for k in ("row", "cabin", "rating", "window", "lieflat")} == \
        {"row": 1, "cabin": "first", "rating": "good", "window": "average (1)", "lieflat": True}
    # rated "be aware" with only good notes: the window view is poor
    assert (by["5A"]["rating"], by["5A"]["window"]) == ("aware", "poor (2)")
    assert by["24A"]["rating"] == "avoid" and by["24A"]["cabin"] == "premium-economy"
    assert by["24A"]["notes"].startswith("- Immediately adjacent to lavatory (behind). Expect noise, odors, and "
                                         "queuing traffic (major)")
    s30 = by["30A"]
    assert (s30["rating"], s30["window"], s30["_good"], s30["_bad"]) == ("avoid", "none (1)", 2, 9)
    assert "- Exit door L3 extends into about 75% of your seat width, 20cm (8in) ahead (moderate)" in s30["notes"]
    # bad notes first, worst severity first, so a cut cell keeps the reasons (the page lists them mixed)
    marks = [n[:1] for n in s30["notes"].split(" · ")]
    assert marks == ["-"] * 5 + ["+"] * 2
    assert s30["notes"].split(" · ")[4] == "- Tray table in armrest (no seatback ahead) (minor)"
    assert by["23K"]["rating"] == ""                     # unrated, but still has a note
    assert by["41A"]["notes"] == "- Plugged or missing windows nearby (moderate)"


def test_schedule_business_route():
    s = schedule(text("schedule_LON-NYC.html"))
    assert (s["cabin"], s["class"], s["date"], s["places"]) == ("business", "business class", "", "London to New York")
    assert len(s["rows"]) == 30
    first = s["rows"][0]
    assert {k: first[k] for k in ("flight", "code", "tier", "seat", "layout", "direct", "wifi", "time", "days",
                                  "season")} == \
        {"flight": "BA 115", "code": "ba-781", "tier": "Top", "seat": "Private suite", "layout": "1-2-1",
         "direct": "100%", "wifi": "10-40 Mbps", "time": "14:15 – 17:20", "days": "Daily",
         "season": "Nov 12 to Mar 12"}
    assert "Seat width" in first["_gated"] and "Bed length" in first["_gated"]
    assert [r["tier"] for r in s["rows"]][-1] == "Bottom"


def test_schedule_dated_economy():
    s = schedule(text("schedule_MUC-SFO_economy_2026-12-01.html"))
    assert (s["cabin"], s["date"]) == ("economy", "Tue, Dec 1")
    lh = s["rows"][0]
    assert (lh["flight"], lh["code"], lh["layout"], lh["seat"], lh["config"]) == \
        ("LH 458", "lh-388-2", "3-4-3", "Economy seat", "371 Economy seats")
    assert "Row pitch" in lh["_gated"]


def test_routes_layout_shares():
    r = routes(text("routes_ua_boeing-777-200.html"))
    assert (r["carrier"], r["from"], r["to"]) == ("UA", "2026-11-12", "2026-12-11")
    assert r["layouts"] == ["ua-77e", "ua-77g", "ua-77m", "ua-77o"]
    ua384 = next(x for x in r["rows"] if x["flight"] == "UA 384")
    # the page: "Boeing 777-200 · 15 of 29 dates (52%)", "13 of 29 (45%)", "777-200ER · 1 of 29 (3%)"
    assert ua384["layouts"] == "ua-77m 52% · ua-77g 45% · ua-77o 3%"
    assert ua384["seen"] == "ua-77m 15 of 29 · ua-77g 13 of 29 · ua-77o 1 of 29"
    assert (ua384["route"], ua384["departs"], ua384["arrives"], ua384["days"], ua384["flying"], ua384["code"]) == \
        ("DEN-HNL", "12:05", "16:28", "Daily", "30 of 30", "ua-77m")


def test_routes_other_types_and_days():
    r = routes(text("routes_kq_boeing-787-8.html"))
    kq761 = next(x for x in r["rows"] if x["flight"] == "KQ 761")
    # "KQ 761 · Boeing 737-800 scheduled 14 Nov–11 Dec · Embraer E190 17 Nov–26 Nov · Boeing 777-300ER on 8 Dec"
    # dogfood round 3: a date range read as every day; the number of flights says how many
    assert kq761["also"] == ("Boeing 737-800 (2026-11-14 to 2026-12-11, 17 flights); "
                             "Embraer E190 (2026-11-17 to 2026-11-26, 2 flights); Boeing 777-300ER (2026-12-08)")
    # dogfood round 3: the type that flew the unfilled share went into `also` (and doubled a
    # scheduled type there); the page names it on hover of the share bar
    kq763 = next(x for x in r["rows"] if x["flight"] == "KQ 763")
    assert kq763["layouts"] == "kq-788 94% · Boeing 777-300ER (other type) 6%"
    assert kq763["seen"] == "kq-788 15 of 16 · Boeing 777-300ER (other type) 1 of 16"
    assert kq763["also"] == "" and kq763["code"] == "kq-788"
    assert (kq761["departs"], kq761["arrives"], kq761["days"], kq761["flying"]) == ("12:05", "17:10", "Tu Fr Su", "8 of 30")
    kq117 = next(x for x in r["rows"] if x["flight"] == "KQ 117")
    assert kq117["arrives"] == "06:10 +1"
    # dogfood round 2: a flight not yet seen with the type printed an empty layouts cell; the page
    # says "Not yet seen as a Boeing 787-8 — scheduled ahead"
    kq887 = next(x for x in r["rows"] if x["flight"] == "KQ 887")
    assert (kq887["layouts"], kq887["code"]) == ("not seen yet", "")
    assert kq887["seen"] == "2 sighted dates"            # re-check: 0 or 4 sightings read the same


def test_type_names_resolve_including_sub_variants():
    slugs = type_slugs(text("aircraft.html"))
    assert match_type("787-9", slugs) == ["boeing-787-9"]
    # dogfood round 1: `--type 777-200ER` was rejected; AeroLOPA files it under boeing-777-200
    assert match_type("777-200er", slugs) == ["boeing-777-200"]
    assert match_type("boeing-777-200er", slugs) == ["boeing-777-200"]
    assert match_type("777-300er", slugs) == ["boeing-777-300er"]
    assert match_type("flub", slugs) == []


def test_days_label():
    assert days_label(range(1, 8)) == "Daily"
    assert days_label([7, 2, 5]) == "Tu Fr Su"


def test_layout_change_is_reported():
    with pytest.raises(PageChanged):
        seatmap("<html><title>x</title></html>")
    with pytest.raises(PageChanged):
        airlines("<html></html>")


# --- whole actions -----------------------------------------------------------------------------

def test_fleet_by_airline_name(action_runner, fake_http, aerolopa):
    fake_http.routes[f"{BASE}/airlines"] = page("/airlines", "airlines.html")
    fake_http.routes[f"{BASE}/ua"] = page("/ua", "fleet_ua.html")
    out = run(action_runner, aerolopa, "fleet", {"airline": "United Airlines", "type": "777-200"})
    assert "airline: United Airlines (ua)" in out
    assert "ua-77m,Boeing 777-200" in out and "ua-78p" not in out
    assert "seatmap --code <code>" in out


def test_fleet_filter_empty_offers_whole_fleet(action_runner, fake_http, aerolopa):
    fake_http.routes[f"{BASE}/ua"] = page("/ua", "fleet_ua.html")
    out = run(action_runner, aerolopa, "fleet", {"airline": "ua", "type": "a380"})
    assert "0 found" in out
    assert "esum aerolopa fleet --airline ua" in out


def test_fleet_by_type_resolves_short_name(action_runner, fake_http, aerolopa):
    fake_http.routes[f"{BASE}/aircraft"] = page("/aircraft", "aircraft.html")
    fake_http.routes[f"{BASE}/aircraft/boeing-787-9"] = page("/aircraft/boeing-787-9", "type_boeing-787-9.html")
    out = run(action_runner, aerolopa, "fleet", {"type": "787-9", "limit": 3})
    assert "type: Boeing 787-9 (boeing-787-9)" in out
    assert "ac-789,Air Canada" in out
    assert "--offset 3" in out


def test_ambiguous_type_is_a_usage_error(action_runner, fake_http, aerolopa, capsys):
    fake_http.routes[f"{BASE}/aircraft"] = page("/aircraft", "aircraft.html")
    code = action_runner.cli(["aerolopa", "fleet", "--type", "a350"], registry=Registry.discover())
    out = capsys.readouterr().out
    assert code == 2
    assert "airbus-a350-900" in out and "airbus-a350-1000" in out and "help:" in out


def test_seatmap_action_and_missing_code(action_runner, fake_http, aerolopa, capsys):
    fake_http.routes[f"{BASE}/ua-77m"] = page("/ua-77m", "seatmap_ua-77m.html")
    out = run(action_runner, aerolopa, "seatmap", {"code": "https://www.aerolopa.com/ua-77m"})
    assert "J Business seats: 28, rows 1 to 4" in out
    assert "SFO-IAH San Francisco to Houston: 6% (schedule --route SFO-HOU)" in out
    assert "IAD-LAX Washington to Los Angeles: 9% (no Super Schedule)" in out
    assert "seat ratings: 83 good, 45 be aware, 32 avoid, 204 unrated" in out
    assert "direct aisle access: business 50%, economy 41%" in out
    assert "seat product: Economy Plus" in out
    assert "not in the data: seat width, bed length" in out
    assert "esum aerolopa seats --code ua-77m --rating good --sort best" in out
    assert "esum aerolopa seats --code ua-77m --rating avoid --sort worst" in out
    assert "esum aerolopa schedule --route LAX-HNL" in out
    assert "esum aerolopa routes --airline ua --type boeing-777-200" in out
    code = action_runner.cli(["aerolopa", "seatmap", "--code", "ua-zzz"], registry=Registry.discover())
    assert code == 1
    assert "help: List the airline's codes" in capsys.readouterr().out


def test_seats_best_and_worst(action_runner, fake_http, aerolopa):
    fake_http.routes[f"{BASE}/ba-781"] = page("/ba-781", "seatmap_ba-781.html")
    out = run(action_runner, aerolopa, "seats", {"code": "ba-781", "sort": "worst"})
    assert "seats[10]{seat,cabin,rating,window,notes}:" in out
    assert "\n  30A,economy,avoid,none (1),\"- No usable window view" in out
    assert "23K" not in out                              # unrated seats are left out by default
    worst = json.loads(run(action_runner, aerolopa, "seats", {"code": "ba-781", "sort": "worst"}, json_out=True))
    # avoid first (most bad notes first), then be aware, then good; equal notes: the worse view first
    assert [r["seat"] for r in worst["data"]["seats"]] == ["30A", "24A", "20A", "45A", "16A", "41A", "5A", "1A", "2A", "7A"]
    best = run(action_runner, aerolopa, "seats", {"code": "ba-781", "sort": "best", "rating": "good"}, json_out=True)
    assert [r["seat"] for r in json.loads(best)["data"]["seats"]] == ["1A", "2A", "7A"]
    biz = run(action_runner, aerolopa, "seats", {"code": "ba-781", "cabin": "business", "note": "bassinet"},
              json_out=True)
    assert [r["seat"] for r in json.loads(biz)["data"]["seats"]] == ["16A"]
    pe = run(action_runner, aerolopa, "seats", {"code": "ba-781", "cabin": "premium-economy", "rating": "all"},
             json_out=True)
    assert [r["seat"] for r in json.loads(pe)["data"]["seats"]] == ["20A", "23K", "24A"]
    assert json.loads(pe)["data"]["seats"][0]["cabin"] == "premium-economy"     # as the flag spells it
    # dogfood round 3: `--note window` also matched "Faces the windows"; filter by the view instead
    poor = run(action_runner, aerolopa, "seats", {"code": "ba-781", "window": "poor"}, json_out=True)
    assert [r["seat"] for r in json.loads(poor)["data"]["seats"]] == ["5A", "20A", "45A"]
    none = run(action_runner, aerolopa, "seats", {"code": "ba-781", "window": "none", "rating": "all"}, json_out=True)
    assert [r["seat"] for r in json.loads(none)["data"]["seats"]] == ["30A"]
    # dogfood round 3: offset past the end said "no seat matches"
    past = run(action_runner, aerolopa, "seats", {"code": "ba-781", "offset": 10})
    assert "no rows past offset 10; 10 in all" in past


def test_seats_cli_exits(action_runner, fake_http, aerolopa, capsys):
    fake_http.routes[f"{BASE}/ba-781"] = page("/ba-781", "seatmap_ba-781.html")
    reg = Registry.discover()
    assert action_runner.cli(["aerolopa", "seats", "--code", "ba-781", "--cabin", "first", "--rating", "avoid"],
                             registry=reg) == 0
    out = capsys.readouterr().out
    assert "no seat matches" in out and "esum aerolopa seats --code ba-781 --rating all" in out
    assert action_runner.cli(["aerolopa", "seats", "--code", "ba-zzz"], registry=reg) == 1
    assert "help: List the airline's codes" in capsys.readouterr().out
    assert action_runner.cli(["aerolopa", "seats", "--code", "ba-781", "--sort", "nicest"], registry=reg) == 2


def test_seatmap_without_type_slug_still_offers_routes_and_fleet(action_runner, fake_http, aerolopa):
    # dogfood round 1: ua-77e/ua-77o pages carry "typeSlug":null and printed no routes/fleet step
    html = text("seatmap_ua-77m.html").replace('\\"typeSlug\\":\\"boeing-777-200\\"', '\\"typeSlug\\":null')
    assert '\\"typeSlug\\":null' in html
    fake_http.routes[f"{BASE}/ua-77m"] = Response(200, f"{BASE}/ua-77m", {"content-type": "text/html"},
                                                  html.encode())
    out = run(action_runner, aerolopa, "seatmap", {"code": "ua-77m"})
    assert "esum aerolopa routes --airline ua --type 'Boeing 777-200'" in out
    assert "esum aerolopa fleet --type 'Boeing 777-200'" in out


def test_fleet_type_filter_takes_manufacturer_prefixes(action_runner, fake_http, aerolopa):
    # dogfood round 1: `--type B787-10` matched nothing in an airline's fleet
    fake_http.routes[f"{BASE}/ua"] = page("/ua", "fleet_ua.html")
    out = run(action_runner, aerolopa, "fleet", {"airline": "ua", "type": "B787-10"})
    assert "ua-78j,Boeing 787-10" in out


def test_schedule_action_paths_and_filters(action_runner, fake_http, aerolopa):
    url = f"{BASE}/super-schedule/MUC-SFO/economy/2026-12-01"
    fake_http.routes[url] = page(url.removeprefix(BASE), "schedule_MUC-SFO_economy_2026-12-01.html")
    out = run(action_runner, aerolopa, "schedule",
              {"route": "muc-sfo", "cabin": "economy", "date_": "2026-12-01", "flight": "ua195"})
    assert "UA 195,ua-77e,Middle,Economy seat,3-4-3" in out
    assert "LH 458" not in out
    assert 'date: 2026-12-01 (Tue, Dec 1)' in out or 'date: "2026-12-01 (Tue, Dec 1)"' in out
    assert "needs_sign_in:" in out and "Row pitch" in out
    assert "esum aerolopa seats --code ua-77e --cabin economy --sort best` to list the configuration's best economy" in out


def test_schedule_date_outside_window_is_usage_error(action_runner, fake_http, aerolopa, capsys):
    code = action_runner.cli(["aerolopa", "schedule", "--route", "MUC-SFO", "--date", "2026-01-01"],
                             registry=Registry.discover())
    out = capsys.readouterr().out
    assert code == 2
    assert "`esum aerolopa schedule --route MUC-SFO`" in out


def test_schedule_unknown_route_and_empty_filter(action_runner, fake_http, aerolopa, capsys):
    assert action_runner.cli(["aerolopa", "schedule", "--route", "XXX-YYY"], registry=Registry.discover()) == 1
    assert "help:" in capsys.readouterr().out
    fake_http.routes[f"{BASE}/super-schedule/LON-NYC"] = page("/super-schedule/LON-NYC", "schedule_LON-NYC.html")
    out = run(action_runner, aerolopa, "schedule", {"route": "LON-NYC", "flight": "XX9"})
    assert "0 found" in out
    assert "esum aerolopa schedule --route LON-NYC" in out


def test_routes_action(action_runner, fake_http, aerolopa):
    fake_http.routes[f"{BASE}/aircraft"] = page("/aircraft", "aircraft.html")
    url = f"{BASE}/routes/ua/boeing-777-200"
    fake_http.routes[url] = page("/routes/ua/boeing-777-200", "routes_ua_boeing-777-200.html")
    out = run(action_runner, aerolopa, "routes", {"airline": "ua", "type": "777-200", "flight": "384"})
    assert "UA 384,DEN-HNL" in out and "ua-77m 52% · ua-77g 45% · ua-77o 3%" in out
    assert "count: 1" in out or "flights[1]" in out
    assert "UA 384,DEN-HNL,\"12:05\",Daily,30 of 30,ua-77m 52% · ua-77g 45% · ua-77o 3%,ua-77m" in out  # code shown
    assert "last 30 days" in out
    # dogfood round 3: a flight filed under another type found nothing and only offered the same list
    miss = run(action_runner, aerolopa, "routes", {"airline": "ua", "type": "777-200", "flight": "UA1"})
    # re-check: pointing to `schedule` looped back to the filed type; list the airline's types instead
    assert "esum aerolopa fleet --airline ua" in miss and "the type that operates it" in miss
