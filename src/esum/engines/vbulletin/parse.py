"""Pure parsing for vBulletin 3.x pages (thread pages, forum listings, search results).

vBulletin has no JSON API, so everything here reads the HTML templates: post blocks are
`<div id="editNNN">`, thread rows carry `id="thread_title_NNN"`, search hits are
`<div class="tbox" id="postNNN">`. The functions take decoded HTML and return plain data, so the
tests run on saved pages without a browser.
"""

from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from esum import (ArgumentError, QUOTE_CHAR_CAP, Post, html_to_text, text_of, clip, element_end,
                  element_inner, quote_html)

WIKI_CHAR_CAP = 8000   # the wikipost in medium mode
SNIPPET_CAP = 300

NO_MATCHES = re.compile(r"Sorry\s*-\s*no matches", re.I)
NO_PERMISSION = re.compile(r"not logged in or you do not have permission", re.I)
STALE_TOKEN = re.compile(r"security token|logged in since|could not be processed", re.I)


# --- small helpers ------------------------------------------------------------------------------------


def to_int(value: str | None) -> int | None:
    if value is None:
        return None
    digits = re.sub(r"[^\d]", "", value)
    return int(digits) if digits else None


def error_message(html: str) -> str | None:
    """The text of vBulletin's standard error box ("vBulletin Message"), if the page is one."""
    m = re.search(r"<!-- main error message -->(.*?)<!-- / main error message -->", html, re.S)
    return text_of(m.group(1)) if m else None


# --- dates ---------------------------------------------------------------------------------------------

_DATE = re.compile(
    r"(?P<day>Today|Yesterday|[A-Z][a-z]{2,8}\.? \d{1,2}, \d{4}|\d{1,2}-\d{1,2}-\d{4}|\d{4}-\d{1,2}-\d{1,2})"
    r"(?:[\s,|]*(?:<[^>]+>\s*)*(?P<time>\d{1,2}:\d{2}\s*[AaPp][Mm]|\d{1,2}:\d{2}))?")


def vb_date(value: str | None, today: date | None = None, zone: str = "") -> datetime | date | None:
    """`Jan 13, 2026 | 11:01 am`, `Today, 1:12 pm`, `Mar 27, 2024, 7:59 am`, `01-13-2026, 11:01 AM`
    -> a datetime with the supplied display zone (naive if unknown), or a date without a time."""
    if not value:
        return None
    m = _DATE.search(value)
    if not m:
        return None
    day_s = m.group("day")
    today = today or date.today()
    if day_s == "Today":
        day = today
    elif day_s == "Yesterday":
        day = today - timedelta(days=1)
    else:
        day = None
        for fmt in ("%b %d, %Y", "%B %d, %Y", "%m-%d-%Y", "%Y-%m-%d"):
            try:
                day = datetime.strptime(day_s.replace(".", ""), fmt).date()
                break
            except ValueError:
                continue
        if day is None:
            return None
    if not m.group("time"):
        return day
    return in_zone(datetime.combine(day, clock_of(m.group("time"))), zone)


def _zone(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name) if "/" in name else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def in_zone(value, zone: str):
    """A naive datetime in the forum's display time -> the same time with that zone's offset (as
    `time_zone` names it: "UTC-6", or an IANA zone such as "America/Chicago" with daylight saving by date),
    so `--json` dates carry an explicit offset. Anything else (a date, an aware datetime, an unknown
    zone) is returned as it is."""
    if not zone or not isinstance(value, datetime) or value.tzinfo is not None:
        return value
    if tz := _zone(zone):
        return value.replace(tzinfo=tz)
    hours, _, minutes = (zone.removeprefix("UTC") or "0").partition(":")
    sign = -1 if hours.startswith("-") else 1
    return value.replace(tzinfo=timezone(timedelta(hours=float(hours), minutes=sign * int(minutes or 0))))


def clock_of(value: str) -> time:
    t = value.replace(" ", "").upper()
    return datetime.strptime(t, "%I:%M%p" if t.endswith("M") else "%H:%M").time()


_FOOTER = re.compile(r"All times are GMT\s*(?P<offset>[+-]?\s*\d{1,2}(?:[.:]\d{1,2})?)?\.?\s*"
                     r"The time now is\s*(?:<[^>]+>\s*)*(?P<time>\d{1,2}:\d{2}\s*[AaPp][Mm]|\d{1,2}:\d{2})", re.I)


def time_zone(html: str, site_zone: str = "") -> str:
    """The zone dates on a page are in, from its footer ("All times are GMT -6"), or "" if unknown.
    `site_zone` is an IANA zone (e.g. "America/Chicago") a forum shows each date in, daylight saving
    by date; it is named when the footer's offset is one that zone uses this year (else the visitor
    has a time zone setting of their own, and the footer's offset is named)."""
    m = _FOOTER.search(html or "")
    if not m:
        return ""
    offset = (m.group("offset") or "").replace(" ", "")
    if (tz := _zone(site_zone)) and offset.lstrip("+-").isdigit():
        year = datetime.now(tz).year
        used = {datetime(year, month, 1, tzinfo=tz).utcoffset() for month in (1, 7)} | {datetime.now(tz).utcoffset()}
        if timedelta(hours=int(offset)) in used:
            return site_zone
    return f"UTC{offset}" if offset.strip("+-0") else "UTC"


def page_today(html: str, utc_offset: float | None = None, now: datetime | None = None) -> date | None:
    """The forum's current date, which "Today" and "Yesterday" mean on this page: from the footer
    ("All times are GMT -5. The time now is 10:12 PM"), else from `utc_offset` (hours), else None."""
    now = now or datetime.now(timezone.utc)
    m = _FOOTER.search(html or "")
    if m:
        off = (m.group("offset") or "0").replace(" ", "")
        hours, _, minutes = off.partition(":")
        offset = float(hours) + (-1 if hours.startswith("-") else 1) * int(minutes or 0) / 60
        local = (now + timedelta(hours=offset)).replace(tzinfo=None)
        clock = clock_of(m.group("time"))
        # The clock shown includes daylight saving, which the GMT offset may leave out: take the
        # day that puts the shown clock nearest the computed local time.
        return min((local.date() + timedelta(days=d) for d in (-1, 0, 1)),
                   key=lambda d: abs(datetime.combine(d, clock) - local))
    if utc_offset is not None:
        return (now + timedelta(hours=utc_offset)).date()
    return None


# --- ids from URLs -------------------------------------------------------------------------------------

def thread_ref(value: str) -> tuple[int | None, int | None, int | None]:
    """A thread id or a thread / post URL -> (thread id, page, post id), each None when absent.

    Accepts `2213102`, `showthread.php?t=2213102&page=3`, `showthread.php?p=37535722`, FlyerTalk
    SEO thread URLs `/forum/<forum>/2213102-<title>-3.html` (vBSEO appends `-a` to titles ending in
    a number, so a trailing `-N` is always the page), and post URLs `…/2213102-<title>-post37535722.html`,
    `/forum/37535722-post2.html` or `…#post37535722`."""
    value = value.strip()
    if value.isdigit():
        return int(value), None, None
    parts = urlsplit(value)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    if m := re.search(r"/(\d+)-post\d+\.html$", parts.path):   # /forum/<post id>-post<number>.html
        return None, None, int(m.group(1))
    post = re.search(r"-post(\d+)\.html$", parts.path) or re.search(r"^post(\d+)$", parts.fragment)
    post_id = int(post.group(1)) if post else (int(q["p"]) if q.get("p", "").isdigit() else None)
    if q.get("t", "").isdigit():
        return int(q["t"]), int(q["page"]) if q.get("page", "").isdigit() else None, post_id
    if m := re.search(r"/(\d+)-[^/]*?(?:-(\d+))?\.html$", re.sub(r"-post\d+\.html$", ".html", parts.path)):
        return int(m.group(1)), int(m.group(2)) if m.group(2) else None, post_id
    if post_id:
        return None, None, post_id
    raise ArgumentError(f"not a thread id or thread URL: {value!r}",
                        help="Pass the thread number, or the thread URL copied from the site")


def forum_ref(value: str) -> int | None:
    """A forum id or URL (`forumdisplay.php?f=599`, `/forum/credit-card-programs-599/`) -> id, else
    None (the caller may look the value up as a slug)."""
    value = value.strip()
    if value.isdigit():
        return int(value)
    parts = urlsplit(value)
    q = parse_qs(parts.query)
    if "f" in q and q["f"][0].isdigit():
        return int(q["f"][0])
    if m := re.search(r"-(\d+)(?:-\d+\.html)?/?$", parts.path):
        return int(m.group(1))
    return None


# --- thread pages --------------------------------------------------------------------------------------

@dataclass
class RawPost:
    id: int
    number: int
    user: str
    text: str
    date: datetime | date | None = None
    likes: int = 0
    is_op: bool = False
    url: str | None = None
    quotes: list[int] = field(default_factory=list)   # post ids this post quotes
    edited: str | None = None
    message: str = ""           # the message HTML: quotes render once it's known what else is shown
    files: list[str] = field(default_factory=list)


@dataclass
class ThreadPage:
    title: str
    url: str
    page: int
    per_page: int
    total: int                  # highest post number in the thread
    posts: list[RawPost]
    forum: str = ""
    closed: bool = False
    zone: str = ""              # the forum's time zone, e.g. UTC-6
    wiki: Post | None = None    # FlyerTalk wikipost, shown as #0

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page)) if self.per_page else 1


def _quote_box(box: str, shown: dict[int, int]) -> tuple[str, int | None]:
    """One quote box -> (`> @who: text` as a blockquote, quoted post id). The text is cut only when
    the quoted post is in `shown` (post id -> number), where the reader finds the rest."""
    who = re.search(r"Originally Posted by\s*<strong>(.*?)</strong>", box, re.S | re.I)
    ref = re.search(r"#post(\d+)", box)
    post = int(ref.group(1)) if ref else None
    number = shown.get(post)
    body = element_inner(box, r'<div style="font-style:\s*italic">')
    if body is None:
        body = element_inner(box, r'<div class="panel[^"]*"[^>]*>')
        if body is None:
            body = box
        body = re.sub(r"Originally Posted by.*?</strong>(\s*<a [^>]*>.*?</a>)?", "", body, flags=re.S | re.I)
    author = f"@{text_of(who.group(1))}" if who else ""
    return quote_html(author, None, body, cap=QUOTE_CHAR_CAP if number else None,
                      hint=f"; whole in #{number}" if number else "",
                      keep_links=number is None), post


def message_text(msg_html: str, shown: dict[int, int] | None = None) -> tuple[str, list[int]]:
    """A post's message HTML -> (text, quoted post ids). Quote boxes (`Quote: Originally Posted by
    X …`) become `> @X: …` with body structure kept, cut at 200 characters if `shown` holds the quoted post."""
    quotes: list[int] = []
    out, pos = [], 0
    label = re.compile(r"<label>\s*Quote:?\s*</label>", re.I)
    while m := label.search(msg_html, pos):
        # the box is the div that encloses `<div><label>Quote:</label></div>`
        wrap = max(msg_html.rfind("<div", pos, msg_html.rfind("<div", pos, m.start())), pos)
        end = element_end(msg_html, wrap)
        replacement, ref = _quote_box(msg_html[wrap:end], shown or {})
        if ref:
            quotes.append(ref)
        out += [msg_html[pos:wrap], replacement]
        pos = end
    out.append(msg_html[pos:])
    return html_to_text("".join(out)), quotes


def _attachments(block: str, post_url: str | None) -> list[str]:
    """Files listed under a post, outside its message: often the point of the post."""
    found: dict[str, str] = {}
    for box in re.findall(r"<fieldset[^>]*>\s*<legend>\s*Attach.*?</fieldset>", block, re.S | re.I):
        for link in re.findall(r'(?:href|src)="([^"]*attachmentid=(\d+)[^"]*)"', box):
            found.setdefault(link[1], urljoin(post_url or "", htmllib.unescape(link[0])))
    return list(found.values())


def _with_files(text: str, files: list[str]) -> str:
    return "\n".join([text, *(f"[attachment: {u}]" for u in files)]).strip()


def _post_blocks(html: str):
    starts = list(re.finditer(r'<div id="edit(\d+)">', html))
    for i, m in enumerate(starts):
        pid = m.group(1)
        stop = html.find(f"<!-- / post #{pid} -->", m.end())
        nxt = starts[i + 1].start() if i + 1 < len(starts) else len(html)
        yield int(pid), html[m.end(): stop if 0 <= stop < nxt else nxt]


def parse_posts(html: str, today: date | None = None) -> list[RawPost]:
    """The posts on one thread page, in page order (deduplicated by post id)."""
    out, seen = [], set()
    for pid, block in _post_blocks(html):
        if pid in seen:
            continue
        count = re.search(rf'<a href="([^"]*)"[^>]*id="postcount{pid}"[^>]*name="(\d+)"', block)
        if not count:
            count = re.search(rf'id="postcount{pid}"[^>]*name="(\d+)"', block)
            href, number = None, int(count.group(1)) if count else 0
        else:
            href, number = htmllib.unescape(count.group(1)), int(count.group(2))
        msg = element_inner(block, rf'<div id="post_message_{pid}">')
        if not number or msg is None:
            continue
        seen.add(pid)
        head = re.search(r'<div class="trow thead[^"]*">(.*?)<a href="[^"]*"[^>]*id="postcount', block, re.S)
        head_text = re.sub(r"<!--.*?-->", " ", head.group(1), flags=re.S) if head else ""
        user = re.search(r'class="bigusername"[^>]*>(.*?)</a>', block, re.S) \
            or re.search(r'<span class="bigusername">(.*?)</span>', block, re.S)
        likes = re.search(rf'id="post_thanks_button_likes_{pid}">\s*(\d+)', block)
        text, quotes = message_text(msg)
        # The note's own box ("Last edited by <who>; <date> at <time>", maybe a reason), read as HTML
        # up to its </div>: the status icon after the box is not part of it.
        note = block.find("Last edited by")
        edited = re.search(r"Last edited by\s+(.*?)(?:</div>|$)", block[note:note + 2000], re.S) if note >= 0 else None
        files = _attachments(block, href)
        out.append(RawPost(
            id=pid, number=number, user=text_of(user.group(1)) if user else "?", text=_with_files(text, files),
            date=vb_date(text_of(head_text), today), likes=int(likes.group(1)) if likes else 0,
            is_op=bool(re.search(r'class="[^"]*threadstarter\b', block)),
            url=href, quotes=quotes, edited=text_of(edited.group(1)) if edited else None, message=msg, files=files))
    return out


def parse_wiki(html: str, today: date | None = None) -> Post | None:
    """FlyerTalk's wikipost (a community-edited summary above post #1) as post #0."""
    if '<div class="tbox wikipost">' not in html:
        return None
    body = element_inner(html, r'<div id="wikipost-\d+">')
    if not body:
        return None
    when = re.search(r'<a name="wikipost">.*?</a>(.*?)</div>', html, re.S)
    editor = re.search(r"Last edit by:\s*<a[^>]*>(.*?)</a>", html, re.S)
    text = html_to_text(body)
    if not text:
        return None
    user = "Wikipost" + (f" (last edit: {text_of(editor.group(1))})" if editor else "")
    return Post(number=0, user=user, text=text, date=vb_date(text_of(when.group(1)), today) if when else None)


def parse_thread_page(html: str, url: str = "", page: int | None = 1, today: date | None = None,
                      zone: str | None = None) -> ThreadPage:
    """One thread page. `page=None` (a page fetched by post id) reads the page number from `url`
    or from the post numbers."""
    today = today or page_today(html)
    posts = parse_posts(html, today)
    title = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    title_s = text_of(title.group(1)) if title else ""
    if not title_s and (t := re.search(r"<title>(.*?)</title>", html, re.S)):
        title_s = re.sub(r"\s+-\s+[^-]*Forums?\s*$", "", text_of(t.group(1)))
    # "Showing results 16 to 30 of 82" (the pager's titles) -> total and posts per page
    shown = [tuple(map(to_int, m)) for m in
             re.findall(r"results\s+([\d,]+)\s+to\s+([\d,]+)\s+of\s+([\d,]+)", html, re.I)]
    total = max((t for _, _, t in shown), default=0)
    firsts = [b for a, b, _ in shown if a == 1]
    numbers = [p.number for p in posts]
    # a single-page thread has only a hidden "results 1 to 1 of 1": a page holds at least what it shows
    per_page = max([*firsts, len(posts), 1])
    total = max([total, *numbers]) if numbers else total
    crumbs = re.findall(r'itemprop="item"[^>]*>\s*<span itemprop="name">(.*?)</span>', html, re.S)
    canonical = re.search(r'<link rel="canonical" href="([^"]+)"', html)
    closed = bool(re.search(r'threadclosed|alt="Closed Thread"', html))
    if page is None:
        try:
            page = thread_ref(url)[1]
        except ValueError:
            page = None
        if page is None:
            page = (min(numbers) - 1) // per_page + 1 if numbers else 1
    return ThreadPage(title=title_s, url=htmllib.unescape(canonical.group(1)) if canonical else url,
                      page=page, per_page=per_page, total=total, posts=posts,
                      forum=text_of(crumbs[-1]) if len(crumbs) > 1 else "", closed=closed,
                      zone=time_zone(html) if zone is None else zone,
                      wiki=parse_wiki(html, today) if page == 1 else None)


def to_posts(raw: list[RawPost], read: list[RawPost] | None = None) -> list[Post]:
    """RawPosts -> Posts, with `↩#M` for the first quoted post whose number is known, else its id.
    `read` is everything fetched, which can be more than `raw` when a range ends mid-page."""
    number_of = {p.id: p.number for p in read or raw}
    shown = {p.id: p.number for p in raw}
    out = []
    for p in raw:
        reply = next((number_of[q] for q in p.quotes if q in number_of and number_of[q] < p.number), None)
        elsewhere = next((q for q in p.quotes if q not in number_of), None)
        out.append(Post(number=p.number, user=p.user, text=_with_files(message_text(p.message, shown)[0], p.files),
                        date=p.date, likes=p.likes, is_op=p.is_op, reply_to=reply,
                        reply_to_id=None if reply else elsewhere, url=p.url, edited=p.edited))
    return out


# --- thread listings (forum pages, thread search) ------------------------------------------------------

def _rows(html: str, start_re: str):
    starts = list(re.finditer(start_re, html))
    for i, m in enumerate(starts):
        nxt = starts[i + 1].start() if i + 1 < len(starts) else len(html)
        yield m, html[m.start(): nxt]


def parse_thread_rows(html: str, base_url: str = "", today: date | None = None,
                      zone: str | None = None) -> list[dict]:
    """Thread rows of a forum page or a thread search ("threadbits")."""
    today = today or page_today(html)
    zone = time_zone(html) if zone is None else zone
    out = []
    for m, row in _rows(html, r'id="td_threadstatusicon_(\d+)"'):
        tid = int(m.group(1))
        # the row ends where the next row (or the list) does; keep only this row's cells
        stop = row.find('<div class="trow', 10)
        row = row[: stop if stop > 0 else len(row)]
        title = re.search(rf'<a href="([^"]*)" id="thread_title_{tid}"[^>]*>(.*?)</a>', row, re.S)
        if not title:
            continue
        stats = re.search(r'title="Replies: ([\d,]+), Views: ([\d,]+)"', row)
        starter = re.search(r"<span[^>]*onclick=\"window\.open\('[^']*members/[^']*'[^>]*>(.*?)</span>", row, re.S) \
            or re.search(r'<div class="smallfont">\s*(?:<a[^>]*>)?([^<]+?)(?:</a>)?\s*<span class="smallfont', row, re.S)
        started = re.search(r'jumbo-txt">\s*on\s+([^<]+)<', row)
        last = re.search(r'<div class="smallfont text-right">(.*?)<br\s*/?>\s*by\s*<a[^>]*>(.*?)</a>', row, re.S)
        pages = [int(n) for n in re.findall(rf'/{tid}-[^"/]*?-(\d+)\.html', row)]
        excerpt = re.search(rf'id="td_threadtitle_{tid}" title="([^"]*)"', row)
        forum = re.search(r'<div class="tcell alt1 text-left"><a href="[^"]*">(.*?)</a></div>', row)
        status = re.search(rf'id="thread_statusicon_{tid}"', row)
        icon = re.search(r'src="[^"]*statusicon/([\w]+)\.gif"', row)
        moved = bool(icon and icon.group(1).startswith("thread_moved"))
        target = re.search(r"/(\d+)-[^/]*$", title.group(1)) if moved else None
        prefix = (f"Moved to {target.group(1)}: " if target else "Moved: ") if moved else ""
        out.append({
            "id": tid,
            "title": prefix + text_of(title.group(2)),
            "url": urljoin(base_url, htmllib.unescape(title.group(1)).split("?")[0]),
            "starter": text_of(starter.group(1)) if starter else "",
            "started": vb_date(started.group(1), today, zone) if started else None,
            "replies": to_int(stats.group(1)) if stats else None,
            "views": to_int(stats.group(2)) if stats else None,
            "last_post": vb_date(text_of(last.group(1)), today, zone) if last else None,
            "last_user": text_of(last.group(2)) if last else "",
            "pages": max(pages, default=1),
            "sticky": "Sticky Thread" in row,
            "closed": bool(status and icon and icon.group(1).endswith("_lock")),
            "moved_to": int(target.group(1)) if target else None,
            "forum": text_of(forum.group(1)) if forum else "",
            "excerpt": clip(" ".join(htmllib.unescape(excerpt.group(1)).split()), SNIPPET_CAP) if excerpt else "",
        })
    return out


def parse_page_count(html: str) -> tuple[int, int]:
    """`Page 2 of 304` -> (2, 304); (1, 1) when there is no pager."""
    m = re.search(r"Page\s+(\d+)\s+of\s+([\d,]+)", html)
    return (int(m.group(1)), to_int(m.group(2)) or 1) if m else (1, 1)


def parse_forum_page(html: str, base_url: str = "", today: date | None = None, zone: str | None = None) -> dict:
    title = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    page, pages = parse_page_count(html)
    canonical = re.search(r'<link rel="canonical" href="([^"]+)"', html)
    return {"title": text_of(title.group(1)) if title else "", "page": page, "pages": pages,
            "url": htmllib.unescape(canonical.group(1)) if canonical else "",
            "threads": parse_thread_rows(html, base_url, today, zone)}


# --- post search ---------------------------------------------------------------------------------------

def security_token(html: str) -> str:
    """The session's form token on a page: a member's own token, or "guest"."""
    m = re.search(r'name="securitytoken"\s+value="([^"]*)"', html) or         re.search(r'SECURITYTOKEN\s*=\s*"([^"]*)"', html)
    return m.group(1) if m and m.group(1) else "guest"


def refresh_searchid(html: str) -> int | None:
    """The searchid a "please wait while we transfer you" page refreshes to, if the page is one."""
    m = re.search(r'<meta[^>]+http-equiv="refresh"[^>]*searchid=(\d+)', html, re.I)
    return int(m.group(1)) if m else None


def parse_search_meta(html: str, final_url: str = "") -> dict:
    """searchid, total hits, page, pages of a search result page."""
    q = parse_qs(urlsplit(final_url).query)
    sid = re.search(r"searchid=(\d+)", final_url) or re.search(r"search\.php\?searchid=(\d+)", html)
    shown = re.search(r"Showing results\s+([\d,]+)\s+to\s+([\d,]+)\s+of\s+([\d,]+)", html)
    page, pages = parse_page_count(html)
    return {"searchid": int(sid.group(1)) if sid else None,
            "total": to_int(shown.group(3)) if shown else 0,
            "first": to_int(shown.group(1)) if shown else 0,
            "page": int(q["page"][0]) if q.get("page", [""])[0].isdigit() else page,
            "pages": pages}


def parse_search_posts(html: str, base_url: str = "", today: date | None = None,
                       zone: str | None = None) -> list[dict]:
    """Hits of a post search (`showposts=1`): one row per matching post."""
    today = today or page_today(html)
    zone = time_zone(html) if zone is None else zone
    out = []
    for m, block in _rows(html, r'<div class="tbox" id="post(\d+)">'):
        pid = int(m.group(1))
        stop = block.find('<div class="tbox"', 10)
        block = block[: stop if stop > 0 else len(block)]
        when = re.search(r'<span style="vertical-align: middle;">(.*?)</span>', block, re.S)
        forum = re.search(r"Forum:\s*<a[^>]*>(.*?)</a>", block, re.S)
        thread = re.search(r'<a href="([^"]*?/(\d+)-[^"]*?)"[^>]*>\s*<strong>(.*?)</strong>', block, re.S)
        if not thread:
            continue
        replies = re.search(r"Replies:\s*<strong>([\d,]+)</strong>", block)
        views = re.search(r"Views:\s*<strong>([\d,]+)</strong>", block)
        user = re.search(r"Posted By\s*<a[^>]*>(.*?)</a>", block, re.S)
        panel = re.search(r'<div class="panel[^"]*"[^>]*>\s*<em>(.*?)</em>', block, re.S)
        post_link = re.search(r'<a href="([^"]*#post\d+)"[^>]*>(.*?)</a>', panel.group(1), re.S) if panel else None
        snippet = ""
        if panel:
            body = panel.group(1)
            if post_link:   # the link repeats the post's first words; the full snippet follows it
                body = body.replace(post_link.group(0), "", 1)
            snippet = clip(" ".join(text_of(body).split()), SNIPPET_CAP)
        post_url = urljoin(base_url, htmllib.unescape(post_link.group(1))) if post_link else ""
        post_url = re.sub(r"\?highlight=[^#]*", "", post_url)
        page = re.search(r"-(\d+)\.html$", post_url.split("#")[0])
        out.append({
            "post_id": pid,
            "thread_id": int(thread.group(2)),
            "title": text_of(thread.group(3)),
            "thread_url": urljoin(base_url, htmllib.unescape(thread.group(1))).split("?")[0],
            "post_url": post_url,
            "page": int(page.group(1)) if page else (None if "-post" in post_url else 1),
            "user": text_of(user.group(1)) if user else "",
            "date": vb_date(text_of(when.group(1)), today, zone) if when else None,
            "forum": text_of(forum.group(1)) if forum else "",
            "replies": to_int(replies.group(1)) if replies else None,
            "views": to_int(views.group(1)) if views else None,
            "snippet": snippet,
        })
    return out


# --- recent threads (RSS) ------------------------------------------------------------------------------

_RSS_NS = {"content": "http://purl.org/rss/1.0/modules/content/", "dc": "http://purl.org/dc/elements/1.1/"}


def parse_rss(xml: str) -> list[dict]:
    """Items of vBulletin's `external.php?type=RSS2` feed: one per thread, newest started first.

    `pubDate` is the thread's start date in GMT; `content:encoded` (else `description`) holds the
    first post, which the feed may shorten."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    head = " ".join(text_of(xml[:200]).split()) or "(empty)"
    try:
        root = ET.fromstring(xml.lstrip("﻿").strip())
    except ET.ParseError as error:
        raise RuntimeError(f"the RSS feed is not XML ({error}): {head!r}") from error
    if root.tag != "rss":
        raise RuntimeError(f"the RSS feed is not XML RSS (root <{root.tag}>): {head!r}")
    out = []
    for item in root.iter("item"):
        def field_(tag: str) -> str:
            node = item.find(tag, _RSS_NS)
            return (node.text or "").strip() if node is not None else ""
        link = field_("link")
        try:
            tid = thread_ref(link)[0] if link else None
        except ArgumentError:
            tid = None
        try:
            started = parsedate_to_datetime(field_("pubDate")) if field_("pubDate") else None
        except (TypeError, ValueError):
            started = None
        body = field_("content:encoded") or field_("description")
        text = html_to_text(body, keep_links=True) if body else ""
        out.append({
            "id": tid,
            "title": " ".join(field_("title").split()),
            "url": link,
            "forum": " ".join(field_("category").split()),
            "starter": " ".join(field_("dc:creator").split()),
            "started": started,
            "excerpt": clip(" ".join(text.split()), SNIPPET_CAP),
            "text": text,
        })
    return out
