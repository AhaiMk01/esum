"""Pure parsing for 一亩三分地's JSON API (api.1point3acres.com, trpc.1point3acres.com).

The API serves Discuz! data (tid, fid, pid, unix `dateline`s) with post bodies as BBCode
(`message_bbcode`). Bodies are turned into HTML here and then into text by ESUM's body converter,
so quotes, code, lists and images read like every other site. A thread's replies come as pages
of top-level floors, each carrying its nested replies (楼中楼).
"""

from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlsplit

from esum import ArgumentError, Post, html_to_body, html_to_label, one_line, quote_html

SITE = "https://www.1point3acres.com"
PER_PAGE = 20   # top-level floors per nested-posts page (the API's ceiling)
LOCKED = re.compile(r"才可浏览|才可以浏览|需要积分高于|需要回复")


def thread_url(tid: int) -> str:
    return f"{SITE}/bbs/thread-{tid}-1-1.html"


def text_of(value) -> str:
    """A name the API sends HTML-escaped (`&#128273;晒工资抖包袱`, `A&amp;B`) -> plain text on one line."""
    return one_line(htmllib.unescape(str(value or "")))


def when(ts) -> datetime | None:
    """A unix `dateline` -> an aware UTC datetime."""
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc) if ts else None
    except (TypeError, ValueError, OverflowError):
        return None


def thread_ref(value: str) -> int:
    """A thread id or URL -> tid. Accepts `1196058`, `…/bbs/thread-1196058-1-1.html`,
    `…/home/thread/1196058`, `…/thread/1196058` and `forum.php?mod=viewthread&tid=1196058`."""
    value = str(value).strip()
    if value.isdigit():
        return int(value)
    parts = urlsplit(value)
    if m := re.search(r"thread-(\d+)-\d+-\d+\.html$|/thread/(\d+)/?$", parts.path):
        return int(m.group(1) or m.group(2))
    q = parse_qs(parts.query)
    if (tid := q.get("tid", q.get("ptid", [""]))[0]).isdigit():
        return int(tid)
    raise ArgumentError(f"not a 1point3acres thread id or thread URL: {value!r}",
                        help="Pass the thread number, or a thread URL such as "
                             "https://www.1point3acres.com/bbs/thread-1196058-1-1.html")


# --- BBCode -> HTML ------------------------------------------------------------------------------------

def unwrap(url: str) -> str | None:
    """The real URL behind the site's link wrappers, or None for an inline `data:` image.

    `link.1p3a.com/?url=<quoted>` is the outbound-link redirect; `link.1p3a.com/camo/<hash>/<hex>`
    is the image proxy, whose last segment is the hex-encoded original URL."""
    url = htmllib.unescape(url.strip())
    parts = urlsplit(url)
    if parts.hostname == "link.1p3a.com":
        if m := re.fullmatch(r"/camo/[0-9a-f]+/([0-9a-f]+)", parts.path):
            try:
                url = bytes.fromhex(m.group(1)).decode("utf-8")
            except ValueError:
                return url
        elif target := parse_qs(parts.query).get("url"):
            url = unquote(target[0]) if "%" in target[0] else target[0]
    if url.lower().startswith("data:"):
        return None
    if url.startswith("forum.php") or url.startswith("thread-"):
        url = f"{SITE}/bbs/{url}"
    return url


def _attr(value: str) -> str:
    return htmllib.escape(value, quote=True)


def _img(src: str) -> str:
    real = unwrap(src)
    return f'<img src="{_attr(real)}">' if real else ""


def _link(href: str, label: str | None) -> str:
    real = unwrap(href) or ""
    shown = label if label is not None else htmllib.escape(real)
    return f'<a href="{_attr(real)}">{shown}</a>' if real else shown


def _hide(m: re.Match) -> str:
    """`[hide=200]…[/hide]`: what a guest gets is a lock notice; a member who qualifies gets the content."""
    need, inner = m.group(1), m.group(2)
    plain = re.sub(r"\[/?[a-z]+(?:=[^\]]*)?\]", "", htmllib.unescape(inner))
    if LOCKED.search(plain):
        first = one_line(plain.split("\n")[0])
        reason = f"needs more than {need} points (积分)" if need and need.isdigit() else first
        return f"<p>[hidden: {htmllib.escape(reason)}]</p>"
    return inner


_STRIP = ("color", "size", "font", "align", "backcolor", "p", "indent", "float", "free", "hr", "media",
          "flash", "audio", "td", "table", "postbg", "sup", "sub")
_TAG = re.compile(r"\[(/?)(" + "|".join(_STRIP) + r")(?:=[^\]]*)?\]", re.I)


def _quote(body: str, codes: list[str]) -> str:
    """`[quote]` with Discuz's header (`[color=#999999]user 发表于 date[/color]`) -> a `> @user:` quote."""
    who = re.search(r"\[color=#999999\](.*?)\s*发表于.*?\[/color\]", body, re.S)
    body = re.sub(r"\[size=2\]\s*(?:\[url=[^\]]*\])?\s*\[color=#999999\].*?\[/color\]\s*(?:\[/url\])?\s*\[/size\]",
                  "", body, count=1, flags=re.S)
    author = f"@{htmllib.unescape(who.group(1)).strip()}" if who else ""
    return quote_html(author, None, to_html(body, {}, codes))


def to_html(raw: str, attachments: dict[int, str], codes: list[str] | None = None) -> str:
    """BBCode (already HTML-escaped or not) -> HTML for `html_to_body`. Code blocks wait in `codes`,
    outside the text, so no other tag rewrites them; the outermost call puts them back."""
    outer = codes is None
    codes = [] if codes is None else codes
    s = htmllib.escape(raw.replace("\r\n", "\n"), quote=False)

    def keep(m: re.Match) -> str:
        codes.append(m.group(1).strip("\n"))
        return f"\0{len(codes) - 1}\0"

    s = re.sub(r"\[code\](.*?)\[/code\]", keep, s, flags=re.S | re.I)
    s = re.sub(r"\[hide(?:=(\d+)[^\]]*)?\](.*?)\[/hide\]", _hide, s, flags=re.S | re.I)
    while m := re.search(r"\[quote\]((?:(?!\[quote\]).)*?)\[/quote\]", s, re.S | re.I):
        s = s[:m.start()] + _quote(htmllib.unescape(m.group(1)), codes) + s[m.end():]
    s = re.sub(r"\[img(?:=[^\]]*)?\](.*?)\[/img\]", lambda m: _img(m.group(1)), s, flags=re.S | re.I)
    s = re.sub(r"\[attach(?:img)?\](\d+)\[/attach(?:img)?\]",
               lambda m: _img(attachments[int(m.group(1))]) if int(m.group(1)) in attachments else "[attachment]",
               s, flags=re.I)
    s = re.sub(r"\[url=([^\]]+)\](.*?)\[/url\]", lambda m: _link(m.group(1), m.group(2)), s, flags=re.S | re.I)
    s = re.sub(r"\[url\](.*?)\[/url\]", lambda m: _link(m.group(1), None), s, flags=re.S | re.I)
    s = re.sub(r"\[email(?:=[^\]]*)?\](.*?)\[/email\]", r"\1", s, flags=re.S | re.I)
    for tag, html in (("b", "b"), ("i", "i"), ("u", "u"), ("s", "s"), ("strike", "s")):
        s = re.sub(rf"\[{tag}\](.*?)\[/{tag}\]", rf"<{html}>\1</{html}>", s, flags=re.S | re.I)
    s = re.sub(r"\[list(?:=[^\]]*)?\]", "<ul>", s, flags=re.I)
    s = re.sub(r"\[/list\]", "</ul>", s, flags=re.I)
    s = re.sub(r"\[\*\]", "<li>", s)
    s = re.sub(r"\[tr\]", "\n", s, flags=re.I)
    s = re.sub(r"\[/tr\]", "", s, flags=re.I)
    s = _TAG.sub("", s)
    s = re.sub(r"data:image/[a-z+]+;base64,[A-Za-z0-9+/=]{16,}", "[image]", s)   # a pasted inline image
    s = "<p>" + s.replace("\n", "<br>") + "</p>"
    if not outer:
        return s
    return re.sub(r"\0(\d+)\0", lambda m: f"<pre>{codes[int(m.group(1))]}</pre>", s)


def body_text(item: dict) -> str:
    """A thread's or post's body as text."""
    attachments = {int(a["aid"]): a["url"] for a in item.get("attachment_list") or []
                   if a.get("aid") and a.get("url")}
    raw = item.get("message_bbcode")
    if raw:
        return html_to_body(to_html(raw, attachments))
    return html_to_body(item.get("message_html") or item.get("message") or "")


def locked_points(item: dict) -> int | None:
    """The points (积分) a `[hide=N]` lock asks for, when this viewer gets the lock notice."""
    m = re.search(r"\[hide=(\d+)[^\]]*\](.*?)\[/hide\]", item.get("message_bbcode") or "", re.S | re.I)
    return int(m.group(1)) if m and LOCKED.search(m.group(2)) else None


# --- listings ------------------------------------------------------------------------------------------

def thread_row(t: dict, forums: dict[int, str] | None = None) -> dict:
    fid = t.get("fid")
    name = t.get("forum_name")
    if isinstance(name, dict):
        name = name.get("name")
    return {
        "id": t.get("tid"),
        "title": html_to_label(t.get("subject") or "", keep_links=False),
        "forum": text_of(name or (forums or {}).get(fid) or ""),
        "forum_id": fid,
        "type": text_of(t.get("thread_type") or ""),
        "type_id": t.get("typeid") or None,
        "author": text_of(t.get("author") or ""),
        "author_id": t.get("authorid") or None,
        "date": when(t.get("dateline")),
        "replies": t.get("replies"),
        "views": t.get("views") or None,   # boards that hide views (面经, 晒工资) send 0 on every thread
        "last_post": when(t.get("lastpost")),
        "last_user": text_of(t.get("lastposter") or ""),
        "recommends": t.get("recommend_add"),
        "digest": bool(t.get("digest")),
        "sticky": (t.get("displayorder") or 0) > 0,
        "url": thread_url(t.get("tid")),
    }


def search_row(t: dict) -> dict:
    row = thread_row(t)
    text = re.sub(r"data:image/[a-z+]+;base64,[A-Za-z0-9+/=]{16,}", "[image]", t.get("message") or "")
    row["excerpt"] = text_of(text)[:300]
    return row


def forum_rows(forums: list[dict]) -> list[dict]:
    """The board tree (groups → boards → sub-boards) flattened in site order: each board after its
    parent, with its group's name and its thread categories (`types`: typeid -> name)."""
    by_id = {f["fid"]: f for f in forums}
    kids: dict[int, list[dict]] = {}
    for f in sorted(forums, key=lambda f: (f.get("displayorder") or 0, f["fid"])):
        kids.setdefault(f.get("fup") or 0, []).append(f)
    out: list[dict] = []

    def add(f: dict, group: str) -> None:
        types = (f.get("forum_field") or {}).get("thread_types") or {}
        out.append({"id": f["fid"], "name": text_of(f.get("name") or ""), "group": group,
                    "parent": f.get("fup") if f.get("type") == "sub" else None,
                    "today": f.get("todayposts") or 0,
                    "types": "; ".join(f"{int(k)} {text_of(v)}" for k, v in types.items()
                                       if str(k).isdigit() and int(k)),
                    "open": f.get("status") == 1})
        for k in kids.get(f["fid"], []):
            add(k, group)

    for g in kids.get(0, []):
        if g.get("type") == "group":
            for f in kids.get(g["fid"], []):
                add(f, text_of(g.get("name") or ""))
    seen = {r["id"] for r in out}
    for f in forums:   # anything outside a group still gets a row
        if f.get("type") != "group" and f["fid"] not in seen:
            add(f, text_of((by_id.get(f.get("fup")) or {}).get("name") or ""))
    return out


def split_types(types: str) -> dict[int, str]:
    """A board row's `types` text (`532 选组选Offer; 1356 应届生NG`) -> {typeid: name}."""
    out = {}
    for part in filter(None, (types or "").split("; ")):
        k, _, v = part.partition(" ")
        if k.isdigit():
            out[int(k)] = v
    return out


# --- threads -------------------------------------------------------------------------------------------

@dataclass
class Floor:
    """One top-level floor and its nested replies (楼中楼), as the API pages them."""
    pid: int
    number: int
    user: str
    text: str
    date: datetime | None
    likes: int
    reactions: int
    quote_pid: int | None
    nested: list[dict] = field(default_factory=list)
    nested_total: int = 0


def _reactions(item: dict) -> int:
    return sum(int(r.get("total") or 0) for r in item.get("reactions") or [])


def _likes(item: dict) -> int:
    return int((item.get("votes") or {}).get("upvote") or 0)


def floors(data: dict, page: int) -> list[Floor]:
    """One nested-posts page -> its floors, numbered as the thread's posts (#1 is the opening post,
    so the first reply on page 1 is #2)."""
    out = []
    for i, p in enumerate(data.get("posts") or []):
        sub = p.get("replies") or {}
        out.append(Floor(pid=p["pid"], number=2 + (page - 1) * PER_PAGE + i, user=text_of(p.get("author") or "?"),
                         text=body_text(p), date=when(p.get("dateline")), likes=_likes(p), reactions=_reactions(p),
                         quote_pid=p.get("quote_pid") or None, nested=list(sub.get("data") or []),
                         nested_total=int(sub.get("count") or 0)))
    return out


def nested_text(floor: Floor, op: str = "") -> str:
    """The floor's nested replies as `↳ user (date · 👍N): text` lines under its own text."""
    if not floor.nested:
        return ""
    lines = []
    for r in floor.nested:
        d = when(r.get("dateline"))
        user = text_of(r.get("author") or "?")
        meta = " · ".join(x for x in (d and f"{d:%Y-%m-%d %H:%M}", _likes(r) and f"👍{_likes(r)}",
                                      user == op and "OP") if x)
        to = (r.get("quote_author") or {}).get("author") if r.get("quote_pid") not in (None, 0, floor.pid) else ""
        text = body_text(r).replace("\n", "\n  ")
        lines.append(f"↳ {user}{f' ({meta})' if meta else ''}: "
                     f"{f'@{text_of(to)} ' if to else ''}{text}")
    more = floor.nested_total - len(floor.nested)
    if more > 0:
        lines.append(f"↳ … {more} more nested repl{'y' if more == 1 else 'ies'} not shown")
    return "\n".join(lines)


def to_posts(found: list[Floor], op: str, url_of) -> list[Post]:
    number_of = {f.pid: f.number for f in found}
    out = []
    for f in found:
        nested = nested_text(f, op)
        reply = number_of.get(f.quote_pid) if f.quote_pid else None
        out.append(Post(number=f.number, user=f.user, text=f.text + (f"\n\n{nested}" if nested else ""),
                        date=f.date, likes=f.likes, reactions=f.reactions, is_op=f.user == op,
                        reply_to=reply, reply_to_id=f.quote_pid if f.quote_pid and reply is None else None,
                        url=url_of(f.pid)))
    return out


def opening_post(t: dict) -> Post:
    return Post(number=1, user=text_of(t.get("author") or "?"), text=body_text(t), date=when(t.get("dateline")),
                likes=_likes(t), reactions=_reactions(t), is_op=True, url=thread_url(t["tid"]))
