"""Pure parsing for Discuz! X 3.x pages (board index, thread lists, thread pages, notices).

Discuz! has a mobile JSON API, but many sites turn it off, so everything here reads the stock
desktop templates: board rows link `forum-<fid>-1.html` (or `forumdisplay&fid=`), thread rows are
`<tbody id="normalthread_<tid>">` / `stickthread_<tid>`, posts are `<div id="post_<pid>">` with
the floor label in `id="postnum<pid>"` and the body in `<td class="t_f" id="postmessage_<pid>">`,
and errors are a 200 page with `<div id="messagetext">`. The functions take decoded HTML and
return plain data, so the tests run on saved pages.
"""

from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urljoin, urlsplit

from esum import (ArgumentError, Post, html_to_label, html_to_text, text_of, clip, element_end, element_inner,
                  remove_elements, quote_html)


# Floors Discuz names instead of numbering (stock language pack)
FLOOR_NAMES = {"楼主": 1, "沙发": 2, "板凳": 3, "地板": 4}
LOGIN = re.compile(r"登录|登陆")
NO_PERMISSION = re.compile(r"权限|无权")
NOT_FOUND = re.compile(r"不存在|已被删除|已删除|nonexistence")
SEARCH_WAIT = re.compile(r"(\d+)\s*秒")
_FID = r"(?:forum-(\d+)-\d+\.html|forumdisplay&(?:amp;)?fid=(\d+))"


# --- small helpers ------------------------------------------------------------------------------------


def to_int(value: str | None) -> int | None:
    digits = re.sub(r"[^\d]", "", value or "")
    return int(digits) if digits else None


def fid_of(href: str) -> int | None:
    m = re.search(_FID, href)
    return int(m.group(1) or m.group(2)) if m else None


# --- dates ---------------------------------------------------------------------------------------------

_ABS = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")
_REL = re.compile(r"(今天|昨天|前天)\s*(\d{1,2}):(\d{2})")


def dz_date(fragment: str | None, today: date | None = None) -> datetime | date | None:
    """A Discuz date as shown (site time zone, naive): `2026-10-5 23:30`, `2026-1-22`, or a
    relative label (`3 天前`, `昨天 12:30`) whose `<span title="…">` holds the full date."""
    if not fragment:
        return None
    title = re.search(r'<span[^>]*title="([^"]+)"', fragment)
    for s in ([title.group(1)] if title else []) + [text_of(fragment)]:
        if m := _ABS.search(s):
            y, mo, d, h, mi, sec = m.groups()
            if h is None:
                return date(int(y), int(mo), int(d))
            return datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0))
        if m := _REL.search(s):
            day = (today or date.today()) - timedelta(days=("今天", "昨天", "前天").index(m.group(1)))
            return datetime(day.year, day.month, day.day, int(m.group(2)), int(m.group(3)))
    return None


# --- ids from URLs -------------------------------------------------------------------------------------

def thread_ref(value: str) -> tuple[int, int | None]:
    """A thread id or thread URL -> (tid, page or None).

    Accepts `2134210`, `thread-2134210-2-1.html`, `forum.php?mod=viewthread&tid=2134210&page=2`,
    `forum.php?mod=redirect&goto=findpost&ptid=2134210&pid=…` and `archiver/tid-2134210.html`."""
    value = value.strip()
    if value.isdigit():
        return int(value), None
    parts = urlsplit(value)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    if m := re.search(r"thread-(\d+)-(\d+)-\d+\.html$", parts.path):
        return int(m.group(1)), int(m.group(2))
    for key in ("tid", "ptid"):
        if q.get(key, "").isdigit():
            return int(q[key]), int(q["page"]) if q.get("page", "").isdigit() else None
    if m := re.search(r"tid-(\d+)\.html$", parts.path):
        return int(m.group(1)), int(q["page"]) if q.get("page", "").isdigit() else None
    raise ArgumentError(f"not a thread id or thread URL: {value!r}",
                        help="Pass the thread number, or the thread URL copied from the site")


def forum_ref(value: str) -> int | None:
    """A board id or board URL -> fid, else None (the caller may look the value up by name)."""
    value = value.strip()
    if value.isdigit():
        return int(value)
    return fid_of(value.replace("&amp;", "&"))


# --- notices -------------------------------------------------------------------------------------------

def message(html: str) -> str | None:
    """The text of Discuz's notice page (`<div id="messagetext">`), if the page is one."""
    m = re.search(r'<div id="messagetext"[^>]*>\s*<p>(.*?)</p>', html, re.S)
    return text_of(m.group(1)) if m else None


def not_found(msg: str) -> bool:
    """The notice says the thread, board or post doesn't exist (or was deleted)."""
    return bool(NOT_FOUND.search(msg))


def search_wait(msg: str) -> int | None:
    """Seconds a flood-control notice on a search asks to wait (`您在 10 秒内只能进行一次搜索`)."""
    m = SEARCH_WAIT.search(msg)
    return int(m.group(1)) if m and "搜索" in msg else None


def is_guest(html: str) -> bool:
    """The page was served to a logged-out visitor (the header carries the login form)."""
    return 'id="lsform"' in html or "member.php?mod=logging&amp;action=login" in html


def needs_login(html: str) -> str | None:
    """The notice text when the page asks to log in (or denies a guest permission), else None."""
    msg = message(html)
    if msg and (LOGIN.search(msg) or (NO_PERMISSION.search(msg) and is_guest(html))):
        return msg
    return None


# --- board index ---------------------------------------------------------------------------------------

def _em(cell: str, label: str) -> str:
    m = re.search(rf"{label}:(.*?)</em>", cell, re.S)
    return m.group(1) if m else ""


def _count(fragment: str) -> int | None:
    """`13459`, or `<span title="13459">1万</span>` (an abbreviated count) -> 13459."""
    m = re.search(r'title="(\d+)"', fragment)
    return int(m.group(1)) if m else to_int(text_of(fragment))


def parse_forums(html: str) -> list[dict]:
    """The board catalog on `forum.php`: one row per board, sub-boards after their parent."""
    out: list[dict] = []
    seen: set[int] = set()
    groups = list(re.finditer(r'<div id="category_(\d+)"[^>]*>', html))
    for i, g in enumerate(groups):
        head = html[max(0, g.start() - 2000): g.start()]
        names = re.findall(r'<h2><a href="[^"]*gid=\d+"[^>]*>(.*?)</a></h2>', head, re.S)
        group = text_of(names[-1]) if names else ""
        stop = groups[i + 1].start() if i + 1 < len(groups) else len(html)
        block = html[g.end(): stop]
        # each board cell starts at its name link: `<h2><a href="forum-N-1.html">` or `<dt><a …>`
        cells = list(re.finditer(rf'<(?:h2|dt)><a href="[^"]*?{_FID}"[^>]*>(.*?)</a>', block, re.S))
        for j, c in enumerate(cells):
            fid = int(c.group(1) or c.group(2))
            if fid in seen:
                continue
            seen.add(fid)
            cell = block[c.end(): cells[j + 1].start() if j + 1 < len(cells) else len(block)]
            today = re.search(r'<em class="xw0 xi1" title="今日">\s*\((\d+)\)', cell)
            if fl_i := re.search(r'<td class="fl_i">(.*?)</td>', cell, re.S):   # table layout
                threads, _, posts = fl_i.group(1).partition(" / ")
            else:                                                               # grid layout
                threads, posts = _em(cell, "主题"), _em(cell, "帖数")
            desc = re.search(r'<p class="xg2">(.*?)</p>', cell, re.S)
            out.append({
                "id": fid, "name": text_of(c.group(3)), "group": group, "parent": None,
                "threads": _count(threads), "posts": _count(posts),
                "today": int(today.group(1)) if today else 0,
                "description": clip(text_of(desc.group(1)), 200) if desc else "",
            })
            subs = re.search(r"子版块:(.*?)</p>", cell, re.S)
            for s in re.finditer(rf'<a href="[^"]*?{_FID}"[^>]*>(.*?)</a>', subs.group(1) if subs else "", re.S):
                sid = int(s.group(1) or s.group(2))
                if sid not in seen:
                    seen.add(sid)
                    out.append({"id": sid, "name": text_of(s.group(3)), "group": group, "parent": fid,
                                "threads": None, "posts": None, "today": 0, "description": ""})
    return out


# --- thread lists (board pages, guide views) -----------------------------------------------------------

def page_count(html: str) -> tuple[int, int]:
    """(current page, pages) from the pager; (1, 1) when there is none."""
    pager = re.search(r'<div class="pg">(.*?)</div>', html, re.S)
    if not pager:
        return 1, 1
    cur = re.search(r"<strong>(\d+)</strong>", pager.group(1))
    total = re.search(r"共\s*(\d+)\s*页", pager.group(1))
    links = [int(n) for n in re.findall(r">(?:\.\.\.\s*)?(\d+)</a>", pager.group(1))]
    page = int(cur.group(1)) if cur else 1
    return page, max([page, *links, int(total.group(1)) if total else 0])


def _by_cell(cell: str, today: date | None) -> tuple[str, datetime | date | None]:
    user = re.search(r"<cite>\s*(?:<a[^>]*>(.*?)</a>|([^<]+))", cell, re.S)
    when = re.search(r"<em>(.*?)</em>", cell, re.S)
    name = text_of((user.group(1) or user.group(2)) if user else "")
    return name, dz_date(when.group(1), today) if when else None


def parse_thread_rows(html: str, base_url: str = "", today: date | None = None) -> list[dict]:
    """Thread rows of a board page or a guide listing (announcement rows are skipped)."""
    out = []
    for m in re.finditer(r'<tbody id="(stickthread|normalthread)_(\d+)"[^>]*>', html):
        tid = int(m.group(2))
        row = html[m.end(): element_end(html, m.start(), "tbody")]
        th = element_inner(row, r"<th\b[^>]*>", "th") or ""
        title = re.search(r'<a href="([^"]*)"[^>]*class="[^"]*\bxst\b[^"]*"[^>]*>(.*?)</a>', th, re.S)
        if not title:
            continue
        kind = re.search(r"<em>\[(?:<a([^>]*)>)?(.*?)(?:</a>)?\]</em>", th[: title.start()], re.S)
        type_id = re.search(r"typeid=(\d+)", kind.group(1) or "") if kind else None
        perm = re.search(r"阅读权限\s*(?:<[^>]+>)*\s*(\d+)", th)
        pages = [int(a or b) for a, b in re.findall(rf"thread-{tid}-(\d+)-\d+\.html|[?&;]page=(\d+)", th)]
        bys = [element_inner(row[c.start():], r'<td class="by"[^>]*>', "td") or "" for c in
               re.finditer(r'<td class="by"', row)]
        board = next((b for b in bys if "<cite" not in b and fid_of(b) is not None), "")
        people = [b for b in bys if "<cite" in b]
        starter, started = _by_cell(people[0], today) if people else ("", None)
        last_user, last_post = _by_cell(people[-1], today) if len(people) > 1 else ("", None)
        num = re.search(r'<td class="num">\s*<a[^>]*>(\d+)</a>\s*<em>(\d+)</em>', row)
        icon = re.search(r'<td class="icn">(.*?)</td>', row, re.S)
        out.append({
            "id": tid,
            "title": text_of(title.group(2)),
            "type": text_of(kind.group(2)) if kind else "",
            "type_id": int(type_id.group(1)) if type_id else None,
            "url": urljoin(base_url, f"forum.php?mod=viewthread&tid={tid}"),
            "forum": text_of(board),
            "forum_id": fid_of(board),
            "starter": starter,
            "started": started,
            "replies": int(num.group(1)) if num else None,
            "views": int(num.group(2)) if num else None,
            "last_post": last_post,
            "last_user": last_user,
            "pages": max(pages, default=1),
            "sticky": m.group(1) == "stickthread",
            "digest": bool(re.search(r'alt="digest"|digest_\d\.gif', th)),
            "closed": bool(icon and "folder_lock" in icon.group(1)),
            "read_permission": int(perm.group(1)) if perm else None,
        })
    return out


def parse_forum_page(html: str, base_url: str = "", today: date | None = None) -> dict:
    """A board's thread-list page: name, thread count, pager and rows."""
    h1 = re.search(r'<h1 class="xs2">\s*<a[^>]*>(.*?)</a>', html, re.S)
    threads = re.search(r'主题:\s*<strong[^>]*>(\d+)</strong>', html)
    page, pages = page_count(html)
    return {"name": text_of(h1.group(1)) if h1 else "", "threads_total": int(threads.group(1)) if threads else None,
            "page": page, "pages": pages, "threads": parse_thread_rows(html, base_url, today)}


# --- thread pages --------------------------------------------------------------------------------------

@dataclass
class RawPost:
    pid: int
    number: int
    user: str
    text: str
    date: datetime | date | None = None
    likes: int = 0
    quotes: list[int] = field(default_factory=list)   # pids this post quotes
    edited: str | None = None


@dataclass
class ThreadPage:
    title: str
    type: str
    forum: str
    forum_id: int | None
    views: int | None
    replies: int | None
    page: int
    pages: int
    posts: list[RawPost]
    featured: list[RawPost] = field(default_factory=list)   # lifted above the thread, no floor


def _quote(box: str) -> tuple[str, int | None]:
    """One `<div class="quote">` -> (`> @who: text` as a blockquote, quoted pid)."""
    ref = re.search(r"findpost&(?:amp;)?pid=(\d+)", box) or re.search(r"[?&;]pid=(\d+)", box)
    who = re.search(r'<font color="#999999">(.*?)\s*发表于', box, re.S)
    body = element_inner(box, r"<blockquote>", "blockquote")
    if body is None:
        body = box
    body = re.sub(r"<font size=\"2\">\s*<a[^>]*>.*?</a>\s*</font>", "", body, count=1, flags=re.S)
    author = f"@{text_of(who.group(1))}" if who else ""
    return quote_html(author, None, body), int(ref.group(1)) if ref else None


def _attachment(block: str) -> str:
    """An inline attachment block -> `[image: name URL]` / `[attachment: name]`, or nothing when the
    block is an empty placeholder. A file's own link needs a login, so it isn't kept."""
    name = re.search(r"<strong>(.*?)</strong>", block, re.S) \
        or re.search(r'<a href="[^"]*mod=attachment[^"]*"[^>]*>(.*?)</a>', block, re.S)
    label = text_of(name.group(1)) if name else ""
    if "<img" not in block and not label:
        return ""
    if "<img" in block:
        url = re.search(r'(?:zoomfile|file)="(https?://[^"]+)"', block)
        shown = " ".join(part for part in (label, url.group(1) if url else "") if part)
        return f" [image: {htmllib.escape(shown)}] " if shown else " [image] "
    return f" [attachment: {htmllib.escape(label)}] "


def unjam(msg: str) -> str:
    """Drop Discuz's anti-copy noise: the random `<font class="jammer">` strings and the
    `display:none` spans it hides between words, which a reader never sees."""
    msg = remove_elements(msg, r'<font\b[^>]*\bclass="jammer"', "font")
    return remove_elements(msg, r'<span\b[^>]*\bstyle="[^"]*display:\s*none', "span")


def _lazy_image(m: re.Match) -> str:
    """A lazy-loaded post image (`src` a placeholder, the picture in `zoomfile` / `file`) gets its
    real URL as `src`, so the body shows the picture's link."""
    tag = m.group(0)
    real = re.search(r'\b(?:zoomfile|file)="([^"]+)"', tag)
    if not real:
        return tag
    if re.search(r'\bsrc="', tag):
        return re.sub(r'\bsrc="[^"]*"', lambda _: f'src="{real.group(1)}"', tag, count=1)
    return tag[:4] + f' src="{real.group(1)}"' + tag[4:]


def _locked(msg: str) -> str:
    """Hidden content (`<div class="locked">`: reply or pay to see) as one `[hidden: …]` line."""
    while m := re.search(r'<div class="locked"[^>]*>', msg):
        end = element_end(msg, m.start(), "div")
        note = text_of(msg[m.end():end])
        msg = msg[:m.start()] + f"<p>[hidden: {htmllib.escape(note)}]</p>" + msg[end:]
    return msg


def message_text(msg: str) -> tuple[str, list[int]]:
    """A post body (`postmessage` inner HTML) -> (text, quoted pids).

    Quotes keep their body structure as `> @user: …`, cut at 200 characters; edit stamps, attachment pop-ups
    and copy-code buttons are dropped; Discuz code blocks keep their lines."""
    msg = re.sub(r'<img\b(?=[^>]*\bsmilieid=)[^>]*>', "", msg, flags=re.I)
    msg = unjam(msg)
    msg = re.sub(r"<img\b[^>]*>", _lazy_image, msg, flags=re.I)
    # the forum's own images (smilies, [hr] rules, icons), once lazy images have their real src
    msg = re.sub(r'<img\b(?=[^>]*\bsrc="[^"]*(?:static/image/|/smilies/))[^>]*>', "", msg, flags=re.I)
    msg = re.sub(r'<i class="pstatus">.*?</i>', "", msg, flags=re.S)
    msg = re.sub(r"<em[^>]*(?:viewsource|copycode)[^>]*>.*?</em>", "", msg, flags=re.S)
    msg = re.sub(r"<ignore_js_op>(.*?)</ignore_js_op>", lambda m: _attachment(m.group(1)), msg, flags=re.S)
    msg = remove_elements(msg, r'<div class="tip\b[^"]*"', "div")
    msg = _locked(msg)
    while m := re.search(r'<div class="blockcode">', msg):
        end = element_end(msg, m.start(), "div")
        lines = [htmllib.unescape(re.sub(r"<[^>]+>", "", li))
                 for li in re.findall(r"<li>(.*?)(?=<li>|</ol>)", msg[m.start():end], re.S)]
        msg = msg[:m.start()] + "<pre>" + htmllib.escape("\n".join(lines)) + "</pre>" + msg[end:]
    quotes: list[int] = []
    out, pos = [], 0
    while m := re.search(r'<div class="quote">', msg[pos:]):
        start = pos + m.start()
        end = element_end(msg, start, "div")
        replacement, ref = _quote(msg[start:end])
        if ref:
            quotes.append(ref)
        out += [msg[pos:start], replacement]
        pos = end
    out.append(msg[pos:])
    return html_to_text("".join(out)), quotes


def floor_of(label_html: str) -> int | None:
    """A floor label (`<em>7</em><sup>#</sup>`, `楼主`, `沙发`, `12#`) -> number; None for labels
    that are not floors (a 推荐 / 置顶 copy of a reply shown above the thread)."""
    if m := re.search(r"<em>(\d+)</em>", label_html):
        return int(m.group(1))
    label = text_of(label_html)
    if m := re.fullmatch(r"(\d+)\s*#?", label):
        return int(m.group(1))
    return FLOOR_NAMES.get(label)


def parse_posts(html: str, today: date | None = None) -> list[RawPost]:
    """The posts on one thread page, in page order, each floor once. A post lifted above the thread
    (推荐, 置顶) carries no floor number in the page, so its `number` is 0."""
    out, seen = [], set()
    starts = list(re.finditer(r'<div id="post_(\d+)"', html))
    for i, m in enumerate(starts):
        pid = int(m.group(1))
        block = html[m.start(): starts[i + 1].start() if i + 1 < len(starts) else len(html)]
        label = re.search(rf'id="postnum{pid}"[^>]*>(.*?)</a>', block, re.S)
        number = floor_of(label.group(1)) if label else None
        body = element_inner(block, rf'<td class="t_f" id="postmessage_{pid}"[^>]*>', "td")
        if number in seen:
            continue
        if number is not None:
            seen.add(number)
        user = re.search(r'<div class="authi">\s*<a[^>]*>(.*?)</a>', block, re.S) \
            or re.search(r'<div class="authi">(.*?)</div>', block, re.S)
        when = re.search(rf'id="authorposton{pid}"[^>]*>(.*?)</em>', block, re.S)
        rated = re.search(r'参与人数\s*(?:<[^>]+>\s*)*(\d+)', block)
        edited = re.search(r'<i class="pstatus">\s*本帖最后由\s*(.*?)\s*编辑', body or "", re.S)
        if body is None:   # a post hidden from this viewer (banned author, moderation)
            locked = re.search(r'<div class="locked">(.*?)</div>', block, re.S)
            text, quotes = (text_of(locked.group(1)) if locked else "(post hidden)"), []
        else:
            text, quotes = message_text(body)
        out.append(RawPost(pid=pid, number=number or 0, user=text_of(user.group(1)) if user else "?", text=text,
                           date=dz_date(when.group(1), today) if when else None,
                           likes=int(rated.group(1)) if rated else 0, quotes=quotes,
                           edited=_edit_note(text_of(edited.group(1)), text_of(user.group(1)) if user else "", today)
                           if edited else None))
    return out


def _edit_note(stamp: str, author: str, today: date | None) -> str:
    """`本帖最后由 <editor> 于 <date> 编辑` -> `2026-11-04 02:47`, plus `by <editor>` when someone
    other than the author edited it, like other sites' edit notes."""
    editor, sep, when = stamp.rpartition(" 于 ")
    if not sep:
        return stamp
    parsed = dz_date(when, today)
    shown = f"{parsed:%Y-%m-%d %H:%M}" if isinstance(parsed, datetime) else f"{parsed:%Y-%m-%d}" if parsed else when
    return shown if not editor or editor == author else f"{shown} by {editor}"


def parse_thread_page(html: str, today: date | None = None) -> ThreadPage:
    found = parse_posts(html, today)
    subject = re.search(r'<span id="thread_subject">(.*?)</span>', html, re.S)
    if not subject:
        subject = re.search(r"<title>(.*?)</title>", html, re.S)
    kind = re.search(r'<h1 class="ts">\s*<a[^>]*>\[(.*?)\]</a>', html, re.S)
    views = re.search(r"查看:\s*</span>\s*<span[^>]*>(\d+)</span>", html)
    replies = re.search(r"回复:\s*</span>\s*<span[^>]*>(\d+)</span>", html)
    crumbs = re.search(r'<div id="pt"[^>]*>(.*?)</div>\s*</div>', html, re.S)
    boards = list(re.finditer(rf'<a href="[^"]*?{_FID}"[^>]*>(.*?)</a>', crumbs.group(1) if crumbs else ""))
    page, pages = page_count(html)
    title = text_of(subject.group(1)) if subject else ""
    if not re.search(r'id="thread_subject"', html):
        title = re.sub(r"\s+-\s+.*$", "", title)
    return ThreadPage(title=title, type=text_of(kind.group(1)) if kind else "",
                      forum=text_of(boards[-1].group(3)) if boards else "",
                      forum_id=int(boards[-1].group(1) or boards[-1].group(2)) if boards else None,
                      views=int(views.group(1)) if views else None,
                      replies=int(replies.group(1)) if replies else None,
                      page=page, pages=pages, posts=[r for r in found if r.number],
                      featured=[r for r in found if not r.number])


def to_posts(raw: list[RawPost], op: str | None, url_of=None) -> list[Post]:
    """RawPosts -> Posts: OP marked by author name, `↩#M` for the first quoted floor that was read."""
    number_of = {p.pid: p.number for p in raw}
    out = []
    for p in raw:
        reply = next((number_of[q] for q in p.quotes if q in number_of and number_of[q] < p.number), None)
        out.append(Post(number=p.number, user=p.user, text=p.text, date=p.date, likes=p.likes,
                        is_op=p.number == 1 or (op is not None and p.user == op), reply_to=reply,
                        url=url_of(p.pid) if url_of else None, edited=p.edited))
    return out


# --- search results ------------------------------------------------------------------------------------

_UID = re.compile(r"space-uid-(\d+)\.html|[?&;]uid=(\d+)")


def search_id(url: str) -> int | None:
    """The `searchid` a search redirected to (`search.php?mod=forum&searchid=300&…`)."""
    m = re.search(r"[?&]searchid=(\d+)", url)
    return int(m.group(1)) if m else None


def parse_search(html: str, base_url: str = "", today: date | None = None) -> dict:
    """A `search.php?mod=forum` result page: total hits, pager and one row per thread (`li.pbw`).

    Discuz matches thread titles by default; the excerpt is the start of the opening post. A page
    with neither the result list (`#threadlist`) nor the no-results notice (`p.emp`) is not read as
    zero hits."""
    if not re.search(r'id="threadlist"|<p class="emp[ "]', html):
        raise RuntimeError("the search page has neither a result list nor a no-results notice "
                           "(the layout may have changed)")
    rows = []
    for m in re.finditer(r'<li class="pbw" id="(\d+)">', html):
        tid = int(m.group(1))
        li = html[m.start(): element_end(html, m.start(), "li")]
        title = re.search(r"<h3[^>]*>\s*<a[^>]*>(.*?)</a>", li, re.S)
        stats = re.search(r'<p class="xg1">(.*?)</p>', li, re.S)
        paras = re.findall(r"<p>(.*?)</p>", li, re.S)
        meta = paras[-1] if paras else ""
        excerpt = paras[0] if len(paras) > 1 else ""
        author = re.search(r'<a href="([^"]*(?:space-uid-|uid=)[^"]*)"[^>]*>(.*?)</a>', meta, re.S)
        board = re.search(rf'<a href="[^"]*?{_FID}"[^>]*>(.*?)</a>', meta, re.S)
        when = re.search(r"<span>([^<]*\d[^<]*)</span>", meta)
        uid = _UID.search(author.group(1)) if author else None
        st = text_of(stats.group(1)) if stats else ""
        replies, views = re.search(r"(\d+)\s*个回复", st), re.search(r"(\d+)\s*次查看", st)
        rows.append({
            "id": tid,
            "title": html_to_label(title.group(1), keep_links=False) if title else "",
            "replies": int(replies.group(1)) if replies else None,
            "views": int(views.group(1)) if views else None,
            "date": dz_date(when.group(1), today) if when else None,
            "author": text_of(author.group(2)) if author else "",
            "author_id": int(uid.group(1) or uid.group(2)) if uid else None,
            "forum": text_of(board.group(3)) if board else "",
            "forum_id": int(board.group(1) or board.group(2)) if board else None,
            "excerpt": clip(html_to_label(excerpt, keep_links=False), 300),
            "url": urljoin(base_url, f"forum.php?mod=viewthread&tid={tid}"),
        })
    total = re.search(r"相关内容\s*(\d+)\s*个", html)
    page, pages = page_count(html)
    return {"total": int(total.group(1)) if total else None, "page": page, "pages": pages, "threads": rows}
