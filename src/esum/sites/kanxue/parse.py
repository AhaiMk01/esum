"""Pure parsers for 看雪 Kanxue (bbs.kanxue.com), a heavily customized Xiuno BBS.

There is no JSON API for guests (Xiuno's `?ajax=1` returns an empty body), so everything here reads
the HTML: board cards on the home page, `<tr class="thread" data-tid>` rows on a board page,
`<tr class="post" data-pid>` rows on a thread page (the opening post sits above them in its own
card), and the hot-thread table. Thread pages hold 25 floors each, the opening post (floor 1)
included. Times are the site's display time (China, UTC+8); recent ones are relative ("3小时前")
and are resolved against the current time there.
"""

from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from esum import (ArgumentError, Post, html_to_text, text_of, element_end, element_inner, remove_elements,
                  quote_html)

BASE = "https://bbs.kanxue.com/"
PER_PAGE = 25
CHINA = timezone(timedelta(hours=8))
DIGEST = {1: "关注", 2: "优秀", 3: "精华"}
TRUNCATED_NOTE = ("[hidden: the rest of this post is shown only to logged-in members who reply to or like "
                  "the thread; ESUM reads as a guest and never replies or likes]")
LINK_HIDDEN = "[link hidden from guests]"


# --- small helpers ------------------------------------------------------------------------------------


def to_int(value: str | None, default: int = 0) -> int:
    digits = re.sub(r"[^\d]", "", value or "")
    return int(digits) if digits else default


def message_of(html: str) -> str | None:
    """The site's message box ("请先登录", "主题不存在！", "您所在的用户组无权访问该版块"), if the page is one."""
    m = re.search(r'<i class="icon-warning-sign"></i>(.*?)</h4>', html, re.S)
    return text_of(m.group(1)) if m else None


LOGIN_MESSAGES = re.compile(r"请先登录|无权访问|权限|登录后")
LOGGED_OUT_MESSAGES = re.compile(r"请先登录|登录后")  # logging in fixes these


# --- dates ---------------------------------------------------------------------------------------------

_ABS = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?")
_REL = re.compile(r"(\d+)\s*(秒|分钟|小时|天|周|个月|月)前")


def china_now() -> datetime:
    return datetime.now(CHINA).replace(tzinfo=None)


def kx_date(value: str | None, now: datetime | None = None) -> datetime | date | None:
    """`2026-9-23 11:52`, `2024-1-3`, `8分钟前`, `19小时前`, `3天前`, `刚刚` -> a naive datetime in the
    site's time (UTC+8), or a date when only the day is known (`N天前` and older)."""
    if not value:
        return None
    value = value.strip()
    if m := _ABS.search(value):
        y, mo, d, h, mi = m.groups()
        try:
            if h is None:
                return date(int(y), int(mo), int(d))
            return datetime(int(y), int(mo), int(d), int(h), int(mi))
        except ValueError:
            return None
    now = now or china_now()
    if "刚刚" in value:
        return now.replace(second=0, microsecond=0)
    if m := _REL.search(value):
        n, unit = int(m.group(1)), m.group(2)
        if unit in ("秒", "分钟", "小时"):
            delta = {"秒": timedelta(seconds=n), "分钟": timedelta(minutes=n), "小时": timedelta(hours=n)}[unit]
            return (now - delta).replace(second=0, microsecond=0)
        days = {"天": n, "周": 7 * n}.get(unit, 30 * n)
        return (now - timedelta(days=days)).date()
    if m := re.search(r"(昨天|前天)\s*(\d{1,2}):(\d{2})", value):
        day = now.date() - timedelta(days=1 if m.group(1) == "昨天" else 2)
        return datetime(day.year, day.month, day.day, int(m.group(2)), int(m.group(3)))
    return None


def iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value else None


# --- ids from URLs -------------------------------------------------------------------------------------

def thread_ref(value: str) -> int:
    """`293027`, `thread-293027.htm`, `thread-293027-3.htm`, a full thread URL -> 293027."""
    value = str(value).strip()
    if value.isdigit():
        return int(value)
    m = re.search(r"thread-(\d+)(?:-\d+)*\.htm", value)
    if not m:
        raise ArgumentError(f"{value!r} is not a kanxue thread id or URL",
                            help="Pass the thread number, or a thread-<id>.htm URL")
    return int(m.group(1))


def forum_ref(value: str) -> int | None:
    """`4`, `forum-4.htm`, `forum-4-2.htm?orderby=tid`, a full board URL -> 4."""
    value = str(value).strip()
    if value.isdigit():
        return int(value)
    m = re.search(r"forum-(\d+)(?:-[\d_]*)*\.htm", value)
    return int(m.group(1)) if m else None


def page_count(html: str, prefix: str) -> int:
    """The last page number in the pagination links `<prefix>-N.htm`, else 1."""
    nums = [int(n) for n in re.findall(rf'href="{re.escape(prefix)}-(\d+)\.htm[^"]*" class="page-link"', html)]
    return max(nums, default=1)


# --- board catalog (home page) -------------------------------------------------------------------------

def parse_forums(html: str) -> list[dict]:
    """Board cards on the home page: [{id, name, category, topics, description, parent}], sub-boards
    (listed inside a card's description) right after their parent."""
    out: list[dict] = []
    heads = list(re.finditer(r'<div class="card-header">(.*?)</div>', html, re.S))
    for i, head in enumerate(heads):
        section = html[head.end(): heads[i + 1].start() if i + 1 < len(heads) else len(html)]
        category = text_of(head.group(1))
        cards = list(re.finditer(r'<div class="col-md-6 cate\b', section))
        for j, card in enumerate(cards):
            chunk = section[card.start(): cards[j + 1].start() if j + 1 < len(cards) else len(section)]
            m = re.search(r'class="font-weight-bold"\s+href="forum-(\d+)\.htm">(.*?)</a>', chunk, re.S)
            if not m:
                continue
            fid = int(m.group(1))
            topics = re.search(r"主题：\s*(\d+)", chunk)
            desc_html = element_inner(chunk, r'<div class="mt-2 text-small" style="line-height: 1\.5[^"]*">') or ""
            subs = re.findall(r'<a href="forum-(\d+)\.htm"\s*><b>(.*?)</b></a>', desc_html, re.S)
            desc = re.sub(r'<img[^>]*>\s*<a href="forum-\d+\.htm"\s*><b>.*?</b></a>', " ", desc_html, flags=re.S)
            out.append({"id": fid, "name": text_of(m.group(2)), "category": category,
                        "topics": int(topics.group(1)) if topics else None,
                        "description": text_of(desc), "parent": None})
            out += [{"id": int(sid), "name": text_of(sname), "category": category, "topics": None,
                     "description": "", "parent": fid} for sid, sname in subs]
    return out


# --- board thread list ---------------------------------------------------------------------------------

def parse_forum_page(html: str, now: datetime | None = None) -> dict:
    """A board page (`forum-<id>-<page>.htm`): its name, thread count, pages and thread rows."""
    name = re.search(r'<span style="font-size: 15px;" class="font-weight-bold">(.*?)</span>', html, re.S)
    total = re.search(r"版块主题：</span><b>\s*(\d+)", html)
    active = re.search(r'<li class="page-item active"><a href="forum-\d+-(\d+)\.htm', html)
    rows = [parse_thread_row(chunk, now) for chunk in re.split(r'(?=<tr class="thread\b)', html)[1:]]
    fid = re.search(r'href="forum-(\d+)-\d+\.htm\?', html)
    return {"id": int(fid.group(1)) if fid else None, "name": text_of(name.group(1)) if name else None,
            "topics": int(total.group(1)) if total else None,
            "page": int(active.group(1)) if active else 1,
            "pages": page_count(html, f"forum-{fid.group(1)}") if fid else 1,
            "threads": [r for r in rows if r]}


def parse_thread_row(chunk: str, now: datetime | None = None) -> dict | None:
    chunk = chunk[: chunk.find("</tr>") + 5] if "</tr>" in chunk else chunk
    tid = re.search(r'data-tid="(\d+)"', chunk)
    title = re.search(r'<a href="thread-\d+\.htm" style="vertical-align: middle;">(.*?)</a>', chunk, re.S)
    if not tid or not title:
        return None
    cls = re.match(r'<tr class="([^"]*)"', chunk)
    digest = re.search(r'icon-digest-(\d)\b', chunk)
    author = re.search(r'<a href="user-home-\d+\.htm"[^>]*class="username[^"]*">(.*?)</a>', chunk, re.S)
    started = re.search(r'<span class="date[^"]*"[^>]*>(.*?)</span>', chunk, re.S)
    last = re.search(r'icon-long-arrow-left.*?class="username"[^>]*>@?(.*?)</a>\s*<span>(.*?)</span>', chunk, re.S)
    counts = re.findall(r'<span style="vertical-align: middle;">\s*(\d+)\s*</span>', chunk)
    last_page = re.search(r'href="thread-\d+-(\d+)\.htm" title="最后一页"', chunk)
    level = int(digest.group(1)) if digest else 0
    return {
        "id": int(tid.group(1)), "title": text_of(title.group(1)),
        "author": text_of(author.group(1)) if author else None,
        "date": iso(kx_date(text_of(started.group(1)).lstrip("・"), now)) if started else None,
        "views": int(counts[0]) if counts else None, "replies": int(counts[1]) if len(counts) > 1 else None,
        "last_user": text_of(last.group(1)) if last else None,
        "last_date": iso(kx_date(text_of(last.group(2)), now)) if last else None,
        "pages": int(last_page.group(1)) if last_page else 1,
        "sticky": bool(cls and "top_" in cls.group(1)),
        "digest": DIGEST.get(level, str(level)) if level else None,
        "attachment": "icon-paperclip" in chunk,
        "url": f"{BASE}thread-{tid.group(1)}.htm",
    }


# --- hot threads ---------------------------------------------------------------------------------------

def parse_hot(html: str) -> list[dict]:
    """`thread-hotlist-all-<n>.htm`: [{rank, id, title, author, heat}]."""
    out = []
    for row in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        m = re.search(r'<td class="hot_num(?:_active)?">\s*(\d+)\s*</td>\s*<td><a[^>]*href="thread-(\d+)\.htm"[^>]*>(.*?)</a>',
                      row, re.S)
        if not m:
            continue
        author = re.search(r'href="user-home-\d+\.htm"[^>]*>(.*?)</a>', row, re.S)
        heat = re.search(r'<td class="text-white text-right">\s*(\d+)', row)
        out.append({"rank": int(m.group(1)), "id": int(m.group(2)), "title": text_of(m.group(3)),
                    "author": text_of(author.group(1)) if author else None,
                    "heat": int(heat.group(1)) if heat else None,
                    "url": f"{BASE}thread-{m.group(2)}.htm"})
    return out


# --- thread pages --------------------------------------------------------------------------------------

@dataclass
class RawPost:
    floor: int
    pid: int | None
    user: str
    text: str                  # the post's own text, quotes rendered as `> @user: …` lines
    own: str                   # the post's own text without quotes (what a later quote is matched to)
    quotes: list[tuple[str, str]] = field(default_factory=list)   # (quoted user, quoted text)
    date: datetime | date | None = None
    likes: int = 0


@dataclass
class ThreadPage:
    id: int
    page: int
    title: str
    forum: str | None
    forum_id: int | None
    replies: int               # the site's reply count (floors 2..n)
    pages: int
    views: int | None
    op: RawPost | None         # floor 1, on page 1 only (pages 2+ show a "查看主题内容" stub)
    op_truncated: bool         # the site cut the opening post for guests
    posts: list[RawPost]

    @property
    def total(self) -> int:
        return max([self.replies + 1] + [p.floor for p in self.posts])


_MARKDOWN = re.compile(r'<textarea[^>]*js-message-markdown[^>]*>(.*?)</textarea>', re.S | re.I)


def is_verification_page(html: str) -> bool:
    """The check a guest gets on some older threads: a 200 page with no posts."""
    return bool(re.search(r"<title>\s*安全验证", html))


# A bare obfuscated URL: three hex digits, then the site's encoding, which starts the same for every
# https:// address. Inside a link's href it follows `elink@`, so the look-behind leaves it to the link rules.
_BARE_LINK = re.compile(r"(?<![0-9A-Za-z_@])[0-9a-f]{3}K9s2c8@[^\s<>\"'\u3000-\u9fff\uff00-\uffef]+")


def hide_links(html: str) -> str:
    """Replace guest-hidden links, in rendered HTML and in the Markdown source alike: an `elink@`
    link keeps its text, and a bare obfuscated URL (or a link showing only one) becomes the note."""
    html = _BARE_LINK.sub(LINK_HIDDEN, html)
    html = re.sub(r'<a [^>]*href="elink@[^"]*"[^>]*>\s*(?:<mark class="encrypted">.*?</mark>|'
                  + re.escape(LINK_HIDDEN) + r')?\s*</a>', LINK_HIDDEN, html, flags=re.S)
    html = re.sub(r'<a [^>]*href="elink@[^"]*"[^>]*>(.*?)</a>', rf"\1 {LINK_HIDDEN}", html, flags=re.S)
    return html


def clean_message(html: str) -> tuple[str, list[tuple[str, str]], bool]:
    """Post HTML -> (text, quotes, truncated). The page carries a post's Markdown source, which is
    complete where the rendered HTML is cut for guests and keeps what rendering damages (`~` becomes
    strikethrough), so it wins when present (D-49). Drops the site's promo link and edit note; hides
    obfuscated links; lists attachments by name only."""
    source = _MARKDOWN.search(html)
    markdown = htmllib.unescape(source.group(1)).replace("\r\n", "\n").strip() if source else ""
    truncated = "expandNoteBox" in html and not markdown
    html = re.sub(r'<textarea[^>]*>.*?</textarea>', "", html, flags=re.S | re.I)
    html = re.sub(r'(<img\b[^>]*\bsrc=")(upload/)', rf"\1{BASE}\2", html)
    html = re.sub(r'<input[^>]*>', "", html)
    html = remove_elements(html, r'<div[^>]*class="expandNoteBox\b')
    html = remove_elements(html, r'<div style="height: 1px;" class="position-relative">')
    html = remove_elements(html, r'<div class="text-right text-muted my-3 small font-italic">')
    html = re.sub(r'<p>\s*<a class="bbs_thread"[^>]*>.*?</a\s*>\s*</p\s*>', "", html, flags=re.S)
    html = hide_links(html)
    markdown = hide_links(markdown)

    attachments: list[str] = []

    def attachment(m: re.Match) -> str:
        items = re.findall(r'<li[^>]*>\s*<a[^>]*>(.*?)</a>\s*(?:<span[^>]*>(.*?)</span>)?', m.group(0), re.S)
        found = [f"[attachment: {text_of(name)} {text_of(size)}]" for name, size in items]
        attachments.extend(found)
        return "".join(f"<p>{htmllib.escape(line)}</p>" for line in found)

    html = re.sub(r'<fieldset class="fieldset">\s*<legend>上传的附件：</legend>.*?</fieldset>', attachment, html,
                  flags=re.S)
    quotes: list[tuple[str, str]] = []

    def quote(m: re.Match) -> str:
        inner = m.group(1)
        who = re.search(r'<a [^>]*class="[^"]*\buser\b[^"]*"[^>]*>(.*?)</a>', inner, re.S)
        user = text_of(who.group(1)) if who else ""
        body = text_of(inner[who.end():] if who else inner)
        quotes.append((user, body))
        return quote_html(f"@{user}" if user else "", None, inner[who.end():] if who else inner)

    html = re.sub(r'<blockquote class="blockquote">(.*?)</blockquote>', quote, html, flags=re.S)
    if markdown:
        return "\n".join([re.sub(r"\]\(upload/", f"]({BASE}upload/", markdown), *attachments]), quotes, False
    return html_to_text(html), quotes, truncated


def _own_text(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.startswith(">"))


def parse_thread_page(html: str, tid: int, page: int = 1, now: datetime | None = None) -> ThreadPage:
    title = re.search(r"<title>(.*?)</title>", html, re.S)
    title_s = text_of(title.group(1)) if title else ""
    crumbs = re.findall(r'<li class="breadcrumb-item"><a href="forum-(\d+)\.htm">(.*?)</a>', html)
    forum_id, forum = (int(crumbs[-1][0]), text_of(crumbs[-1][1])) if crumbs else (None, None)
    if forum and title_s.find(f"-{forum}-") > 0:
        title_s = title_s[: title_s.rfind(f"-{forum}-")]
    replies = re.search(r'<span\s+class="posts">(\d+)</span>', html)
    replies_n = int(replies.group(1)) if replies else 0
    views = re.search(r"发表于:.*?<span style=\"vertical-align: middle;\">\s*(\d+)", html, re.S)
    op, truncated = None, False
    start = re.search(r'<div class="message[^"]*" isfirst="1">', html)
    if start and page == 1:
        body = element_inner(html, r'<div class="message[^"]*" isfirst="1">', start=start.start()) or ""
        text, quotes, truncated = clean_message(body)
        user = re.search(r"<a class=\"small\" href='user-home-\d+\.htm'[^>]*>(.*?)</a>", html, re.S)
        posted = re.search(r"发表于:\s*([^<]+)</span>", html)
        likes = re.search(r'<span class="thumbs_num">(\d+)</span>', html)
        if truncated:
            text = f"{text}\n{TRUNCATED_NOTE}" if text else TRUNCATED_NOTE
        op = RawPost(1, None, text_of(user.group(1)) if user else "?", text, _own_text(text), quotes,
                     kx_date(text_of(posted.group(1)), now) if posted else None,
                     int(likes.group(1)) if likes else 0)
    posts = []
    for chunk in re.split(r'(?=<tr class="post\b[^"]*" data-pid=")', html)[1:]:
        pid = re.match(r'<tr class="[^"]*" data-pid="(\d+)"', chunk)
        floor = re.search(r'<span class="floor">(\d+)</span>', chunk)
        if not pid or not floor:
            continue
        user = re.search(r'<a class="username_box"[^>]*>(.*?)</a>', chunk, re.S)
        mstart = re.search(r'<div class="message mt-2[^"]*">', chunk)
        body = element_inner(chunk, r'<div class="message mt-2[^"]*">') if mstart else ""
        text, quotes, _ = clean_message(body or "")
        when = re.search(r'<span class="date text-grey small[^"]*">(.*?)</span>', chunk, re.S)
        likes = re.search(r"class='thumb_num'[^>]*>\s*(\d+)", chunk)
        posts.append(RawPost(int(floor.group(1)), int(pid.group(1)), text_of(user.group(1)) if user else "?",
                             text, _own_text(text), quotes,
                             kx_date(text_of(when.group(1)), now) if when else None,
                             int(likes.group(1)) if likes else 0))
    pages = max(page_count(html, f"thread-{tid}"), (replies_n + 1 + PER_PAGE - 1) // PER_PAGE, 1)
    return ThreadPage(tid, page, title_s or f"Thread {tid}", forum, forum_id, replies_n, pages,
                      int(views.group(1)) if views else None, op, truncated, posts)


def _norm(text: str) -> str:
    return re.sub(r"\[em_\d+\]|\s+", "", text or "")


def quote_target(quote: tuple[str, str], number: int, earlier: list[RawPost]) -> int | None:
    """The floor a quote comes from: the latest earlier post by the quoted user whose own text
    contains the start of the quoted text. None when no loaded post matches."""
    user, body = quote
    needle = _norm(body)[:20]
    if not user or not needle:
        return None
    for p in reversed(earlier):
        if p.floor < number and p.user == user and needle in _norm(p.own):
            return p.floor
    return None


def floor_url(tid: int, floor: int, pid: int | None = None) -> str:
    page = (floor - 1) // PER_PAGE + 1
    url = f"{BASE}thread-{tid}{f'-{page}' if page > 1 else ''}.htm"
    return f"{url}#{pid}" if pid else url


def to_posts(raw: list[RawPost], tid: int, op_user: str | None) -> list[Post]:
    """Floors -> Posts: `is_op` for floor 1 and the starter's replies, `↩#M` from the last quote."""
    raw = sorted(raw, key=lambda p: p.floor)
    out = []
    for i, p in enumerate(raw):
        reply_to = quote_target(p.quotes[-1], p.floor, raw[:i]) if p.quotes else None
        out.append(Post(p.floor, p.user, p.text or "(no text)", date=p.date, likes=p.likes,
                        is_op=p.floor == 1 or (bool(op_user) and p.user == op_user),
                        reply_to=reply_to, url=floor_url(tid, p.floor, p.pid)))
    return out
