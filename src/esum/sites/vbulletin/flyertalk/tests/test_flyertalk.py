"""FlyerTalk (flyertalk, on the vBulletin engine): its actions through the CLI, with the T3 backend
faked, on FlyerTalk's scrubbed pages. The vBulletin engine's parser tests read the same pages, so they
are the engine's fixtures, in the checkout's tests/fixtures/vbulletin/ (see the README there); the
parser tests are tests/test_parsers_vbulletin.py."""

from __future__ import annotations

import json
import re
import shlex
from urllib.parse import parse_qs

import pytest

from esum.core.registry import Registry
from esum.transport import Response

from support import engine_fixtures

FIX = engine_fixtures("vbulletin")
FORUM = "https://www.flyertalk.com/forum"
THREAD_URL = f"{FORUM}/credit-card-programs/2213102-us-bank-altitude-reserve-card-2026-a.html"
SEARCH_FINAL = f"{FORUM}/search.php?searchid=70127352&query=altitude-reserve"


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def page(url: str, name: str | None = None, text: str | None = None) -> Response:
    """A FlyerTalk response: ISO-8859-1 as the site serves it (decoded as windows-1252)."""
    body = (text if text is not None else fixture(name)).encode("cp1252", errors="xmlcharrefreplace")
    return Response(200, url, {"content-type": "text/html; charset=ISO-8859-1"}, body)


TOKEN = "1700000000-0123456789abcdef0123456789abcdef01234567"   # synthetic member token


def search_form(token: str) -> str:
    return (f'<form action="search.php?do=process" method="post">'
            f'<input type="hidden" name="securitytoken" value="{token}" /><input name="query" /></form>')


# --- whole actions through the CLI (T3 backend faked) -----------------------------------------------


def error_page(message: str) -> str:
    return (f"<html><body><div class='tbox'><!-- main error message --><div>{message}</div>"
            f"<!-- / main error message --></div></body></html>")


class FakeInPage:
    """Canned FlyerTalk responses keyed by URL (GET) or `POST <url>`; a list answers in turn.
    Records requests."""

    def __init__(self):
        self.routes: dict[str, Response | list[Response]] = {}
        self.requests = []

    def send(self, request, *, before_send=lambda: None):
        before_send()
        self.requests.append(request)
        key = request.url if request.method == "GET" else f"{request.method} {request.url}"
        route = self.routes.get(key)
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        return route or Response(404, request.url, {}, b"not found")

    def close(self):
        pass


@pytest.fixture
def ft(action_runner):
    backend = FakeInPage()
    backend.routes[f"{FORUM}/search.php"] = page(f"{FORUM}/search.php", text=search_form(TOKEN))
    action_runner.backend = backend
    return backend


@pytest.fixture
def cli(capsys, monkeypatch, action_runner):
    monkeypatch.setenv("ESUM_SPILL_CHARS", "10000000")
    registry = Registry.discover(custom_root=None)

    def _run(*argv: str):
        code = action_runner.cli(argv, registry=registry)
        out = capsys.readouterr()
        return code, out.out, out.err

    return _run


def showthread(**params) -> str:
    from urllib.parse import urlencode
    return f"{FORUM}/showthread.php?{urlencode(params)}"


def test_thread_page_flag_reads_one_page(ft, cli):
    ft.routes[showthread(t=2213102, page=6)] = page(THREAD_URL.replace(".html", "-6.html"), "thread_p6.html")
    code, out, err = cli("flyertalk", "thread", "--id", "2213102", "--page", "6")
    assert code == 0, err
    assert "untrusted" in out.splitlines()[0]
    assert "7 of 82 posts shown" in out and "82 posts in 6 page(s) of 15 · page 6" in out
    assert "[... omitted 75 post numbers (#1–#75), not read …]" in out and "[iahphx #82]" in out
    assert len(ft.requests) == 1


def test_thread_medium_fetches_head_and_tail_pages(ft, cli):
    ft.routes[showthread(t=2213102)] = page(THREAD_URL, "thread_p1.html")
    for n in (2, 4, 5, 6):   # any page content will do for the request plan; p6 holds #76–#82
        ft.routes[showthread(t=2213102, page=n)] = page(THREAD_URL, "thread_p6.html")
    code, out, err = cli("flyertalk", "thread", "--id", THREAD_URL)
    assert code == 0, err
    asked = [parse_qs(r.url.split("?", 1)[1]).get("page", ["1"])[0] for r in ft.requests]
    assert asked == ["1", "2", "4", "5", "6"]
    assert "[jolapo #1] (2026-01-13 10:24 · 👍1 · OP · ↩post " in out   # it quotes another thread
    assert "[trikotret #2] (2026-01-13 11:01 · ↩#1): > @jolapo:" in out
    assert "[... omitted 26 post numbers (#5–#30), deleted or hidden …]" in out and "11 of 82 posts shown" in out


def test_thread_by_post_reads_the_page_holding_it(ft, cli):
    final = f"{FORUM}/hyatt-world-hyatt/1307256-park-hyatt-paris-vend-me-review-master-thread-aug-2011-onward-342.html"
    ft.routes[showthread(p=37986891)] = page(final, "thread_by_post.html")
    code, out, err = cli("flyertalk", "thread", "--id", f"{final[:-9]}-post37986891.html")
    assert code == 0, err
    # the scrubbed page doesn't hold 37986891 (a deleted or hidden post): don't claim it does
    assert "post 37986891 not found on page 342" in out and "holds" not in out and "[... omitted 5115 post numbers (#1–#5115), not read …]" in out
    assert len(ft.requests) == 1


def test_thread_wikipost_shown_first(ft, cli):
    url = f"{FORUM}/credit-card-programs/2156048-2024-visa-mastercard-interchange-settlement.html"
    ft.routes[showthread(t=2156048)] = page(url, "wiki_p1.html")
    code, out, err = cli("flyertalk", "thread", "--id", "2156048", "--to", "2")
    assert code == 0, err
    body = out.split("\n\n", 1)[1]
    assert body.startswith("[Wikipost (last edit: phltraveler) #0] (2024-03-27 07:59): LINK TO PROPOSED")
    assert "[MASTERNC #1]" in out and "3 of 77 posts shown" in out


def test_search_posts_form_and_second_page(ft, cli):
    ft.routes[f"POST {FORUM}/search.php?do=process"] = page(SEARCH_FINAL, "search_posts.html")
    ft.routes[f"{FORUM}/search.php?searchid=70127352&page=2"] = page(SEARCH_FINAL + "&page=2", "search_posts.html")
    code, out, err = cli("flyertalk", "search", "--query", "altitude reserve", "--forum", "credit-card-programs",
                         "599", "--titles-only", "--days", "30", "--page", "2")
    assert code == 0, err
    assert ft.requests[0].url == f"{FORUM}/search.php"
    post = ft.requests[1]
    assert post.method == "POST" and post.headers["Content-Type"] == "application/x-www-form-urlencoded"
    form = parse_qs(post.content.decode())
    assert form["securitytoken"] == [TOKEN]
    assert form["query"] == ["altitude reserve"] and form["showposts"] == ["1"] and form["titleonly"] == ["1"]
    assert form["forumchoice[]"] == ["599", "599"] and form["childforums"] == ["1"]
    assert form["searchdate"] == ["30"] and form["beforeafter"] == ["after"]
    assert ft.requests[2].url.endswith("searchid=70127352&page=2")
    lines = out.splitlines()
    assert "page: 2" in lines and "pages: 40" in lines and "count: 4 of 989 total" in lines
    assert "results[4]{thread_id,post_id,title,snippet}:" in lines
    assert any(line.startswith("  2213102,37988887,US Bank") for line in lines)
    assert any("--page 3" in line and "for the next page" in line for line in lines)
    assert "  Run `esum flyertalk thread --id <thread_id> --post <post_id>` to read the page holding a post" in lines


def test_search_threads_and_no_matches(ft, cli):
    ft.routes[f"POST {FORUM}/search.php?do=process"] = page(SEARCH_FINAL, "search_threads.html")
    code, out, err = cli("flyertalk", "search", "--query", "altitude reserve", "--show", "threads")
    assert code == 0, err
    assert "results[4]{id,title,replies,last_post}:" in out
    assert "  2146615,US Bank Altitude Reserve Card (2024 - 2025)," in out
    code, out, err = cli("flyertalk", "search", "--query", "altitude reserve", "--show", "threads",
                         "--fields", "closed")
    assert "  2146615,US Bank Altitude Reserve Card (2024 - 2025)," in out and ",true" in out
    ft.routes[f"POST {FORUM}/search.php?do=process"] = page(f"{FORUM}/search.php?do=process", "search_none.html")
    code, out, err = cli("flyertalk", "search", "--query", "zzqxjvkwplmnb")
    assert code == 0, err
    assert "results: \"0 found: no posts match 'zzqxjvkwplmnb'\"" in out
    assert "without" not in out
    code, out, err = cli("flyertalk", "search", "--query", '"zzqx jvkw" +plmnb')
    assert code == 0, err
    assert "without" not in out


def test_search_rereads_a_stale_token_once(ft, cli):
    ft.routes[f"{FORUM}/search.php"] = [page(f"{FORUM}/search.php", text=search_form("stale")),
                                        page(f"{FORUM}/search.php", text=search_form(TOKEN))]
    ft.routes[f"POST {FORUM}/search.php?do=process"] = [
        page(f"{FORUM}/search.php?do=process", text=error_page(
            "Your submission could not be processed because you have logged in since the previous page was "
            "loaded.")),
        page(SEARCH_FINAL, "search_threads.html")]
    code, out, err = cli("flyertalk", "search", "--query", "altitude reserve", "--show", "threads")
    assert code == 0, err
    tokens = [parse_qs(r.content.decode())["securitytoken"][0] for r in ft.requests if r.method == "POST"]
    assert tokens == ["stale", TOKEN] and "  2146615,US Bank" in out


def test_search_follows_the_transfer_page(ft, cli):
    ft.routes[f"POST {FORUM}/search.php?do=process"] = page(f"{FORUM}/search.php?do=process", text=(
        '<meta http-equiv="Refresh" content="0; URL=search.php?searchid=70127352&amp;query=x">'
        "Thank you for searching. Please wait while we transfer you to the search results."))
    ft.routes[f"{FORUM}/search.php?searchid=70127352"] = page(SEARCH_FINAL, "search_threads.html")
    code, out, err = cli("flyertalk", "search", "--query", "altitude reserve", "--show", "threads")
    assert code == 0, err
    assert "  2146615,US Bank" in out


def test_forum_by_slug_decodes_iso_8859_1(ft, cli):
    ft.routes[f"{FORUM}/forumdisplay.php?f=599&page=2&sort=dateline&order=desc"] = \
        page(f"{FORUM}/credit-card-programs-599/", "forum.html")
    code, out, err = cli("flyertalk", "forum", "--id", "credit-card-programs", "--page", "2", "--sort", "newest")
    assert code == 0, err
    lines = out.splitlines()
    assert {"id: 599", "title: Credit Card Programs", "sort: newest", "page: 1", "pages: 304"} <= set(lines)
    assert "threads[" in out and "{id,title,replies,last_post}:" in out
    assert "  1479651,Credit card referral offers" in out.replace('"', "")
    assert "  Run `esum flyertalk forum --id credit-card-programs --page 2 --sort newest` for the next page" in lines
    code, out, err = cli("flyertalk", "forum", "--id", "credit-card-programs", "--page", "2", "--sort", "newest",
                         "--fields", "excerpt")
    assert code == 0, err
    assert "they’ve" in out


def test_unknown_forum_slug_is_an_error(ft, cli):
    code, err, _ = cli("flyertalk", "forum", "--id", "no-such-forum")
    assert code == 2 and "esum flyertalk forums" in err and not ft.requests


def test_no_permission_is_login_needed_and_other_errors_pass_through(ft, cli):
    ft.routes[showthread(t=1)] = page(showthread(t=1), text=error_page(
        "You are not logged in or you do not have permission to access this page."))
    code, err, _ = cli("flyertalk", "thread", "--id", "1")
    assert code == 3 and "LOGIN NEEDED" in err
    ft.routes[f"POST {FORUM}/search.php?do=process"] = page(f"{FORUM}/search.php?do=process", text=error_page(
        "This forum requires that you wait 30 seconds between searches. Please try again in 12 seconds."))
    code, err, _ = cli("flyertalk", "search", "--query", "x")
    assert code == 1 and "wait 30 seconds between searches" in err


def test_forums_catalog(cli):
    code, out, err = cli("flyertalk", "forums", "--query", "hyatt")
    assert code == 0, err
    assert "forums[1]{id,slug,name}:" in out and "  418,hyatt-world-hyatt,Hyatt | World of Hyatt" in out
    code, out, err = cli("flyertalk", "forums", "--query", "zzz")
    assert code == 0 and "0 found: no catalog forum matches" in out


LONG = "word " * 100


def quote_box(post_id: int, who: str, words: str) -> str:
    return (f'<div style="margin:1rem"><div><label>Quote:</label></div><div class="panel alt2"><div>Originally Posted by '
            f'<strong>{who}</strong> <a href="{FORUM}/x-1.html#post{post_id}" title="View Post"><i></i></a></div>'
            f'<div style="font-style:italic">{words}</div></div></div>')


def post_html(post_id: int, number: int, body: str) -> str:
    return (f'<div id="edit{post_id}"><a href="{FORUM}/{post_id}-post{number}.html" id="postcount{post_id}" '
            f'name="{number}">#{number}</a><div id="post_message_{post_id}">{body}</div><!-- / post #{post_id} -->')


def thread_html(*posts: str) -> str:
    return ('<h1>A long thread</h1><a title="Show results 1 to 15 of 30"></a><a title="Show results 16 to 30 of 30"></a>'
            + "".join(posts))


def two_pages(ft):
    """#15 is on page 1. On page 2, #16 quotes #15 and #17 quotes #16."""
    ft.routes[showthread(t=7)] = page(f"{FORUM}/x-7.html", text=thread_html(post_html(915, 15, "old")))
    ft.routes[showthread(t=7, page=2)] = page(f"{FORUM}/x-7-2.html", text=thread_html(
        post_html(916, 16, quote_box(915, "ann", LONG) + "reply"), post_html(917, 17, quote_box(916, "bob", LONG) + "reply")))


def test_a_quote_of_a_post_not_read_names_that_post_and_stays_whole(ft, cli):
    two_pages(ft)
    code, out, err = cli("flyertalk", "thread", "--id", "7", "--page", "2", "--json")
    assert code == 0, err
    posts = [p for p in json.loads(out)["data"]["posts"] if "number" in p]
    assert posts[0]["reply_to"] is None and posts[0]["reply_to_id"] == 915
    assert LONG.strip() in posts[0]["text"]

    code, out, err = cli("flyertalk", "thread", "--id", "7", "--page", "2")
    assert code == 0, err
    assert "↩post 915" in out


def test_a_quote_cut_short_says_where_the_rest_is(ft, cli):
    two_pages(ft)
    code, out, err = cli("flyertalk", "thread", "--id", "7", "--page", "2")
    assert code == 0, err
    assert "↩#16" in out and "[cut at 200 chars; whole in #16]" in out and LONG.strip() not in out.split("#17]")[1]


def test_a_range_still_numbers_a_quoted_post_it_fetched_and_keeps_the_words(ft, cli):
    two_pages(ft)
    code, out, err = cli("flyertalk", "thread", "--id", "7", "--from", "17", "--to", "17")
    assert code == 0, err
    assert "↩#16" in out and "↩post" not in out and "cut at" not in out and LONG.strip() in out


def test_a_whole_quote_keeps_its_links(ft, cli):
    ft.routes[showthread(t=7, page=2)] = page(f"{FORUM}/x-7-2.html", text=thread_html(
        post_html(916, 16, quote_box(915, "ann", 'see <a href="https://example.invalid/list">the list</a>'))))
    code, out, err = cli("flyertalk", "thread", "--id", "7", "--page", "2")
    assert code == 0, err
    assert "https://example.invalid/list" in out


MOVED_ROW = """<div class="trow text-center"><div class="tcell alt1" id="td_threadstatusicon_2225011">
<img class="inlineimg" src="https://www.flyertalk.com/forum/images/statusicon/thread_moved.gif" id="thread_statusicon_2225011" />
</div><div class="tcell alt1 text-left" id="td_threadtitle_2225011" title="Is it down?"><div> Moved
<h4 class="style-inherit" style="display: inline;"><a href="https://www.flyertalk.com/forum/credit-card-programs/2211257-new-bilt-cards.html" id="thread_title_2225011" class="">Transfer is down?</a></h4></div>
<div class="smallfont"><span style="cursor: pointer" onclick="window.open('https://www.flyertalk.com/forum/members/user_a.html', '_self')">user_a</span>
<span class="smallfont jumbo-txt"> on Oct 28, 2026</span></div></div>
<div class="tcell alt2 smallfont">-</div><div class="tcell alt1"> - </div><div class="tcell alt1"><span>-</span></div></div>"""


def test_a_moved_thread_says_so_and_names_where_it_went(ft, cli):
    ft.routes[f"{FORUM}/forumdisplay.php?f=599"] = page(f"{FORUM}/credit-card-programs-599/", text=MOVED_ROW)
    code, out, err = cli("flyertalk", "forum", "--id", "599", "--fields", "moved_to")
    assert code == 0, err
    assert '  2225011,"Moved to 2211257: Transfer is down?",null,null,2211257' in out.splitlines()


def test_a_moved_thread_names_its_target_in_the_default_columns(ft, cli):
    ft.routes[f"{FORUM}/forumdisplay.php?f=599"] = page(f"{FORUM}/credit-card-programs-599/", text=MOVED_ROW)
    code, out, err = cli("flyertalk", "forum", "--id", "599")
    assert code == 0, err
    assert '  2225011,"Moved to 2211257: Transfer is down?",null,null' in out.splitlines()


FOOTER = "All times are GMT -6. The time now is 10:12 PM."


def test_lists_name_their_time_zone(ft, cli):
    ft.routes[f"{FORUM}/forumdisplay.php?f=599"] = page(f"{FORUM}/credit-card-programs-599/", text=MOVED_ROW + FOOTER)
    code, out, err = cli("flyertalk", "forum", "--id", "599")
    assert code == 0, err
    assert "times: America/Chicago" in out.splitlines()


def test_a_member_time_zone_of_their_own_keeps_the_footer_offset(ft, cli):
    footer = "All times are GMT +1. The time now is 10:12 PM."
    ft.routes[f"{FORUM}/forumdisplay.php?f=599"] = page(f"{FORUM}/credit-card-programs-599/", text=MOVED_ROW + footer)
    code, out, err = cli("flyertalk", "forum", "--id", "599")
    assert code == 0, err
    assert "times: UTC+1" in out.splitlines()


def test_forum_dates_take_each_dates_daylight_saving(ft, cli):
    ft.routes[f"{FORUM}/forumdisplay.php?f=599"] = page(f"{FORUM}/credit-card-programs-599/",
                                                      text=fixture("forum.html") + FOOTER)
    code, out, err = cli("flyertalk", "forum", "--id", "599", "--json")
    assert code == 0, err
    rows = {r["id"]: r for r in json.loads(out)["data"]["threads"]}
    assert rows[1479651]["last_post"] == "2026-10-20T09:32:00-05:00"   # CDT, before 2026-11-01


@pytest.mark.parametrize('as_json', [False, True])
def test_thread_range_past_total_does_not_fetch_extra_pages(ft, cli, as_json):
    ft.routes[showthread(t=2213102)] = page(THREAD_URL, 'thread_p1.html')
    args = ['flyertalk', 'thread', '--id', '2213102', '--from', '999', '--to', '1000']
    code, out, err = cli(*args, *(['--json'] if as_json else []))
    assert code == 2, err
    assert [r.url for r in ft.requests] == [showthread(t=2213102)]
    assert 'ends at #82' in out
    assert 'esum flyertalk thread --id 2213102 --from 53 --to 82' in out


def test_empty_search_names_unindexed_terms_and_retries_without_them(ft, cli):
    ft.routes[f"POST {FORUM}/search.php?do=process"] = page(f"{FORUM}/search.php", "search_none.html")
    code, out, err = cli("flyertalk", "search", "--query", "sapphire 5/24", "--days", "365")
    assert code == 0, err
    assert "short words and punctuated terms, so '5/24' may match nothing" in out
    assert "Run `esum flyertalk search --query sapphire --days 365` to search without 5/24" in out


def test_thread_keeps_display_time_and_json_dates_have_each_dates_offset(ft, cli):
    html = fixture("thread_p1.html").replace("Jan 13, 2026 | 11:01 am", "Oct 27, 2026 | 11:01 am", 1) + FOOTER
    ft.routes[showthread(t=2213102)] = page(THREAD_URL, text=html)
    code, out, err = cli("flyertalk", "thread", "--id", "2213102", "--to", "2")
    assert code == 0, err
    assert "Times: America/Chicago" in out and "UTC-6" not in out
    assert "[jolapo #1] (2026-01-13 10:24 " in out and " #2] (2026-10-27 11:01" in out
    code, out, err = cli("flyertalk", "thread", "--id", "2213102", "--to", "2", "--json")
    assert code == 0, err
    dates = [p["date"] for p in json.loads(out)["data"]["posts"] if p.get("number") in (1, 2)]
    assert dates == ["2026-01-13T10:24:00-06:00", "2026-10-27T11:01:00-05:00"]


def test_search_json_dates_have_each_dates_offset(ft, cli):
    html = fixture("search_posts.html") + FOOTER
    ft.routes[f"POST {FORUM}/search.php?do=process"] = page(SEARCH_FINAL, text=html)
    code, out, err = cli("flyertalk", "search", "--query", "altitude reserve", "--json")
    assert code == 0, err
    data = json.loads(out)["data"]
    assert data["times"] == "America/Chicago"
    dates = [r["date"] for r in data["results"] if r["date"].startswith("2024-04-17T")]
    assert dates and all(d.endswith("-05:00") for d in dates)   # April: daylight saving


def test_empty_quoted_search_retries_a_well_formed_query(ft, cli):
    ft.routes[f"POST {FORUM}/search.php?do=process"] = page(f"{FORUM}/search.php", "search_none.html")
    code, out, err = cli("flyertalk", "search", "--query", '"chase sapphire 5/24" bonus')
    assert code == 0, err
    assert "Run `esum flyertalk search --query '\"chase sapphire\" bonus'` to search without 5/24" in out
    code, out, err = cli("flyertalk", "search", "--query", '"sapphire 5/24')
    assert code == 0, err
    assert "'5/24' may match nothing" in out and "to search without" not in out


def test_empty_search_retry_runs_with_a_leading_minus_query(ft, cli):
    ft.routes[f"POST {FORUM}/search.php?do=process"] = page(f"{FORUM}/search.php", "search_none.html")
    code, out, err = cli("flyertalk", "search", "--query=-spam 5/24")
    assert code == 0, err
    retry = re.search(r"Run `(esum flyertalk search [^`]+)` to search without 5/24", out)
    assert retry and retry.group(1) == "esum flyertalk search --query=-spam"
    code, out, err = cli(*shlex.split(retry.group(1))[1:])
    assert code == 0, err


def test_medium_and_range_reads_offer_next_range_commands(ft, cli):
    ft.routes[showthread(t=2213102)] = page(THREAD_URL, "thread_p1.html")
    for n in (2, 4, 5, 6):
        ft.routes[showthread(t=2213102, page=n)] = page(THREAD_URL, "thread_p6.html")
    code, out, err = cli("flyertalk", "thread", "--id", THREAD_URL)
    assert code == 0, err
    assert "  Run `esum flyertalk thread --id 2213102 --from 31 --to 52` to read the posts left out" in out.splitlines()
    code, out, err = cli("flyertalk", "thread", "--id", "2213102", "--from", "1", "--to", "10")
    assert code == 0, err
    assert "  Run `esum flyertalk thread --id 2213102 --from 11 --to 82` to read the posts after these" in out.splitlines()


def test_thread_by_post_says_holds_only_when_the_post_is_on_the_page(ft, cli):
    final = f"{FORUM}/hyatt-world-hyatt/1307256-park-hyatt-paris-vend-me-review-master-thread-aug-2011-onward-342.html"
    ft.routes[showthread(p=37981934)] = page(final, "thread_by_post.html")
    code, out, err = cli("flyertalk", "thread", "--id", f"{final[:-9]}-post37981934.html")
    assert code == 0, err
    assert "page 342 (holds post 37981934)" in out and "not found" not in out


def rss(url: str, name: str) -> Response:
    body = fixture(name).encode("cp1252", errors="xmlcharrefreplace")
    return Response(200, url, {"content-type": "text/xml; charset=ISO-8859-1"}, body)


def test_recent_site_wide(ft, cli):
    ft.routes[f"{FORUM}/external.php?type=RSS2"] = rss(f"{FORUM}/external.php?type=RSS2", "rss.xml")
    code, out, err = cli("flyertalk", "recent")
    assert code == 0, err
    lines = out.splitlines()
    assert "forum: all forums" in lines and "count: 3" in lines
    assert "threads[3]{id,title,forum,started}:" in out
    assert '  2226141,Stunned,American Airlines | AAdvantage,"2026-11-08 01:40"' in lines
    assert "  Run `esum flyertalk thread --id <id>` to read a thread" in lines
    assert len(ft.requests) == 1


def test_recent_in_a_forum_by_slug(ft, cli):
    url = f"{FORUM}/external.php?type=RSS2&forumids=681%2C599"
    ft.routes[url] = rss(url, "rss.xml")
    code, out, err = cli("flyertalk", "recent", "--forum", "united-airlines-mileageplus", "599",
                         "--fields", "starter,text")
    assert code == 0, err
    assert "{id,title,forum,started,starter,text}:" in out and "bcj1949" in out
    assert "forum --id" not in out   # paging back by start date needs a single forum
    assert "find a sub-forum" not in out


def test_recent_empty_parent_forum_names_sub_forums(ft, cli):
    url = f"{FORUM}/external.php?type=RSS2&forumids=374"
    ft.routes[url] = rss(url, "rss_empty.xml")
    code, out, err = cli("flyertalk", "recent", "--forum", "374")
    assert code == 0, err
    assert "leaves out sub-forums" in out
    assert "Run `esum flyertalk forums` to find a sub-forum" in out


def test_recent_for_an_unknown_forum_id_is_no_such_forum(ft, cli):
    url = f"{FORUM}/external.php?type=RSS2&forumids=99999999"
    ft.routes[url] = Response(200, url, {"content-type": "text/xml; charset=ISO-8859-1"}, b"")
    code, out, err = cli("flyertalk", "recent", "--forum", "99999999")
    assert code == 1, err
    error, help_ = out.splitlines()[:2]
    assert error.startswith("error: ") and "99999999" in error
    assert "RuntimeError" not in error and "not XML" not in error
    assert "esum flyertalk forums" in help_


PAGE_404 = '<!DOCTYPE html><html><head><script type="text/javascript" src="x.js"></script></head><body>gone</body></html>'


@pytest.mark.parametrize("args, url, help_has", [
    (("thread", "--id", "9999999"), showthread(t=9999999), "check the id"),
    (("forum", "--id", "99999"), f"{FORUM}/forumdisplay.php?f=99999", "esum flyertalk forums"),
])
def test_a_404_page_is_a_clean_no_such_item(ft, cli, args, url, help_has):
    ft.routes[url] = Response(404, url, {"content-type": "text/html; charset=ISO-8859-1"}, PAGE_404.encode())
    code, out, err = cli("flyertalk", *args)
    assert code == 1, err
    error, help_ = out.splitlines()[:2]
    assert error.startswith("error: ") and "404" in error
    assert "<" not in error and "HttpError" not in error
    assert help_has in help_


def test_an_unknown_forum_name_says_slugs_work(ft, cli):
    code, out, err = cli("flyertalk", "forum", "--id", "no-such-forum")
    assert code == 2, err
    assert "slug" in out.splitlines()[0] and "esum flyertalk forums" in out
    assert ft.requests == []


def test_recent_with_a_broken_feed_is_still_a_layout_error(ft, cli):
    url = f"{FORUM}/external.php?type=RSS2"
    ft.routes[url] = Response(200, url, {"content-type": "text/xml"}, b"<?xml version='1.0'?><rss><channel>")
    code, out, err = cli("flyertalk", "recent")
    assert code == 1, err
    assert "not XML" in out and "esum-site-author" in out
