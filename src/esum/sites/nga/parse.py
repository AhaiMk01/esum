"""Pure parsers for NGA's JSON pages (`__output=11`): the forum directory, topic lists, search, hot
topics and thread pages.

Posts are BBCode, not HTML: `[quote]`, `[img]`, `[url]`, `[s:ac:…]` stickers, with `<br/>` for line
breaks and HTML entities in the text. A floor (楼, `lou`) is a post's place in its thread: the
opening post is floor 0 and page N holds floors (N-1)*20 … N*20-1. Hidden and deleted floors are
missing from their page.

NGA's JSON gives every author as `UID:<n>`; real names survive only in topic rows (`author`) and in
the `[uid=n]name[/uid]` tags of quote and reply headers, so `names_in` collects those.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from esum import (ArgumentError, Post, one_line)

BASE = "https://bbs.nga.cn"
HOSTS = ("bbs.nga.cn", "ngabbs.com", "nga.178.com", "g.nga.cn")
ATTACH = "https://img.nga.cn/attachments/"
PER_PAGE = 20          # floors on a thread page
QUOTE_CHARS = 300
BEIJING = timezone(timedelta(hours=8))
# Messages that mean "log in": guests may not open the page (15), not logged in (1).
LOGIN_MESSAGES = re.compile(r"访客不能直接访问|未登录")
# Search needs a registered account with reputation (2048): a guest, or a member with 威望 0.
MEMBERS_ONLY = "注册用户"
# 找不到主题(集), 版面ID…不存在, and 未指定帖子 (what read.php?pid= says for a post that doesn't exist)
NOT_FOUND = re.compile(r"找不到|不存在|未指定帖子")
NO_BOARD = re.compile(r"版面|主题集")


def num(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def from_unix(value) -> datetime | None:
    seconds = num(value)
    return datetime.fromtimestamp(seconds, tz=timezone.utc) if seconds > 0 else None


def from_local(value) -> datetime | None:
    """`2026-10-27 23:40`, Beijing time, as the post list gives the opening post's date."""
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M").replace(tzinfo=BEIJING)
    except ValueError:
        return None


def error_of(payload: dict) -> str | None:
    """The site's message (`找不到主题`) when the reply is an error, else None."""
    err = payload.get("error")
    if not err:
        return None
    first = err[0] if isinstance(err, list) and err else err
    return re.sub(r"^\d+:", "", str(first)).strip() or "unknown error"


def rows(value) -> list:
    """NGA sends lists either as JSON arrays or as objects keyed "0", "1", …"""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [v for k, v in sorted(value.items(), key=lambda kv: num(kv[0], 10**9))]
    return []


# --- references --------------------------------------------------------------------------------

def thread_ref(value: str) -> tuple[int | None, int | None]:
    """(tid, pid) from a topic id, `pid:<n>`, or a read.php URL on any NGA host."""
    value = value.strip()
    if value.isdigit():
        return int(value), None
    if m := re.fullmatch(r"pid[:=]?(\d+)", value, re.I):
        return None, int(m.group(1))
    parts = urlsplit(value if "://" in value else f"https://{value}")
    if parts.hostname not in HOSTS:
        raise ArgumentError(f"{value!r} is not an NGA topic id, pid:<n> or read.php URL",
                            help="Pass the tid number, pid:<post id>, or a read.php?tid=<n> URL")
    q = parse_qs(parts.query)
    tid = num((q.get("tid") or [""])[0]) or None
    pid = num((q.get("pid") or [""])[0]) or None
    if tid is None and pid is None:
        raise ArgumentError(f"{value!r} is not an NGA topic id, pid:<n> or read.php URL",
                            help="Pass the tid number, pid:<post id>, or a read.php?tid=<n> URL")
    return tid, (pid if not tid else None)


def topic_url(tid: int) -> str:
    return f"{BASE}/read.php?tid={tid}"


def page_of(floor: int) -> int:
    return floor // PER_PAGE + 1


def floor_url(tid: int, floor: int) -> str:
    page = page_of(floor)
    return topic_url(tid) + (f"&page={page}" if page > 1 else "") + f"#l{floor}"


# --- BBCode --------------------------------------------------------------------------------------

_IMG = re.compile(r"\[img(?:=[^\]]*)?\]\s*(.*?)\s*\[/img\]", re.I | re.S)
_CUT_IMG = re.compile(r"\[img(?:=[^\]]*)?\][^\[\s]*", re.I)   # NGA cuts long quotes mid-tag
_STICKER = re.compile(r"\[s:[^\]]*\]")
_URL_LABEL = re.compile(r"\[url=([^\]]+)\](.*?)\[/url\]", re.I | re.S)
_URL = re.compile(r"\[url\](.*?)\[/url\]", re.I | re.S)
_UID = re.compile(r"\[uid(?:=(-?\d+))?\]([^\[]*)\[/uid\]", re.I)
_REF = re.compile(r"\[(pid|tid)=([^\]]*)\]([^\[]*)\[/\1\]", re.I)
_DEL = re.compile(r"\[del\](.*?)\[/del\]", re.I | re.S)
_COLLAPSE = re.compile(r"\[collapse(?:=([^\]]*))?\]", re.I)
_ITEM = re.compile(r"\[\*\]")
_CELL = re.compile(r"\[/td\]\s*", re.I)
_ROW = re.compile(r"\[/tr\]", re.I)
_TAG = re.compile(r"\[/?(?:b|i|u|del|color|size|font|align|collapse|h|list|table|tr|td|code|quote|"
                  r"flash|l|r|attach|dice|lessernuke|randomblock|span|crypt|album|stripbr|style|"
                  r"comment|pid|tid|uid)(?:=[^\]]*)?\]", re.I)
_BR = re.compile(r"<br\s*/?>", re.I)
_INNER_QUOTE = re.compile(r"\[quote\]((?:(?!\[quote\]).)*?)\[/quote\]", re.I | re.S)
# `[pid=PID,TID,PAGE]Reply[/pid]` points at a reply, `[tid=TID]Topic[/tid]` at the opening post
_TARGET = r"\[(?P<kind>pid|tid)=(?P<id>\d+)(?:,\d+)?(?:,(?P<page>\d+))?\][^\[]*\[/(?P=kind)\]"
_AUTHOR = r"Post by\s*\[uid(?:=-?\d+)?\](?P<user>[^\[]*)\[/uid\]\s*(?:\([^)]*\))?\s*:?\s*"
_REPLY_HEAD = re.compile(r"^\s*\[b\]\s*Reply to\s*" + _TARGET + r"\s*" + _AUTHOR + r"\[/b\]", re.I)
_QUOTE_HEAD = re.compile(r"^\s*(?:" + _TARGET + r")?\s*\[b\]\s*" + _AUTHOR + r"\[/b\]", re.I)
# A quote's place in `Body.text` until `render_body` knows floors: `\x00Q<i>\x00`, then `: text`.
_MARK = re.compile(r"\x00Q(\d+)\x00")
_MARK_LINE = re.compile(r"\n?\x00Q\d+\x00[^\n]*\n?")
_MARK_TEXT = re.compile(r"\x00Q(\d+)\x00(: )?")


def image_url(raw: str) -> str:
    raw = html.unescape(raw.strip())
    if raw.startswith("./"):
        return ATTACH + raw[2:]
    if raw.startswith("/attachments/"):
        return "https://img.nga.cn" + raw
    return raw


def _markup(s: str) -> str:
    """BBCode and `<br/>` to plain text; quotes are handled before this."""
    s = _STICKER.sub(lambda m: f":{m.group(0)[1:-1].rsplit(':', 1)[-1]}:", s)   # the name carries the tone
    s = _DEL.sub(r"~~\1~~", s)
    s = _IMG.sub(lambda m: f" [image: {image_url(m.group(1))}] ", s)
    s = _URL_LABEL.sub(lambda m: m.group(1) if m.group(2).strip() in ("", m.group(1))
                       else f"{m.group(2)} ({m.group(1)})", s)
    s = _URL.sub(r"\1", s)
    s = _UID.sub(lambda m: "@" + user_name(m.group(2)), s)
    s = _REF.sub(lambda m: f"{m.group(3)} ({m.group(1).lower()} {m.group(2)})".strip(), s)
    s = _COLLAPSE.sub(lambda m: f"\n{m.group(1)}: " if m.group(1) else "\n", s)
    s = _ITEM.sub("\n- ", s)
    s = _CELL.sub(" | ", s)
    s = _ROW.sub("\n", s)
    s = _TAG.sub(" ", s)
    s = _BR.sub("\n", s)
    return unescape(s)


def unescape(s: str) -> str:
    """NGA escapes what users type, entities included: `&amp;#128020;` is an emoji."""
    once = html.unescape(s)
    return html.unescape(once) if "&" in once else once


def user_name(raw: str) -> str:
    """A name from a `[uid]` tag or `__U`. Anonymous posts (匿名) carry `#anony_<hash>`, one hash per
    poster, so a short piece of it keeps different anonymous posters apart."""
    name = one_line(raw)
    if not name.startswith("#anony"):
        return name
    code = name.removeprefix("#anony").lstrip("_")[:4]
    return f"anonymous#{code}" if code else "anonymous"


def _shorten(text: str, cap: int) -> str:
    text = one_line(text)
    return text if len(text) <= cap else text[:cap].rstrip() + "…"


@dataclass
class Quote:
    """Who a post quotes or replies to: a post id (`pid`, 0 for the opening post) and a page."""

    user: str | None
    pid: int | None
    page: int | None = None


@dataclass
class Body:
    text: str
    reply: Quote | None = None                         # a `Reply to` header, or the first quote
    quotes: list[Quote] = field(default_factory=list)  # every quote, in order (markers in `text`)


def _target(m: re.Match) -> Quote:
    """The post a reply or quote header points at; a `[tid]` target is the opening post (pid 0)."""
    user = user_name(m.group("user")) or None
    if not m.group("kind"):
        return Quote(user, None)
    if m.group("kind").lower() == "tid":
        return Quote(user, 0)
    return Quote(user, num(m.group("id")), num(m.group("page")) or None)


def post_body(content: str) -> Body:
    """A post's BBCode as text. A quote becomes one `> @user #N: …` line whose `#N` placeholder
    (`_MARK`) `render_body` fills in once floors are known."""
    s = str(content or "")
    reply = None
    if m := _REPLY_HEAD.match(s):
        reply = _target(m)
        s = s[m.end():]
    quotes: list[Quote] = []
    dropped: set[int] = set()
    while m := _INNER_QUOTE.search(s):   # innermost first
        inner = m.group(1)
        head = _QUOTE_HEAD.match(inner)
        q = _target(head) if head else Quote(None, None)
        if head:
            inner = inner[head.end():]
        # a quote inside this one only mattered to the quoted author: drop it
        dropped.update(num(i) for i in _MARK.findall(inner))
        text = _shorten(_markup(_CUT_IMG.sub(" ", _IMG.sub(" ", _MARK_LINE.sub(" ", inner)))), QUOTE_CHARS)
        quotes.append(q)
        marker = f"\x00Q{len(quotes) - 1}\x00"
        s = s[:m.start()] + f"\n{marker}{': ' + text if text else ''}\n" + s[m.end():]
    kept = [q for i, q in enumerate(quotes) if i not in dropped]
    if reply is None:
        reply = next((q for q in kept if q.pid is not None), None)
    return Body(_markup(s), reply, quotes)


def render_body(body: Body, floors: dict[int, int]) -> str:
    """Fill the quote markers: `> @user #N` when the quoted post's floor is known (`floors` maps
    pid → floor; the opening post is pid 0), else `> @user (page P)`."""
    def head(m: re.Match) -> str:
        label = _label(body.quotes[int(m.group(1))], floors)
        return f"> {label}{m.group(2) or ''}" if label else "> "
    text = _MARK_TEXT.sub(head, body.text)
    if body.reply and not any(q is body.reply for q in body.quotes) and floor_of(body.reply, floors) is None:
        text = f"↩ {_label(body.reply, floors)}\n{text}"
    return text


def _label(q: Quote, floors: dict[int, int]) -> str:
    return " ".join(p for p in (q.user and f"@{q.user}", ref(q, floors)) if p)


def floor_of(q: Quote, floors: dict[int, int]) -> int | None:
    return floors.get(q.pid) if q.pid is not None else None


def ref(q: Quote, floors: dict[int, int]) -> str:
    n = floor_of(q, floors)
    if n is not None:
        return f"#{n}"
    if not q.page:
        return ""
    first = (q.page - 1) * PER_PAGE
    return f"(page {q.page}: #{first}–#{first + PER_PAGE - 1})"


def names_in(content: str) -> dict[int, str]:
    """`[uid=n]name[/uid]` pairs in a post: the only place the JSON keeps real user names."""
    return {num(u): name.strip() for u, name in _UID.findall(str(content or ""))
            if u and num(u) > 0 and name.strip() and not name.startswith("#anony")}


# --- forums ----------------------------------------------------------------------------------------

def forums(payload: dict) -> list[dict]:
    """Every board and collection in `app_api.php home/category`: fid (stid for a collection),
    name, description, category and group."""
    found = []
    for cat in rows(payload.get("data")):
        for group in cat.get("groups") or []:
            for f in group.get("forums") or []:
                stid = num(f.get("stid")) or None
                found.append({"fid": num(f.get("fid")), "stid": stid, "name": one_line(f.get("name")),
                              "info": one_line(f.get("info") or f.get("infoL") or f.get("infoS") or ""),
                              "category": one_line(cat.get("name")), "group": one_line(group.get("name")),
                              "url": f"{BASE}/thread.php?" + (f"stid={stid}" if stid else f"fid={num(f.get('fid'))}")})
    return found


def match_forums(found: list[dict], query: str | None) -> list[dict]:
    q = (query or "").casefold()
    return [f for f in found if not q or any(q in (f[k] or "").casefold() for k in ("name", "info", "category", "group"))]


# --- topic lists ---------------------------------------------------------------------------------

def subject(t: dict) -> str:
    return one_line(unescape(str(t.get("subject") or "")))


def topic_row(t: dict) -> dict:
    tid = num(t.get("tid"))
    parent = t.get("parent") or {}
    denied = one_line(t.get("error")) if t.get("denied") else None
    return {"id": tid, "title": f"(hidden from this account: {denied})" if denied else subject(t),
            "author": one_line(t.get("author")) or None, "fid": num(t.get("fid")),
            "forum": one_line(parent.get("2") or t.get("forumname") or "") or None,
            "replies": num(t.get("replies")),
            "date": _iso(from_unix(t.get("postdate"))), "last_reply": _iso(from_unix(t.get("lastpost"))),
            "last_reply_by": one_line(t.get("lastposter")) or None,
            "denied": denied,
            "url": topic_url(tid)}


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def topic_page(payload: dict) -> dict:
    """A `thread.php` page: its topic rows, the board, and how many topics match in all."""
    data = payload.get("data") or {}
    topics = [topic_row(t) for t in rows(data.get("__T"))]
    per_page = num(data.get("__T__ROWS_PAGE")) or len(topics) or 1
    forum = data.get("__F") or {}
    return {"threads": topics, "total": num(data.get("__ROWS")), "per_page": per_page,
            "forum": one_line(forum.get("name")) or None, "fid": num(forum.get("fid")) or None}


def hot_topics(payload: dict) -> list[dict]:
    lists = rows(payload.get("data"))
    return [topic_row(t) for t in rows(lists[0] if lists else [])]


# --- threads -------------------------------------------------------------------------------------

@dataclass
class Floor:
    floor: int
    pid: int
    uid: int
    user: str            # `UID:<n>` until a real name is known
    date: datetime | None
    likes: int
    body: Body


@dataclass
class ThreadPage:
    tid: int
    fid: int
    title: str
    forum: str | None
    author: str | None
    author_uid: int
    total: int           # posts in the thread, the opening post included
    page: int
    floors: list[Floor]
    names: dict[int, str]


def thread_page(payload: dict) -> ThreadPage:
    data = payload.get("data") or {}
    topic = data.get("__T") or {}
    users = data.get("__U") or {}
    tid = num(topic.get("tid"))
    names: dict[int, str] = {}
    if topic.get("author") and num(topic.get("authorid")) > 0:
        names[num(topic.get("authorid"))] = one_line(topic["author"])
    floors = []
    for r in rows(data.get("__R")):
        content = r.get("content")
        names.update(names_in(content))
        uid = num(r.get("authorid"))
        user = user_name((users.get(str(uid)) or {}).get("username") or "") or (f"UID:{uid}" if uid else "anonymous")
        date = from_unix(r.get("postdatetimestamp")) or from_local(r.get("postdate"))
        floors.append(Floor(floor=num(r.get("lou")), pid=num(r.get("pid")), uid=uid, user=user, date=date,
                            likes=num(r.get("score")), body=post_body(content)))
    if floors and floors[0].floor == 0 and floors[0].date is None:
        floors[0].date = from_unix(topic.get("postdate"))
    return ThreadPage(tid=tid, fid=num(topic.get("fid")),
                      title=subject(topic) or f"Topic {tid}",
                      forum=one_line((data.get("__F") or {}).get("name")) or None,
                      author=one_line(topic.get("author")) or None, author_uid=num(topic.get("authorid")),
                      total=num(data.get("__ROWS")) or len(floors), page=num(data.get("__PAGE")) or 1,
                      floors=floors, names=names)


def to_post(f: Floor, tid: int, op_uid: int, names: dict[int, str], floors: dict[int, int]) -> Post:
    reply_to = floor_of(f.body.reply, floors) if f.body.reply else None
    return Post(number=f.floor, user=names.get(f.uid, f.user), text=render_body(f.body, floors).strip() or "(no text)",
                date=f.date, likes=f.likes, is_op=op_uid > 0 and f.uid == op_uid,
                reply_to=reply_to if reply_to is not None and reply_to < f.floor else None,
                url=floor_url(tid, f.floor))
