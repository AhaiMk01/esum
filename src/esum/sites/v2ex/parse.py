"""Pure parsers for V2EX: the public v1 API (topics, replies, nodes) and the sov2ex search index.

The v1 API returns numbers as strings ("replies": "94") and times as Unix seconds in strings, so
every field goes through `num`. A thread is the topic (post #1) followed by its replies in floor
order: V2EX floor N is post #N+1 here. Replies name the post they answer as `@user` or `@user #N`
at the start; `reply_to` follows that convention.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from esum import Post, html_to_text, inner_div, one_line

TOPIC_URL = "https://www.v2ex.com/t/"
DELETED = "[deleted]"
MENTION = re.compile(r"^\s*@([A-Za-z0-9_-]+)(?:\s*#(\d+))?")


def num(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def from_unix(value) -> datetime | None:
    seconds = num(value)
    return datetime.fromtimestamp(seconds, tz=timezone.utc) if seconds else None


def from_index_time(value) -> datetime | None:
    """sov2ex times: ISO without a zone (`2014-05-29T23:51:36`), in UTC."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def body_of(item: dict) -> str:
    """Rendered HTML when the API has it (keeps links and line breaks), else the raw Markdown."""
    return html_to_text(item.get("content_rendered") or "") or (item.get("content") or "").strip()


@dataclass
class Topic:
    """One row of a topic list or a search result."""

    id: int
    title: str
    author: str | None
    node: str | None           # node name (`python`); None in search results, which carry only its id
    date: datetime | None
    replies: int
    text: str = ""

    @property
    def url(self) -> str:
        return f"{TOPIC_URL}{self.id}"

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "author": self.author, "node": self.node,
                "date": self.date.isoformat() if self.date else None, "replies": self.replies,
                "url": self.url, "text": self.text}


def topic_from_api(t: dict) -> Topic:
    """A topic from `topics/hot|latest|show.json`."""
    return Topic(id=num(t.get("id")), title=t.get("title") or "", author=(t.get("member") or {}).get("username"),
                 node=(t.get("node") or {}).get("name"), date=from_unix(t.get("created")),
                 replies=num(t.get("replies")), text=body_of(t))


def topic_from_hit(hit: dict) -> Topic:
    """A sov2ex search hit (`hits[]._source`, plus `_id`)."""
    s = hit.get("_source") or {}
    return Topic(id=num(s.get("id") or hit.get("_id")), title=s.get("title") or "",
                 author=s.get("member") or None, node=None, date=from_index_time(s.get("created")),
                 replies=num(s.get("replies")), text=(s.get("content") or "").strip())


def search_row(t: Topic) -> dict:
    """A list row for a sov2ex hit, without its reply count: the index stores it at crawl time, so
    a topic with 42 replies shows 0."""
    return {k: v for k, v in topic_row(t).items() if k != "replies"}


def topic_row(t: Topic) -> dict:
    """A list row: `to_dict` with one-line names (the table shortens dates and text)."""
    row = t.to_dict()
    row.update(title=one_line(t.title), author=one_line(t.author) or DELETED, node=one_line(t.node) or None)
    return row


@dataclass
class Thread:
    id: int
    title: str
    node: str | None
    url: str
    posts: list[Post]          # every post, numbered 1..n


def reply_target(text: str, number: int, posts: list[Post]) -> int | None:
    """The post a reply answers, from a leading `@user #N` (floor N) or `@user` (their latest post)."""
    m = MENTION.match(text)
    if not m:
        return None
    if m.group(2):
        target = num(m.group(2)) + 1
        return target if 1 < target < number else None
    for p in reversed(posts):
        if p.user == m.group(1):
            return p.number if p.number != 1 else None
    return None


def thread_from(topic: dict, replies: list[dict]) -> Thread:
    """`topics/show.json?id=` (its one topic) plus `replies/show.json?topic_id=` (every reply)."""
    tid = num(topic.get("id"))
    op = (topic.get("member") or {}).get("username")
    url = f"{TOPIC_URL}{tid}"
    posts = [Post(1, op or DELETED, body_of(topic) or "(no text)", date=from_unix(topic.get("created")),
                  is_op=True, url=url, likes=None)]
    for floor, r in enumerate(replies, 1):
        number = floor + 1
        text = body_of(r) or DELETED
        user = (r.get("member") or {}).get("username") or DELETED
        posts.append(Post(number, user, text, date=from_unix(r.get("created")),
                          is_op=bool(op) and user == op, reply_to=reply_target(r.get("content") or text, number, posts),
                          url=f"{url}#r_{r.get('id')}", likes=None))
    return Thread(id=tid, title=topic.get("title") or f"Topic {tid}",
                  node=(topic.get("node") or {}).get("name"), url=url, posts=posts)


@dataclass
class Supplement:
    number: int
    date: str
    text: str


def supplements(html: str) -> list[Supplement]:
    """附言, which the public API leaves out: the page's `subtle` blocks after the topic. They can
    hold more than the topic itself, such as how the story ended."""
    out = []
    for n, m in enumerate(re.finditer(r'<div class="subtle">.*?title="([^"]+)".*?(<div class="topic_content">)',
                                      html or "", re.S), 1):
        when = datetime.fromisoformat(m.group(1).replace(" +", "+")).astimezone(timezone.utc)
        out.append(Supplement(n, f"{when:%Y-%m-%d %H:%M}", html_to_text(inner_div(html, m.start(2)))))
    return out


def add_supplements(thread: Thread, found: list[Supplement]) -> None:
    first = thread.posts[0]
    first.text = "\n".join([first.text, *(f"[supplement {s.number}, {s.date}]\n{s.text}" for s in found)])


def match_nodes(nodes: list[dict], query: str | None) -> list[dict]:
    """Nodes whose name or title contains `query` (case-insensitive), busiest first."""
    q = (query or "").casefold()
    hits = [n for n in nodes if not q or q in (n.get("name") or "").casefold()
            or q in (n.get("title") or "").casefold()]
    return sorted(hits, key=lambda n: -num(n.get("topics")))
