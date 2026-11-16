"""vBulletin engine parsers on the engine's fixtures (tests/fixtures/vbulletin/): scrubbed pages
captured from FlyerTalk. A site config's own actions are tested in that site's folder."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from esum import Post
from esum.engines.vbulletin import parse
from esum.transport import Response
from datetime import timedelta
from datetime import timezone
from esum.engines.vbulletin import parse as vb
from types import SimpleNamespace

from support import engine_fixtures

FIX = engine_fixtures("vbulletin")
FORUM = "https://www.flyertalk.com/forum"
THREAD_URL = f"{FORUM}/credit-card-programs/2213102-us-bank-altitude-reserve-card-2026-a.html"
SEARCH_FINAL = f"{FORUM}/search.php?searchid=70127352&query=altitude-reserve"
TODAY = date(2026, 10, 26)


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


# --- pure parsing ------------------------------------------------------------------------------------

def test_vb_date_forms():
    assert parse.vb_date("Jan 13, 2026 | 11:01 am") == datetime(2026, 1, 13, 11, 1)
    assert parse.vb_date("Mar 27, 2024, 7:59 pm") == datetime(2024, 3, 27, 19, 59)
    assert parse.vb_date("Today, 1:12 pm", TODAY) == datetime(2026, 10, 26, 13, 12)
    assert parse.vb_date("Yesterday | 12:05 am", TODAY) == datetime(2026, 10, 25, 0, 5)
    assert parse.vb_date(" on Jan 13, 2026") == date(2026, 1, 13)
    assert parse.vb_date("01-13-2026, 11:01 AM") == datetime(2026, 1, 13, 11, 1)
    assert parse.vb_date("") is None and parse.vb_date("never") is None


def test_thread_ref_forms():
    assert parse.thread_ref("2213102") == (2213102, None, None)
    assert parse.thread_ref(THREAD_URL) == (2213102, None, None)
    assert parse.thread_ref(THREAD_URL.replace("-a.html", "-a-6.html")) == (2213102, 6, None)
    assert parse.thread_ref(f"{FORUM}/showthread.php?t=2213102&page=3") == (2213102, 3, None)
    assert parse.thread_ref(f"{FORUM}/hyatt-world-hyatt/1307256-park-hyatt-paris-vend-me-review-"
                            "master-thread-aug-2011-onward-post37986891.html#post37986891") == (1307256, None, 37986891)
    assert parse.thread_ref(f"{FORUM}/37535722-post2.html") == (None, None, 37535722)
    assert parse.thread_ref(THREAD_URL.replace(".html", "-6.html#post37988887")) == (2213102, 6, 37988887)
    with pytest.raises(ValueError):
        parse.thread_ref("not a thread")


def test_forum_ref_forms():
    assert parse.forum_ref("599") == 599
    assert parse.forum_ref(f"{FORUM}/credit-card-programs-599/") == 599
    assert parse.forum_ref(f"{FORUM}/forumdisplay.php?f=418&page=2") == 418
    assert parse.forum_ref("hyatt-world-hyatt") is None


def test_thread_page_one():
    tp = parse.parse_thread_page(fixture("thread_p1.html"), page=1)
    assert tp.title == "US Bank Altitude Reserve Card (2026)"
    assert tp.url == THREAD_URL
    assert (tp.per_page, tp.total, tp.pages) == (15, 82, 6)
    assert tp.forum == "Credit Card Programs" and not tp.closed and tp.wiki is None
    assert [p.number for p in tp.posts] == [1, 2, 3, 4]
    first, second = tp.posts[0], tp.posts[1]
    assert first.user == "jolapo" and first.is_op and first.likes == 1
    assert first.date == datetime(2026, 1, 13, 10, 24)
    assert first.url == "https://www.flyertalk.com/forum/37535640-post1.html"
    assert not second.is_op
    # the quote box is one "> @user: …" line; the reply text follows
    assert second.text.startswith("> @jolapo: It's been a while and no credit.")
    assert second.text.splitlines()[-1].startswith("Lots of data points")
    assert second.quotes == [37535640]
    posts = parse.to_posts(tp.posts)
    assert posts[1].reply_to == 1 and posts[0].reply_to is None   # #1 quotes a post from another thread


def test_thread_last_page():
    tp = parse.parse_thread_page(fixture("thread_p6.html"), page=6)
    assert (tp.per_page, tp.total, tp.pages) == (15, 82, 6)
    assert [p.number for p in tp.posts] == list(range(76, 83))
    assert tp.posts[-1].user == "iahphx"


def test_page_fetched_by_post_id_knows_its_number():
    html = fixture("thread_by_post.html")
    url = f"{FORUM}/hyatt-world-hyatt/1307256-park-hyatt-paris-vend-me-review-master-thread-aug-2011-onward-342.html"
    assert parse.parse_thread_page(html, url, page=None).page == 342
    tp = parse.parse_thread_page(html, "", page=None)      # no page in the URL: from the post numbers
    assert tp.page == 342 and [p.number for p in tp.posts] == [5116, 5117, 5118]
    assert tp.total == 5132


def test_wikipost_is_post_zero():
    tp = parse.parse_thread_page(fixture("wiki_p1.html"), page=1)
    wiki = tp.wiki
    assert isinstance(wiki, Post) and wiki.number == 0
    assert wiki.user == "Wikipost (last edit: phltraveler)"
    assert wiki.date == datetime(2024, 3, 27, 7, 59)
    assert "Some highlights:" in wiki.text and "\n- Visa and MC will each permit" in wiki.text
    assert [p.number for p in tp.posts] == [1, 2]


def test_forum_page_rows():
    data = parse.parse_forum_page(fixture("forum.html"), FORUM + "/", TODAY)
    assert data["title"] == "Credit Card Programs" and (data["page"], data["pages"]) == (1, 304)
    rows = {r["id"]: r for r in data["threads"]}
    assert list(rows)[0] == 1479651 and rows[1479651]["sticky"]
    r = rows[2213102]
    assert r["title"] == "US Bank Altitude Reserve Card (2026)" and r["url"] == THREAD_URL
    assert (r["replies"], r["views"], r["pages"]) == (81, 16677, 6)
    assert r["starter"] == "jolapo" and r["started"] == date(2026, 1, 13)
    assert r["last_user"] == "iahphx" and r["last_post"] == datetime(2026, 10, 26, 13, 12)
    assert not r["sticky"] and not r["closed"]
    assert any("’" in row["excerpt"] for row in data["threads"])   # curly quotes survive


def test_search_post_hits():
    html = fixture("search_posts.html")
    meta = parse.parse_search_meta(html, SEARCH_FINAL)
    assert meta == {"searchid": 70127352, "total": 989, "first": 1, "page": 1, "pages": 40}
    rows = parse.parse_search_posts(html, FORUM + "/", TODAY)
    assert len(rows) == 4
    r = rows[1]
    assert (r["post_id"], r["thread_id"], r["page"]) == (37988887, 2213102, 6)
    assert r["post_url"] == THREAD_URL.replace(".html", "-6.html#post37988887")
    assert r["user"] == "iahphx" and r["forum"] == "Credit Card Programs" and r["replies"] == 81
    assert r["date"] == datetime(2026, 10, 26, 13, 12)
    assert r["snippet"].startswith("I recommend booking a hotel, and then cancelling.")


def test_search_thread_hits_and_closed_threads():
    rows = parse.parse_thread_rows(fixture("search_threads.html"), FORUM + "/", TODAY)
    assert [r["id"] for r in rows] == [2213102, 2146615, 1834823, 2081580]
    assert [r["closed"] for r in rows] == [False, True, True, False]
    assert {r["forum"] for r in rows} == {"Credit Card Programs"}
    assert rows[2]["pages"] == 152 and rows[2]["replies"] == 2272


def test_error_box():
    assert parse.error_message(fixture("search_none.html")).startswith("Sorry - no matches.")
    assert parse.error_message(fixture("thread_p1.html")) is None


def test_quote_without_author():
    html = ('<div style="margin:1rem"><div><label>Quote:</label></div><div class="panel alt2">'
            'plain quoted words</div></div>my reply')
    text, quotes = parse.message_text(html)
    assert text == "> plain quoted words\nmy reply" and quotes == []


def test_security_token():
    assert parse.security_token(search_form(TOKEN)) == TOKEN
    assert parse.security_token(fixture("search_posts.html")) == "guest"
    assert parse.security_token("<html></html>") == "guest"


@pytest.mark.parametrize("local, offset", [
    (datetime(2026, 3, 8, 1, 59), "-06:00"), (datetime(2026, 3, 8, 3, 0), "-05:00"),
    (datetime(2026, 11, 1, 0, 59), "-05:00"), (datetime(2026, 11, 1, 2, 0), "-06:00"),
    (datetime(2005, 4, 2, 12, 0), "-06:00"), (datetime(2005, 4, 3, 12, 0), "-05:00"),
    (datetime(2005, 10, 29, 12, 0), "-05:00"), (datetime(2005, 10, 31, 12, 0), "-06:00"),
])
def test_us_daylight_saving_follows_the_rules_of_the_dates_year(local, offset):
    assert parse.in_zone(local, "America/Chicago").isoformat().endswith(offset)


def test_in_zone_offsets():
    t = datetime(2026, 1, 13, 10, 24)
    assert parse.in_zone(t, "").tzinfo is None
    assert parse.in_zone(t, "UTC").isoformat() == "2026-01-13T10:24:00+00:00"
    assert parse.in_zone(t, "UTC+5:30").isoformat() == "2026-01-13T10:24:00+05:30"
    assert parse.in_zone(t, "UTC-3:30").isoformat() == "2026-01-13T10:24:00-03:30"
    assert parse.in_zone(date(2026, 1, 13), "UTC-6") == date(2026, 1, 13)


@pytest.mark.parametrize("query, dropped, kept", [
    ("sapphire 5/24", ["5/24"], "sapphire"),
    ('"sapphire 5/24"', ["5/24"], "sapphire"),
    ('"chase sapphire 5/24" bonus', ["5/24"], '"chase sapphire" bonus'),
    ('+"sapphire preferred" -"5/24 rule"', ["5/24"], '+"sapphire preferred" -rule'),
    ('"5/24" CSP', ["5/24"], "CSP"),
    ('"sapphire 5/24', ["5/24"], ""),   # unpaired quote: no retry
    ("5/24", ["5/24"], ""),
])
def test_unindexed_terms_keep_quoted_phrases_well_formed(query, dropped, kept):
    from esum.engines.vbulletin import unindexed_terms
    assert unindexed_terms(query) == (dropped, kept)


# --- recent threads (RSS) ----------------------------------------------------------------------------

def test_parse_rss_items():
    from datetime import timezone
    items = parse.parse_rss(fixture("rss.xml"))
    assert [i["id"] for i in items] == [2226141, 2226139, 2226131]
    first = items[0]
    assert first["title"] == "Stunned" and first["forum"] == "American Airlines | AAdvantage"
    assert first["starter"] == "bcj1949"
    assert first["started"] == datetime(2026, 11, 8, 1, 40, 35, tzinfo=timezone.utc)
    assert first["url"].endswith("/2226141-stunned.html")
    # content:encoded holds the whole first post; description is the feed's shortened copy
    assert first["text"].endswith("thanked the cabin attendant for great service....")
    assert len(first["excerpt"]) <= parse.SNIPPET_CAP + 1
    assert items[1]["title"] == "Error on View Premier Status…?"
    assert parse.parse_rss(fixture("rss_empty.xml")) == []


def test_parse_rss_keeps_plain_text_fields_as_written():
    xml = """<?xml version="1.0"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
<item><title>Fares &lt; $100 &gt; $50</title><link>https://www.flyertalk.com/forum/x/2226141-fares.html</link>
<category>Deals &lt;US&gt; &amp;amp; more</category><dc:creator>a&lt;b&gt;c</dc:creator>
<description>&lt;p&gt;body&lt;/p&gt;</description></item></channel></rss>"""
    [item] = parse.parse_rss(xml)
    assert item["title"] == "Fares < $100 > $50"
    assert item["forum"] == "Deals <US> &amp; more"
    assert item["starter"] == "a<b>c"
    assert item["text"] == "body"


def test_parse_rss_rejects_html():
    with pytest.raises(RuntimeError, match="not XML"):
        parse.parse_rss("<html><body>Just a moment...</body></html>")


# --- What the page says must survive: code, struck text, images, attachments, and honest gaps --------

def pager(*spans: tuple[int, int, int]) -> str:
    return "".join(f'<a title="Showing results {a} to {b} of {c}">x</a>' for a, b, c in spans)


def paged_thread_html(numbers: list[int], spans: str) -> str:
    posts = "".join(
        f'<div id="edit{n}"><a href="p{n}" id="postcount{n}" name="{n}">#{n}</a>'
        f'<div id="post_message_{n}">text {n}</div><!-- / post #{n} -->' for n in numbers)
    return f"<h1>T</h1>{spans}{posts}"


def test_vbulletin_single_page_thread_is_not_one_post_per_page():
    page = vb.parse_thread_page(paged_thread_html(list(range(1, 8)), pager((1, 1, 1))), "u")
    assert page.total == 7 and page.per_page == 7 and page.pages == 1


def test_vbulletin_keeps_a_real_pager():
    page = vb.parse_thread_page(paged_thread_html(list(range(1, 16)), pager((1, 15, 82), (16, 30, 82))), "u")
    assert page.per_page == 15 and page.pages == 6


def test_vbulletin_lists_attachments_outside_the_message():
    html = ('<div id="edit5"><a href="https://f.invalid/forum/t.html#post5" id="postcount5" name="5">#5</a>'
            '<div id="post_message_5">Will this help?</div>'
            '<fieldset><legend>Attached Images</legend><a href="attachment.php?attachmentid=60509&amp;d=1">'
            '<img src="attachment.php?attachmentid=60509&amp;thumb=1"></a></fieldset><!-- / post #5 -->')
    [post] = vb.parse_posts(html)
    assert post.text == "Will this help?\n[attachment: https://f.invalid/forum/attachment.php?attachmentid=60509&d=1]"


# --- V2EX and NodeSeek against their raw pages: nothing unknown shown as zero, nothing silently missing ---

def test_vbulletin_keeps_the_edit_note():
    html = ('<div id="edit5"><a href="p5" id="postcount5" name="5">#5</a><div id="post_message_5">x</div>'
            '<div class="smallfont"><em>Last edited by notquiteaff; Aug 27, 2025 at <span class="time">2:21 pm</span>'
            '</em></div><!-- / post #5 -->')
    assert vb.parse_posts(html)[0].edited == "notquiteaff; Aug 27, 2025 at 2:21 pm"


# --- Regressions for review fixes in text extraction and the parsers (offline) -----------------------

VB = FIX


# --- vBulletin: "Today" is the forum's date ----------------------------------------------------------

FOOTER_HTML = (VB / "footer.html").read_text(encoding="utf-8")


def test_page_today_from_the_footer():
    # 03:12 UTC on Oct 27 is 22:12 on Oct 26 at GMT -5, which the footer shows as 10:12 PM.
    now = datetime(2026, 10, 27, 3, 12, tzinfo=timezone.utc)
    assert vb.page_today(FOOTER_HTML, now=now) == date(2026, 10, 26)
    # The clock includes daylight saving the GMT offset leaves out: 10:12 PM is then 02:12 UTC.
    assert vb.page_today(FOOTER_HTML, now=datetime(2026, 10, 27, 2, 12, tzinfo=timezone.utc)) == date(2026, 10, 26)


def test_page_today_falls_back_to_the_utc_offset_extra():
    now = datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc)
    assert vb.page_today("<p>no footer</p>", -5, now=now) == date(2026, 10, 26)
    assert vb.page_today("<p>no footer</p>", now=now) is None


def test_forum_today_rows_use_the_footer_date():
    html = (VB / "forum.html").read_text(encoding="utf-8") + FOOTER_HTML
    forum_day = vb.page_today(html)
    rows = vb.parse_forum_page(html, "https://www.flyertalk.com/forum/")["threads"]
    days = {r["last_post"].date() for r in rows if isinstance(r["last_post"], datetime)}
    assert forum_day in days


def test_vb_engine_passes_the_utc_offset_extra():
    from esum.engines.vbulletin import page_today
    ctx = SimpleNamespace(site=SimpleNamespace(extras={"utc_offset": 14}))
    assert page_today(ctx, "<p>no footer</p>") == (datetime.now(timezone.utc) + timedelta(hours=14)).date()


# --- the edit note (dogfooding) --------------------------------------------------------------------------------

def vb_post(note: str, cut: str) -> str:
    """A post whose edit note is followed by the status-icon box, padded so that 400 characters after
    "Last edited by" (the old fixed window) end inside `cut` in that box, as on FlyerTalk's pages."""
    head = ('<div id="edit1"><a href="showpost.php?p=1" id="postcount1" name="1">1</a>'
            '<div id="post_message_1">Body</div><div class="smallfont"><hr /><em>')
    tail = ('</em></div></div><div class="trow"><div class="tcell alt2">'
            '<img class="inlineimg" src="https://www.flyertalk.com/forum/images/statusicon/user_offline.gif" '
            'alt="Dr Jabadski is offline" /> &nbsp; </div></div><!-- / post #1 -->')
    pad = 400 - len(note) - tail.index(cut) - len(cut) // 2
    assert pad >= 0
    return head + note + " " * pad + tail


def test_vbulletin_edit_note_drops_tags_entities_and_the_status_icon():
    [post] = vb.parse_posts(vb_post('Last edited by Dr Jabadski; Sep 1, 2026 at <span class="time">10:49 pm</span>'
                                    '<br />Reason: typo(s) correction(s) &amp; more', cut='alt="Dr Jabadski'))
    assert post.edited == "Dr Jabadski; Sep 1, 2026 at 10:49 pm Reason: typo(s) correction(s) & more"


def test_vbulletin_edit_note_without_a_reason_has_no_entity_debris():
    [post] = vb.parse_posts(vb_post('Last edited by RTW1; Mar 1, 2017 at <span class="time">9:52 am</span>',
                                    cut="&nbsp;"))
    assert post.edited == "RTW1; Mar 1, 2017 at 9:52 am"
