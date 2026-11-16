"""The compact post format every thread-reading action prints:

    [user #N] (date · 👍likes · OP · ↩#M): text
        continuation line

Parts that don't apply are left out. Gaps in the post numbering (posts not fetched, deleted or
outside the requested range) are shown as `[... omitted … ]` markers so the agent knows what it
has not seen.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from .errors import ArgumentError
from .text import one_line, sanitize_text, squeeze


@dataclass
class Post:
    number: int
    user: str
    text: str
    date: datetime | date | str | None = None
    likes: int | None = 0        # None: the site's data doesn't say
    is_op: bool = False          # written by the thread's original poster
    reply_to: int | None = None  # post number this replies to
    url: str | None = None
    edited: str | None = None    # when (and by whom, if the site says) it was last edited
    reactions: int = 0           # reactions other than likes, where a site has them
    reply_to_id: int | None = None  # the site's id of the post it replies to, when its number isn't known

    def to_dict(self) -> dict:
        d = asdict(self)
        if isinstance(self.date, (datetime, date)):
            d["date"] = self.date.isoformat()
        return d


@dataclass
class Gap:
    """Posts #start..#end (inclusive) are not shown."""

    start: int
    end: int
    reason: str = ""

    @property
    def count(self) -> int:
        return self.end - self.start + 1

    def to_dict(self) -> dict:
        return {"gap": True, "start": self.start, "end": self.end, "count": self.count,
                "reason": self.reason}


def format_date(value: datetime | date | str | None) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.strftime("%Y-%m-%d %H:%M")
    return value.isoformat()


def parse_date(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp as APIs return it (`2026-10-26T15:04:05.123Z`)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_since(value: str | None, now: datetime | None = None) -> datetime | None:
    """A `--since` value: `2026-10-01`, `2026-10-01T08:00` (UTC unless it has an offset), or a
    relative `2w` / `7d` / `12h` / `30m` -> UTC datetime. A malformed, future or pre-year-1 value, or a
    relative zero (now, so nothing is newer), is an ArgumentError."""
    if not value:
        return None
    value = value.strip()
    if m := re.fullmatch(r"(\d+)\s*([wdhm])", value, re.I):
        unit = {"w": "weeks", "d": "days", "h": "hours", "m": "minutes"}[m.group(2).lower()]
        if not m.group(1).strip("0"):
            raise ArgumentError(f"--since {value!r} is now, so nothing is newer",
                                help="give a positive relative time such as 1d or 12h, or a date")
        try:
            return (now or datetime.now(timezone.utc)) - timedelta(**{unit: int(m.group(1))})
        except (OverflowError, ValueError):
            raise ArgumentError(f"--since {value!r} is too far back",
                                help="give a relative time after year 1, such as 2w or 365d, or a date") from None
    dt = parse_date(value)
    if dt is None:
        raise ArgumentError(f"--since {value!r} is not a time",
                            help="use YYYY-MM-DD, an ISO time such as 2026-10-30T08:00Z, or a relative 2w / 7d / 12h / 30m")
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if dt > (now or datetime.now(timezone.utc)) + timedelta(minutes=5):
        raise ArgumentError(f"--since {value!r} is in the future", help="give a past time; times are UTC")
    return dt


def subtree(posts: Iterable[Post], root: int) -> list[int]:
    """`root` and every post that replies to it, directly or further down (`reply_to`), in number
    order: one sub-thread of a thread whose comments are listed in time order."""
    numbers, children = set(), {}
    for post in posts:
        numbers.add(post.number)
        if post.reply_to is not None:
            children.setdefault(post.reply_to, []).append(post.number)
    if root not in numbers:
        raise ArgumentError(f"--root {root} is not a post in this thread",
                            help="Give --root a post number the thread shows (the #N after a name)")
    found, todo = set(), [root]
    while todo:
        number = todo.pop()
        if number not in found:
            found.add(number)
            todo.extend(children.get(number, ()))
    return sorted(found)


def largest_subthreads(posts: Iterable[Post], n: int = 5, least: int = 3) -> list[tuple[int, str, int]]:
    """The `n` top-level comments with the most replies under them (any depth), as
    `(number, user, replies)`, largest first, each with at least `least` replies. A top-level
    comment is one that replies to nothing, or to post #1. Tells an agent which `--root` to read."""
    posts = list(posts)
    replies = reply_counts(posts)
    tops = [(p.number, p.user, replies[p.number]) for p in posts if is_top_level(p)]
    tops = [t for t in tops if t[2] >= least]
    return sorted(tops, key=lambda t: (-t[2], t[0]))[:n]


def is_top_level(post: Post) -> bool:
    """A comment that replies to nothing, or to post #1 (the post itself)."""
    return post.number > 1 and post.reply_to in (None, 1)


def reply_counts(posts: Iterable[Post]) -> dict[int, int]:
    """Each post's number of replies under it, at any depth, by `reply_to`."""
    posts = list(posts)
    children: dict[int, list[int]] = {}
    for post in posts:
        if post.reply_to is not None:
            children.setdefault(post.reply_to, []).append(post.number)

    def size(number: int) -> int:
        count, todo = 0, list(children.get(number, ()))
        while todo:
            count += 1
            todo.extend(children.get(todo.pop(), ()))
        return count

    return {p.number: size(p.number) for p in posts}


def compact_text(text: str) -> str:
    """Trim each line, collapse runs of spaces, drop blank lines; a ``` code block stays as it is."""
    return squeeze(sanitize_text(text or ""))


_one_line = one_line


def format_post(post: Post) -> str:
    parts = []
    if d := format_date(post.date):
        parts.append(_one_line(d))
    if post.likes:
        parts.append(f"👍{post.likes}")
    if post.is_op:
        parts.append("OP")
    if post.reply_to:
        parts.append(f"↩#{post.reply_to}")
    elif post.reply_to_id:
        parts.append(f"↩post {post.reply_to_id}")
    if post.reactions:
        parts.append(f"+{post.reactions} reactions")
    if post.edited:
        parts.append(f"edited {_one_line(post.edited)}")
    meta = f" ({' · '.join(parts)})" if parts else ""
    body = compact_text(post.text).replace("\n", "\n    ")
    return f"[{_one_line(post.user)} #{post.number}]{meta}: {body}"


def format_gap(gap: Gap) -> str:
    noun = "post number" if gap.count == 1 else "post numbers"
    span = f"#{gap.start}" if gap.count == 1 else f"#{gap.start}–#{gap.end}"
    why = f", {_one_line(gap.reason)}" if gap.reason else ""
    return f"[... omitted {gap.count} {noun} ({span}){why} …]"


def with_gaps(posts: Iterable[Post], total: int | None = None, reason: str = "") -> list[Post | Gap]:
    """Sort posts by number and insert Gap markers wherever numbers are missing, including before
    the first post and, when `total` (the highest post number) is known, after the last."""
    ordered = sorted(posts, key=lambda p: p.number)
    out: list[Post | Gap] = []
    expected = 1
    for p in ordered:
        if p.number > expected:
            out.append(Gap(expected, p.number - 1, reason))
        out.append(p)
        expected = max(expected, p.number + 1)
    if total is not None and total >= expected:
        out.append(Gap(expected, total, reason))
    return out


def render_posts(items: Iterable[Post | Gap]) -> str:
    return "\n".join(format_gap(i) if isinstance(i, Gap) else format_post(i) for i in items)


def render_thread(title: str, items: list[Post | Gap], *, url: str | None = None,
                  total: int | None = None, notes: list[str] | None = None) -> str:
    """A thread header (title, url, how many posts are shown) followed by the posts."""
    shown = sum(1 for i in items if isinstance(i, Post))
    head = [f"# {_one_line(title)}"]
    info = [_one_line(url)] if url else []
    info.append(f"{shown} of {total} posts shown" if total is not None else f"{shown} posts shown")
    head.append(" · ".join(info))
    head.extend(sanitize_text(note).replace("\n", " ") for note in notes or [])
    return "\n".join(head) + "\n\n" + render_posts(items)
