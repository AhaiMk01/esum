"""Wings of the Points (D-104): the transfer chart's CSV and its README's notes and change log.

`transfers.csv` and `README.md` are the repository's files as they were on 2026-11-18, whole and
unchanged: public files with nothing of an account in them. They are their authors' work under
CC BY-NC-ND 4.0, not under this repository's MIT license (`fixtures/NOTICE.md`), which is why
neither is trimmed. Expected values were read off the rendered chart that day (its node labels,
alliance logos, link colours and the ratio tooltip of each link hovered) and off GitHub's rendering
of the README (30 dated entries, 63 bullets), not from this parser."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from esum import Options
from esum.core.registry import Registry
from esum.sites.wotp.parse import (PageChanged, change_patterns, match_airlines, miles_per_point, parse_changes,
                                   parse_chart, program_rows, reach, resolve_program, section, shared_programs,
                                   updated)
from esum.transport import Response

FIX = Path(__file__).parent / "fixtures"
RAW = "https://raw.githubusercontent.com/USCreditCardGuide/Wings-of-the-Points/gh-pages"
CSV = f"{RAW}/_data/transfers.csv"
README = f"{RAW}/README.md"


def text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.fixture
def chart():
    return parse_chart(text("transfers.csv"))


@pytest.fixture
def wotp():
    return Registry.discover().get("wotp")


@pytest.fixture
def raw(fake_http):
    for url, name in ((CSV, "transfers.csv"), (README, "README.md")):
        fake_http.routes[url] = Response(200, url, {"content-type": "text/plain; charset=utf-8"},
                                         text(name).encode())
    return fake_http


def run(action_runner, target, name, kwargs, json_out=False):
    output = io.StringIO()
    action_runner(target, target.all_actions[name], kwargs, Options(json=json_out), stream=output)
    return json.loads(output.getvalue())["data"] if json_out else output.getvalue()


def row(chart, code):
    return next(r for r in chart.rows if r["code"] == code)


def test_the_chart_has_the_pages_nodes_and_links(chart):
    # the page drew 63 airline nodes and 141 links: one per transfer, and one to N/A per program nothing reaches
    assert len(chart.rows) == 63
    transfers = sum(r["routes"] for r in chart.rows)
    orphans = [r["code"] for r in chart.rows if not r["routes"]]
    assert transfers + len(orphans) == 141
    assert chart.programs == ("amex", "citi", "chase", "c1", "bilt", "marriott")


def test_an_airline_row_reads_as_the_pages_tooltips(chart):
    # hovered: "AmEx MR 1:1.6 Aeroméxico (Aeromexico Rewards) [AM]", "Capital One 1:1 …", "Marriott Bonvoy 3:1.25 …"
    am = row(chart, "AM")
    assert am["name"] == "Aeroméxico (Aeromexico Rewards)" and am["alliance"] == "skyteam"
    assert {p: am[p] for p in chart.programs} == {"amex": "1:1.6", "citi": None, "chase": None, "c1": "1:1",
                                                  "bilt": None, "marriott": "3:1.25"}
    assert (am["routes"], am["best"]) == (3, "1:1.6 amex")
    ek = row(chart, "EK")
    assert (ek["amex"], ek["bilt"], ek["marriott"], ek["alliance"]) == ("1:0.8", "1:1", "3:1.25", "none")
    assert reach(ek, chart.programs) == {"amex", "citi", "c1", "bilt", "marriott"}
    assert row(chart, "UA")["chase"] == "1:1" and reach(row(chart, "UA"), chart.programs) == {"chase", "bilt", "marriott"}
    assert row(chart, "BR")["c1"] == "1:0.75" and row(chart, "BR")["citi"] == "1:1"
    assert reach(row(chart, "B6"), chart.programs) == {"amex", "citi", "chase", "c1", "bilt"}
    assert reach(row(chart, "AA"), chart.programs) == {"citi", "marriott"} and row(chart, "AA")["alliance"] == "oneworld"
    nh = row(chart, "NH")
    assert nh["name"] == "All Nippon Airways (ANA Mileage Club)" and nh["alliance"] == "star"
    assert reach(nh, chart.programs) == {"amex", "marriott"}


def test_a_program_nothing_reaches_has_no_ratio_and_no_best(chart):
    # Lufthansa and Korean Air each had one grey link, to the N/A node
    for code, alliance in (("LH", "star"), ("KE", "skyteam")):
        r = row(chart, code)
        assert reach(r, chart.programs) == set() and r["best"] is None and r["routes"] == 0
        assert r["alliance"] == alliance


def test_best_names_every_currency_at_the_top_ratio(chart):
    assert row(chart, "AF")["best"] == "1:1 amex citi chase c1 bilt"
    assert row(chart, "B6")["best"] == "1:1 citi chase bilt"         # amex 1:0.8 and c1 1:0.6 are worse
    assert row(chart, "CZ")["best"] == "3:1.25 marriott"
    assert miles_per_point("3:1.25") == pytest.approx(0.41667, abs=1e-4)
    assert miles_per_point("200:1.25") == 0.00625 and miles_per_point("") is None and miles_per_point("n/a") is None


def test_a_csv_without_its_columns_is_a_layout_failure():
    with pytest.raises(PageChanged):
        parse_chart("airline,program\nNH,ANA\n")
    with pytest.raises(PageChanged):
        parse_chart("code,name,alliance,link_order\nNH,ANA,starAlliance,1\n")
    with pytest.raises(PageChanged):
        parse_chart("code,name,alliance,link_order,amex\n")
    with pytest.raises(PageChanged):
        section("# Wings\n\n## Usage\n", "Change Log")


def test_a_currency_resolves_from_its_key_name_or_short_name(chart):
    names = {"UR": "chase", "chase": "chase", "Chase UR": "chase", "mr": "amex", "AmEx": "amex", "TYP": "citi",
             "capital one": "c1", "C1": "c1", "bonvoy": "marriott", "Marriott": "marriott", "bilt": "bilt"}
    assert {n: resolve_program(n, chart.programs) for n in names} == names
    assert resolve_program("wells fargo", chart.programs) is None


def test_airline_terms_match_codes_and_word_starts(chart):
    shared = shared_programs(text("README.md"))
    assert match_airlines(chart, ["nh"], shared) == ({"NH"}, [], [])
    assert match_airlines(chart, ["ana"], shared)[0] == {"NH"}              # not Air Canada
    assert match_airlines(chart, ["avios"], shared)[0] == {"EI", "BA", "IB", "QR", "VY"}
    assert match_airlines(chart, ["aeromexico"], shared)[0] == {"AM"}       # the accent doesn't matter
    assert match_airlines(chart, ["flying blue"], shared)[0] == {"AF"}
    assert match_airlines(chart, ["blue"], shared)[0] == {"AF"}             # not JetBlue (TrueBlue)
    found, notes, unmatched = match_airlines(chart, ["UA", "zz", "nope"], shared)
    assert (found, notes, unmatched) == ({"UA"}, [], ["zz", "nope"])


def test_an_airline_on_anothers_program_shows_that_row(chart):
    shared = shared_programs(text("README.md"))
    # the README: Flying Blue is also KLM (KL) and TAROM (RO); Miles & More also OS, SN, OU, AZ, LO, LX
    assert [(s["code"], s["host"]) for s in shared] == [("KL", "AF"), ("RO", "AF"), ("OS", "LH"), ("SN", "LH"),
                                                        ("OU", "LH"), ("AZ", "LH"), ("LO", "LH"), ("LX", "LH")]
    assert shared[0] == {"code": "KL", "name": "KLM", "host": "AF", "program": "Flying Blue"}
    assert shared[-1]["name"] == "SWISS" and shared[-1]["program"] == "Miles & More"
    assert match_airlines(chart, ["kl"], shared) == ({"AF"}, ["KLM (KL) uses Flying Blue, listed under AF"], [])
    assert match_airlines(chart, ["swiss"], shared) == ({"LH"}, ["SWISS (LX) uses Miles & More, listed under LH"], [])
    assert match_airlines(chart, ["kl"], []) == (set(), [], ["kl"])
    # a second term naming a row an earlier term found is matched too
    assert match_airlines(chart, ["OS", "austrian"], shared) == (
        {"LH"}, ["Austrian Airlines (OS) uses Miles & More, listed under LH"], [])
    assert match_airlines(chart, ["avios", "british", "BA"], shared)[2] == []


def test_currencies_compared(chart):
    rows = {r["key"]: r for r in program_rows(chart)}
    assert list(rows) == ["amex", "citi", "chase", "c1", "bilt", "marriott"]
    chase = rows["chase"]
    # Chase's twelve: EI B6 WN, BA CX IB QF, AF VS, AC SQ UA, every one 1:1
    assert (chase["name"], chase["partners"], chase["ratios"]) == ("Chase UR", 12, "1:1 ×12")
    assert [r["kind"] for r in rows.values()] == ["bank"] * 5 + ["hotel"]
    assert (chase["none"], chase["oneworld"], chase["skyteam"], chase["star"]) == (3, 4, 2, 3)
    assert rows["amex"]["ratios"] == "1:1 ×12; 1:0.8 ×3 (EK B6 CX); 1:1.6 ×1 (AM)"
    assert rows["marriott"]["ratios"].endswith("3:1 ×3 (AA DL AV); 3:1.5 ×1 (UA); 200:1.25 ×1 (NZ)")
    # sole: no other bank currency reaches them; Marriott does reach DL and NH, and isn't counted
    assert rows["amex"]["sole"] == "DL NH" and rows["chase"]["sole"] is None
    assert "CZ" in rows["marriott"]["sole"].split() and "NH" not in rows["marriott"]["sole"].split()


def test_change_log_rows_read_as_github_renders_them():
    rows = parse_changes(text("README.md"))
    assert len(rows) == 63 and len({r["date"] for r in rows}) == 30
    assert rows[0] == {"date": "2026-11-18", "change": "Added Cathay Pacific Asia Miles (CX) and Qantas Frequent "
                       "Flyer (QF) as Chase UR transfer partners, both at 1:1.", "older": False}
    # the heading reads `2026-11.04`; a line right under a bullet continues it
    assert rows[1] == {"date": "2026-11-04", "change": "CM is now a transfer partner of C1 at ratio 1:0.75.",
                       "older": False}
    assert rows[2]["change"] == "CM is now a transfer partner of Citi at ratio 1:1. (and the following old changes)"
    assert rows[3]["change"].startswith("Combined Alaska Mileage Plan") and rows[3]["date"] == "2026-11-04"
    # the lines after that mark are older changes written down on 2026-11-04; the next entry starts afresh
    assert [r["older"] for r in rows[:4]] == [False, False, False, True] and all(r["older"] for r in rows[3:20])
    assert rows[20] == {"date": "2026-10-20", "change": "B6 is now a transfer partner of Bilt at ratio 1:1.",
                        "older": False} and sum(r["older"] for r in rows) == 17
    assert rows[-1] == {"date": "2018-02-14", "change": "Add Aer Lingus (EI) and Iberia (IB) to Chase as these can be "
                        "transferred directly as of 11/2017.", "older": False}
    assert updated(rows) == "2026-11-18"
    assert section(text("README.md"), "Note").startswith("* Chase UR is short for Chase Ultimate Rewards points")
    assert "## Change Log" not in section(text("README.md"), "Note")


def test_the_log_is_searched_by_code_and_by_a_currencys_names(chart):
    rows = parse_changes(text("README.md"))

    def hits(term):
        patterns = change_patterns(term, chart, shared_programs(text("README.md")))
        return [r["change"] for r in rows if any(p.search(r["change"]) for p in patterns)]

    chase = hits("chase")
    assert len(chase) == 7 and any("transfer partner of UR" in c for c in chase)      # the log says UR
    alaska = hits("as")                # the code AS, or the airline's name: never the word `as`
    assert len(alaska) == 3 and all("AS" in c or "Alaska" in c for c in alaska)
    assert len(hits("marriott")) == 15 and any("MB transfer partners" in c for c in hits("bonvoy"))
    assert [c[:9] for c in hits("EK")] == ["EK is no ", "Emirates ", "Emirates "] and hits("Kenya") == [rows[17]["change"]]
    assert hits("KL") == []


def test_airlines_two_currencies_both_reach(action_runner, raw, wotp):
    out = run(action_runner, wotp, "airlines", {"points": ["UR", "MR"], "match": "all"}, json_out=True)
    assert out["points"] == "all of amex, chase" and out["total"] == 10 and out["updated"] == "2026-11-18"
    assert [r["code"] for r in out["airlines"]] == ["EI", "B6", "BA", "CX", "IB", "QF", "AF", "VS", "AC", "SQ"]
    shown = run(action_runner, wotp, "airlines", {"points": ["chase"], "alliance": "star"})
    assert "airlines[3]{code,name,alliance,chase,routes,best}:" in shown and "by_alliance: star 3" in shown
    # best is among the named currencies; routes still counts Bilt and Marriott
    assert 'UA,United Airlines (MileagePlus),star,"1:1",3,"1:1 chase"' in shown and 'ratios: "chase: 1:1 ×3"' in shown


def test_named_currencies_get_their_ratios_summed_up_and_their_own_best(action_runner, raw, wotp):
    out = run(action_runner, wotp, "airlines", {"points": ["citi", "c1"], "match": "all"}, json_out=True)
    assert out["total"] == 14
    assert out["ratios"] == "citi: 1:1 ×13; 1:0.8 ×1 (EK) | c1: 1:1 ×9; 1:0.75 ×4 (EK JL CM BR); 1:0.6 ×1 (B6)"
    best = {r["code"]: r["best"] for r in out["airlines"]}
    assert best["EK"] == "1:0.8 citi" and best["B6"] == "1:1 citi" and best["AF"] == "1:1 citi c1"   # never bilt


def test_airlines_none_reach_and_paging(action_runner, raw, wotp):
    out = run(action_runner, wotp, "airlines", {"match": "none", "limit": 5})
    assert "count: 5 of 24 total" in out and "esum wotp airlines --match none --offset 5 --limit 5" in out
    out = run(action_runner, wotp, "airlines", {"points": ["chase", "amex"], "match": "none", "alliance": "star"},
              json_out=True)
    assert out["total"] == 14 and "UA" not in {r["code"] for r in out["airlines"]}
    assert out["unreachable"] == "5 of these no currency reaches (AI OZ MS LH SA)"
    assert "TK" in {r["code"] for r in out["airlines"]}            # Citi, Capital One and Bilt reach it; these two don't


def test_airlines_names_a_shared_program_and_an_unmatched_term(action_runner, raw, wotp):
    out = run(action_runner, wotp, "airlines", {"airline": ["klm", "zz"]})
    assert 'shared: "KLM (KL) uses Flying Blue, listed under AF"' in out and "unmatched: zz" in out
    typo = run(action_runner, wotp, "airlines", {"airline": ["Etihadx"]})
    assert 'unmatched: "Etihadx (closest on the chart: EY Etihad (Etihad Guest))"' in typo
    assert "AF,Air France (Flying Blue),skyteam" in out
    empty = run(action_runner, wotp, "airlines", {"airline": ["zz"]})
    assert "0 found: no airline program on the chart matches airline zz" in empty
    assert "Run `esum wotp airlines` to list the whole chart" in empty
    assert "Run `esum wotp notes` to see which airlines are listed under another airline's program" in empty


def test_an_empty_answer_offers_wider_searches(action_runner, raw, wotp):
    out = run(action_runner, wotp, "airlines", {"airline": ["delta"], "points": ["bilt"], "match": "all"})
    assert "0 found: no airline program on the chart matches airline delta; points all of bilt" in out
    assert "esum wotp airlines --airline delta` to see every currency's ratio" in out
    assert "esum wotp airlines --points bilt --match all` to drop the airline filter" in out
    assert "to list the whole chart" in out
    out = run(action_runner, wotp, "changes", {"query": ["bilt"], "since": "2026-11-10"})
    assert "0 found: no line of the change log mentions bilt on or after 2026-11-10" in out
    assert "esum wotp changes --query bilt` to search the whole log" in out and "to read the whole change log" in out


def test_airlines_answers_without_the_readme(action_runner, raw, wotp):
    del raw.routes[README]
    out = run(action_runner, wotp, "airlines", {"airline": ["NH", "kl"]})
    assert "updated: unknown (the README answered HTTP 404)" in out and "unmatched: kl" in out
    assert 'NH,All Nippon Airways (ANA Mileage Club),star,"1:1"' in out


def test_cli_outcomes(action_runner, raw, capsys):
    reg = Registry.discover()
    assert action_runner.cli(["wotp", "airlines", "--points", "wells"], registry=reg) == 1
    out = capsys.readouterr().out
    assert "the chart has no points currency 'wells'; it has amex (AmEx MR)" in out and "esum wotp programs" in out
    assert action_runner.cli(["wotp", "airlines", "--airline", "zz"], registry=reg) == 0
    assert action_runner.cli(["wotp", "airlines", "--alliance", "skyteem"], registry=reg) == 2
    assert action_runner.cli(["wotp", "changes", "--since", "yesterday"], registry=reg) == 2
    assert action_runner.cli(["wotp", "changes", "--query", "NH"], registry=reg) == 0
    assert "0 found: no line of the change log mentions NH" in capsys.readouterr().out


def test_programs_action(action_runner, raw, wotp):
    out = run(action_runner, wotp, "programs", {})
    assert "airlines: 63" in out and "unreachable: 24 (LY MH WY" in out and "count: 6" in out
    assert 'chase,Chase UR,bank,12,3,4,2,3,"1:1 ×12",null' in out
    assert "esum wotp airlines --points <key> <key> --match all" in out


def test_changes_action_filters_by_date_and_words(action_runner, raw, wotp):
    out = run(action_runner, wotp, "changes", {"since": "2026-10-01"}, json_out=True)
    assert out["total"] == 21 and out["since"] == "2026-10-01" and out["oldest"] == "2018-02-14"
    assert {r["date"] for r in out["changes"]} == {"2026-11-18", "2026-11-04", "2026-10-20"}
    assert out["older"] == "17 of these are older changes written down on 2026-11-04; the log doesn't say when they happened"
    out = run(action_runner, wotp, "changes", {"since": "2026-10-01", "older": False}, json_out=True)
    assert out["total"] == 4 and out["older"] == "17 older changes left out" and not any(r["older"] for r in out["changes"])
    out = run(action_runner, wotp, "changes", {"query": ["cx", "bilt"], "limit": 2})
    assert "query: \"cx, bilt\"" in out and "count: 2 of 8 total" in out and "changes[2]{date,change,older}:" in out and ",false" in out
    assert "esum wotp changes --query cx bilt --offset 2 --limit 2" in out
    assert len(raw.requests) >= 2


def test_notes_action(action_runner, raw, wotp):
    out = run(action_runner, wotp, "notes", {})
    assert "Citi TYP ratios shown assume an eligible premium ThankYou card" in out
    assert "| United MileagePlus (UA) | 3:1 | 10,000 miles | 30,000 miles | 3:1.5 |" in out
    assert "Change Log" not in out
    data = run(action_runner, wotp, "notes", {}, json_out=True)
    assert len(data["shared"]) == 8 and data["notes"].startswith("* Chase UR")


def test_a_csv_that_changes_shape():
    # a new currency column shows under its key; a header's stray spaces don't lose its cells;
    # a cell that isn't a ratio is still a link, as the page draws one for any non-empty cell
    grown = parse_chart("code, name ,alliance,link_order, amex ,wf\r\n"
                        "NH,ANA (Mileage Club),starAlliance,1,1:1,1:2\r\n"
                        "LH,Lufthansa (Miles & More),starAlliance,2,soon,\r\n")
    assert grown.programs == ("amex", "wf")
    nh, lh = grown.rows
    assert (nh["name"], nh["amex"], nh["wf"], nh["routes"], nh["best"]) == ("ANA (Mileage Club)", "1:1", "1:2", 2, "1:2 wf")
    assert (lh["amex"], lh["wf"], lh["routes"], lh["best"]) == ("soon", None, 1, None)
    wf = program_rows(grown)[1]
    assert (wf["key"], wf["name"], wf["kind"], wf["partners"], wf["sole"]) == ("wf", "wf", "bank", 1, None)
    assert resolve_program("wf", grown.programs) == "wf"


def test_a_word_of_a_currencys_name_is_not_the_currency(chart):
    assert resolve_program("rewards", chart.programs) is None and resolve_program("one", chart.programs) is None
    rows = parse_changes(text("README.md"))
    patterns = change_patterns("rewards", chart, [])
    hits = [r["change"] for r in rows if any(p.search(r["change"]) for p in patterns)]
    assert len(hits) == 3 and any("Asante Rewards" in h for h in hits) and any("Atmos Rewards" in h for h in hits)


def test_the_old_changes_mark_on_a_line_of_its_own_and_a_crlf_readme():
    readme = ("# Chart\r\n\r\n## Note\r\n\r\n* A note.\r\n\r\n## Change Log\r\n\r\n### 2026-11-04\r\n\r\n"
              "* CM is now a partner of Citi.\r\n\r\n(and the following old changes)\r\n\r\n"
              "* EY is no longer a partner of MR.\r\n\r\n### 2026-10-20\r\n\r\n* B6 is now a partner of Bilt.\r\n")
    assert section(readme, "Note") == "* A note."
    assert [(r["date"], r["older"]) for r in parse_changes(readme)] == [
        ("2026-11-04", False), ("2026-11-04", True), ("2026-10-20", False)]


def test_a_page_past_the_last_row_says_so(action_runner, raw, wotp):
    out = run(action_runner, wotp, "airlines", {"points": ["chase"], "offset": 20})
    assert "0 found: no row at offset 20: 12 matched" in out and "to start from the first row" in out
    assert "no airline program on the chart matches" not in out
    out = run(action_runner, wotp, "changes", {"offset": 100})
    assert "0 found: no row at offset 100: 63 matched" in out and "to start from the first row" in out


def test_no_older_emptying_the_answer_says_so(action_runner, raw, wotp):
    out = run(action_runner, wotp, "changes", {"query": ["Alaska"], "since": "2026-11-01", "older": False})
    assert "older: 1 older change left out" in out
    assert "0 found: no line of the change log mentions Alaska on or after 2026-11-01 (1 older change left out by --no-older)" in out
    assert "to include the older changes" in out


def test_a_named_currency_that_reaches_none_of_the_rows(action_runner, raw, wotp):
    out = run(action_runner, wotp, "airlines", {"airline": ["NH", "DL"], "points": ["chase", "amex"]})
    assert 'ratios: "amex: 1:1 ×2 | chase: none of these"' in out


def test_a_readme_without_a_change_log_still_answers_the_chart(action_runner, raw, wotp):
    raw.routes[README] = Response(200, README, {"content-type": "text/plain; charset=utf-8"}, b"# Chart\n\n## Note\n\n* A note.\n")
    out = run(action_runner, wotp, "airlines", {"airline": ["NH"]})
    assert "updated: unknown (the README's change log has no dates)" in out and "NH,All Nippon Airways" in out
