"""One listing (or search) and one thread per built-in site, read-only.

A thread check reads the first item of the site's listing, so it follows whatever is live today.
Add a row here when you add a site.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest


def rows(data: dict, key: str) -> list[dict]:
    found = data.get(key)
    assert isinstance(found, list) and found, f"no {key} in {sorted(data)}"
    assert all(r.get("title") for r in found), f"a row without a title: {found[0]}"
    return found


def posts(data: dict) -> list[dict]:
    items = [p for p in data.get("posts") or [] if "number" in p]   # Post rows, not gap markers
    assert items, f"no posts in {sorted(data)}"
    # the opening post is #1, or #0 on WordPress, where comments start at #1
    assert items[0]["number"] in (0, 1) and all(p.get("text") for p in items[:3])
    return items


# site, listing args, the key holding its rows, thread args built from the first row
SITES = [
    ("uscf", ["latest"], "topics", lambda r: ["--id", str(r["id"])]),
    ("doc", ["recent", "--limit", "3"], "posts", lambda r: ["--id", str(r["id"])]),
    ("uscreditcardguide", ["recent", "--limit", "3"], "posts", lambda r: ["--id", str(r["id"])]),
    ("meb", ["recent", "--limit", "3"], "posts", lambda r: ["--id", str(r["id"])]),
    ("flyertalk", ["forum", "--id", "722"], "threads", lambda r: ["--id", str(r["id"])]),
    ("flyertalk", ["recent"], "threads", lambda r: ["--id", str(r["id"])]),
    ("reddit", ["listing", "--subreddit", "churning", "--limit", "3"], "posts",
     lambda r: ["--id", str(r["id"])]),
    ("hn", ["stories", "--limit", "3"], "stories", lambda r: ["--id", str(r["id"])]),
    ("v2ex", ["topics", "--feed", "hot", "--limit", "3"], "topics", lambda r: ["--id", str(r["id"])]),
    ("substack", ["archive", "--publication", "noahpinion", "--limit", "3"], "posts",
     lambda r: ["--post", r["url"]]),
    ("kanxue", ["forum", "--id", "161"], "threads", lambda r: ["--id", str(r["id"])]),
    ("wuaipojie", ["latest", "--view", "hot"], "threads", lambda r: ["--id", str(r["id"])]),
    ("linuxdo", ["latest"], "topics", lambda r: ["--id", str(r["id"])]),
    ("nodeseek", ["listing", "--category", "tech"], "threads", lambda r: ["--id", str(r["id"])]),
    ("nga", ["hot", "--fid", "-7"], "threads", lambda r: ["--id", str(r["id"])]),   # login-only
    ("onepoint3acres", ["latest", "--view", "hot"], "threads", lambda r: ["--id", str(r["id"])]),
]

SEARCHES = [
    ("uscf", ["search", "--query", "chase"], "topics"),
    ("doc", ["search", "--query", "chase", "--limit", "3"], "posts"),
    ("uscreditcardguide", ["search", "--query", "chase", "--limit", "3"], "posts"),
    ("meb", ["search", "--query", "amex", "--limit", "3"], "posts"),
    ("flyertalk", ["search", "--query", "sapphire", "--show", "threads"], "results"),
    ("reddit", ["search", "--query", "sapphire", "--subreddit", "churning", "--limit", "3"], "posts"),
    ("hn", ["search", "--query", "python", "--limit", "3"], "results"),
    ("v2ex", ["search", "--query", "python", "--limit", "3"], "results"),
    ("substack", ["search", "--publication", "noahpinion", "--query", "china", "--limit", "3"], "posts"),
    ("linuxdo", ["search", "--query", "python"], "topics"),   # login-only: skipped as a guest
    ("nga", ["search", "--query", "python"], "threads"),        # login-only: skipped as a guest
    ("onepoint3acres", ["search", "--query", "H1B", "--type", "keywords"], "threads"),
]


@pytest.mark.parametrize("site, listing, key, thread_args", SITES, ids=[s[0] for s in SITES])
def test_listing_then_thread(esum, site, listing, key, thread_args):
    first = rows(esum(site, *listing), key)[0]
    thread = esum(site, "thread", *thread_args(first), "--to", "5")
    posts(thread)


@pytest.mark.parametrize("site, args, key", SEARCHES, ids=[s[0] for s in SEARCHES])
def test_search(esum, site, args, key):
    rows(esum(site, *args), key)


def test_reddit_recent_comments_duplicates_and_wiki_history(esum):
    """Reddit's lists that are not posts: newest comments, other discussions of a link, wiki revisions."""
    comments = esum("reddit", "recent-comments", "--subreddit", "churning", "--limit", "3")["comments"]
    assert comments and all(c["post_id"] and c["author"] and c["date"] for c in comments)
    news = rows(esum("reddit", "listing", "--subreddit", "news", "--sort", "top", "--time", "week", "--limit", "1"),
                "posts")[0]
    found = esum("reddit", "duplicates", "--id", news["id"], "--limit", "3")
    assert found["post"] == news["id"] and found["title"]
    assert all(p["id"] != news["id"] and p["title"] for p in found["posts"])
    history = esum("reddit", "wiki", "--subreddit", "churning", "--show", "history")["revisions"]
    assert history and all(r["id"] and r["author"] and r["date"] for r in history)


@pytest.mark.parametrize("name, gain", [("cardcenter", "margin"), ("alignedincentives", "profit")])
def test_gift_card_buyers_rates_deals_deal(esum, name, gain):
    """The gift card buyers have rates and deals, not threads: rates → deal, and the deals list.
    Both need the user's login; a guest run skips."""
    rates = rows(esum(name, "rates", "--limit", "5"), "rates")
    assert all(r["brand"] and r["value"].startswith("$") and r["deal"] and gain in r for r in rates)
    listed = rows(esum(name, "deals", "--limit", "5"), "deals")
    assert all(r["id"] and r["title"] and r["pay"].endswith("%") for r in listed)
    detail = esum(name, "deal", "--id", str(rates[0]["deal"]))
    assert str(detail["id"]) == str(rates[0]["deal"]) and detail["title"] and detail["rates"]
    assert "@" not in json.dumps(detail)        # never the account the page was read as


def test_savewise_rates_cards_top_stack_giftcards(esum):
    """Savewise has rates, not threads: one store's portal rates and card offers (the extension's
    API, an anonymous token), then the public Top Offers, stacking and gift card pages."""
    def have(found):
        assert found, "an empty list"
        return found

    shop = esum("savewise", "rates", "--merchant", "walmart.com")
    assert shop["domain"] == "walmart.com" and shop["rates"] and all(r["portal"] and r["rate"] for r in shop["rates"])
    assert esum("savewise", "rates", "--merchant", "best buy")["domain"] == "bestbuy.com"
    assert all(r["bank"] and r["rate"] for r in have(esum("savewise", "cards", "--merchant", "walmart.com")["cards"]))
    assert all(r["merchant"] and r["rate"] for r in have(esum("savewise", "top", "--by", "miles", "--limit", "3")["top"]))
    assert all(r["card"] and r["portal"] for r in have(esum("savewise", "stack", "--limit", "3")["stack"]))
    assert all(r["discount"].endswith("%") for r in have(esum("savewise", "giftcards", "--limit", "3")["giftcards"]))


def test_offerlove_latest_offer_search_merchant(esum):
    """Offer.love has offers, not threads: latest → offer, plus search and a merchant page."""
    first = rows(esum("offerlove", "latest", "--limit", "3"), "offers")[0]
    detail = esum("offerlove", "offer", "--id", first["id"])
    assert detail["id"] == first["id"] and detail["bank"] and detail["terms"] and "<" not in detail["terms"]
    found = rows(esum("offerlove", "search", "--query", "walmart"), "offers")
    assert all(r["id"].count("/") == 2 for r in found)
    shop = esum("offerlove", "merchant", "--slug", found[0]["merchant"], "--past")
    assert shop["offers"] and all(r["bank"] for r in shop["offers"])
    listed = esum("offerlove", "merchants", "--query", "walmart")
    assert listed["total"] >= 1 and "merchants" in listed["site_total"]
    assert all(r["merchant"] and r["name"] and r["offers"] >= 1 for r in listed["merchants"])


def test_matrix_airports_search_detail(esum):
    """ITA Matrix has no threads: airports → search → detail instead (a search takes 20-60 s)."""
    codes = esum("matrix", "airports", "--query", "tokyo")["locations"]
    assert any(r["code"] == "TYO" for r in codes)
    day = (date.today() + timedelta(days=60)).isoformat()
    found = esum("matrix", "search", "--origin", "LAX", "--dest", "TYO", "--depart", day, "--limit", "3")
    first = found["itineraries"][0]
    assert first["id"] and first["price"] and " → " in first["itinerary"]
    detail = esum("matrix", "detail", "--search", found["search"], "--id", str(first["id"]))
    assert detail["fares"] and detail["fares"][0]["basis"] and detail["price"]

def test_aerolopa_fleet_seatmap_schedule_routes(esum):
    """AeroLOPA has no threads: fleet → seat map → its best seats and top route's schedule, plus an airline's routes."""
    configs = esum("aerolopa", "fleet", "--airline", "ua")["configs"]
    first = next((r for r in configs if r["code"] == "ua-77m"), configs[0])
    assert first["code"].startswith("ua-") and first["seats"] > 0
    m = esum("aerolopa", "seatmap", "--code", first["code"])
    assert m["cabins"] and all(c["rows"] and c["seats"] for c in m["cabins"])
    assert m["image"].startswith("https://") and "not_in_data" in m and sum(m["ratings"].values()) == m["seats"]
    best = esum("aerolopa", "seats", "--code", first["code"], "--rating", "good", "--sort", "best")["seats"]
    assert best and all(s["rating"] == "good" and s["seat"] and s["cabin"] for s in best)
    flights = esum("aerolopa", "schedule", "--route", "LON-NYC")["flights"]
    assert flights and all(f["code"] and f["tier"] for f in flights)
    found = esum("aerolopa", "routes", "--airline", "ua", "--type", "777-200")["flights"]
    assert found and any("%" in f["layouts"] for f in found)


def test_wotp_airlines_programs_changes_notes(esum):
    """Wings of the Points has no threads: the chart by airline and by currency, the currencies
    compared, the change log and the notes."""
    chart = esum("wotp", "airlines")
    assert chart["total"] >= 50 and all(len(r["code"]) == 2 and r["name"] and r["alliance"] for r in chart["airlines"])
    found = esum("wotp", "airlines", "--airline", "AF", "klm")
    assert [r["code"] for r in found["airlines"]] == ["AF"] and "KLM" in found["shared"] and found["updated"][:2] == "20"
    both = esum("wotp", "airlines", "--points", "UR", "MR", "--match", "all")["airlines"]
    assert both and all(":" in r["chase"] and ":" in r["amex"] and r["best"] for r in both)
    currencies = esum("wotp", "programs")["programs"]
    assert {"chase", "amex"} <= {r["key"] for r in currencies} and all(r["partners"] > 0 and r["ratios"] for r in currencies)
    log = esum("wotp", "changes", "--query", "chase", "--limit", "3")["changes"]
    assert log and all(date.fromisoformat(r["date"]) and r["change"] for r in log)
    assert "Marriott" in esum("wotp", "notes")["notes"]


def test_gflights_search_returns_detail(esum):
    """Google Flights has no threads: a round-trip search → returns → detail with its booking link."""
    out = (date.today() + timedelta(days=45)).isoformat()
    back = (date.today() + timedelta(days=52)).isoformat()
    found = esum("gflights", "search", "--origin", "SFO", "--dest", "LHR", "--depart", out, "--return", back,
                 "--limit", "3")
    first = found["itineraries"][0]
    assert first["id"] and first["price"] and " → " in first["itinerary"]
    back_rows = esum("gflights", "returns", "--search", found["search"], "--id", first["id"], "--limit", "3")
    ret = back_rows["returns"][0]
    detail = esum("gflights", "detail", "--search", back_rows["search"], "--id", ret["id"])
    assert len(detail["slices"]) == 2
    assert detail["booking_url"].startswith("https://www.google.com/travel/flights/booking?tfs=")


def test_gflights_search_all_reads_the_rows_behind_view_more_flights(esum):
    """`search --all` clicks "View more flights" in ESUM's browser and reads Google's English rows:
    a reworded page shows here as rows left out or as times, durations or prices gone unknown."""
    out = (date.today() + timedelta(days=45)).isoformat()
    found = esum("gflights", "search", "--origin", "SFO", "--dest", "SIN", "--depart", out, "--all", "--limit", "300")
    more = [row for row in found["itineraries"] if row.get("more")]
    assert len(more) >= 20, found["listed"]
    assert "left out" not in found["listed"], found["listed"]
    unknown = {row["id"]: row["missing"] for row in more if set(row["missing"]) & {"times", "duration"}}
    assert not unknown, unknown         # times and duration on every row
    unpriced = [row["id"] for row in more if "price" in row["missing"]]     # Google lists a few rows without one
    assert len(unpriced) <= len(more) // 10, unpriced
    assert sum("layovers" in row["missing"] for row in more) <= len(more) // 10, found["listed"]
    # a row from behind the button is named by its flights, and that name alone reads it again
    row = more[0]
    assert row["id"].count(":") == 2 * len(row["legs"]), row["id"]
    detail = esum("gflights", "detail", "--search", found["search"], "--id", row["id"])
    assert [leg["flight"] for leg in detail["slices"][0]["legs"]] == [leg["flight"] for leg in row["legs"]]
    assert detail["booking_url"].startswith("https://www.google.com/travel/flights/booking?tfs=")


def test_flightconnections_route_schedule_connections_destinations_airline(esum):
    """FlightConnections has routes, not threads: route → schedule, the site's one-stop options,
    an airport's nonstop destinations by airline, and an airline's top routes (5 pages, 3 s apart)."""
    route = esum("flightconnections", "route", "--origin", "JFK", "--dest", "LHR")
    assert route["per_week"] and {"BA", "VS"} <= {r["code"] for r in route["airlines"]}
    assert all(r["departures"] and all(d.split()[-1][:2].isdigit() for d in r["departures"]) for r in route["airlines"])
    flights = esum("flightconnections", "schedule", "--origin", "JFK", "--dest", "LHR", "--airline", "BA",
                   "--limit", "5")["flights"]
    assert flights and all(r["flight"].startswith("BA ") and r["depart"] and r["dates"] for r in flights)
    options = esum("flightconnections", "connections", "--origin", "AEY", "--dest", "AGA")["connections"]
    assert options and all(r["via"] and r["leg1"] and r["leg2"] for r in options)
    found = esum("flightconnections", "destinations", "--airport", "JFK", "--airline", "BA")["destinations"]
    assert any(r["code"] == "LHR" for r in found)
    # LAX words some routes' sentences in a form of its own (SFO, LAS): every row with flights names its airlines
    lax = esum("flightconnections", "destinations", "--airport", "LAX", "--limit", "300")["destinations"]
    assert lax and all(r["airlines"] for r in lax if r["flights"])
    assert not any(r["flights"] and "not operational" in r["note"] for r in lax)    # a namesake's sentence
    top = esum("flightconnections", "airline", "--code", "B6", "--show", "routes", "--limit", "3")["routes"]
    assert all(r["origin"] and r["dest"] for r in top)
