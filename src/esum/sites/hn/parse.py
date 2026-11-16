"""Pure parsers for Hacker News responses (Algolia search and items, Firebase items).

A thread is the story (post #1) followed by its comments in time order (#2, #3, …): new comments
only ever append, so post numbers stay stable between reads and `--from/--to` ranges keep pointing at
the same comments. `↩#M` shows which comment a reply answers; replies to the story itself carry no
`↩`, because every comment answers the story in the end.

Algolia's tree leaves out a dead comment and everything under it, while the item page lists them. The
page is the only cheap way to find those ids: Firebase can't look up replies, and walking `kids`
costs one request per comment.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from esum import Post, html_to_text, html_to_label, clip, one_line

ITEM_URL = "https://news.ycombinator.com/item?id="
DELETED = "[deleted]"
DEAD = "[dead comment]"
UNREAD = "[comment not read]"
HIDDEN_TEXT = ("", "[flagged]", "[dead]")   # what Firebase gives a guest for a newer dead comment


def title_text(value: str | None) -> str:
    """A title as text. HN's API serves titles as HTML, so old ones carry entities such as `&#34;`."""
    return html.unescape(value or "")


def when(seconds: int | None, iso: str | None = None) -> datetime | None:
    """A Unix time (Firebase `time`, Algolia `created_at_i`), falling back to an ISO string."""
    if seconds:
        return datetime.fromtimestamp(int(seconds), tz=timezone.utc)
    if iso:
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


@dataclass
class Story:
    """One row of a search result or a feed."""

    id: int
    title: str
    author: str | None
    date: datetime | None
    points: int | None
    comments: int | None       # None for a comment, which has no count
    url: str | None            # the linked article; None for Ask HN and text posts
    text: str = ""             # a comment's text (comment search) or a text post's body

    @property
    def discussion(self) -> str:
        return f"{ITEM_URL}{self.id}"

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "author": self.author,
                "date": self.date.isoformat() if self.date else None, "points": self.points,
                "comments": self.comments, "url": self.url, "discussion": self.discussion,
                "text": self.text}


def story_from_hit(hit: dict) -> Story:
    """An Algolia search hit (a story, or a comment with its story's title)."""
    is_comment = hit.get("comment_text") is not None
    title = title_text(hit.get("title") or hit.get("story_title"))
    if is_comment:
        title = f"comment on: {title}" if title else "comment"
    return Story(id=int(hit["objectID"]), title=title, author=hit.get("author"),
                 date=when(hit.get("created_at_i"), hit.get("created_at")),
                 points=hit.get("points"), comments=None if is_comment else hit.get("num_comments") or 0,
                 url=hit.get("url") or (hit.get("story_url") if is_comment else None),
                 text=html_to_text(hit.get("comment_text") or hit.get("story_text") or ""))


def story_from_item(item: dict) -> Story:
    """A Firebase item (`/v0/item/<id>.json`)."""
    return Story(id=int(item["id"]), title=title_text(item.get("title")), author=item.get("by"),
                 date=when(item.get("time")), points=item.get("score"),
                 comments=item.get("descendants") or 0, url=item.get("url"),
                 text=html_to_text(item.get("text") or ""))


def story_row(s: Story) -> dict:
    """A list row: `to_dict` with one-line title and author (the table shortens the rest)."""
    row = s.to_dict()
    row.update(title=one_line(s.title), author=one_line(s.author) or DELETED)
    return row


@dataclass
class Thread:
    id: int
    title: str
    url: str
    link: str | None           # the story's external article, if any
    posts: list[Post]          # every post, numbered 1..n
    story_id: int | None = None
    parent_id: int | None = None


def tree_comments(root: dict) -> list[dict]:
    """Every comment in an Algolia item tree. A null-author node stays only if it has replies, as a
    placeholder they can point at."""
    comments: list[dict] = []

    def walk(children: list[dict] | None) -> None:
        for c in children or []:
            kids = c.get("children") or []
            if c.get("author") or c.get("text") or kids:
                comments.append(c)
            walk(kids)

    walk(root.get("children"))
    return comments


def page_rows(page: str, root_id: int) -> list[dict]:
    """The comment rows of an item page, in Algolia's shape, with parents from the indentation.

    A row gives identity and placement; its text comes from Firebase. HN no longer pages comments
    (`p=2` returned the same 1,862 rows, measured 2026-11-01), so one page holds every row.
    """
    rows = []
    path = [root_id]
    for part in re.split(r'(?=<tr class="athing comtr)', page)[1:]:
        row = re.match(r'<tr class="athing comtr[^"]*" id="(\d+)">', part)
        indent = re.search(r'indent="(\d+)"', part)
        if not row or not indent:
            continue
        del path[int(indent[1]) + 1:]
        author = re.search(r'href="user\?id=([^"&]+)"', part)
        age = re.search(r'class="age" title="([^" ]+)', part)
        posted = when(None, f"{age[1]}+00:00") if age else None
        rows.append({"id": int(row[1]), "parent_id": path[-1], "author": author[1] if author else None,
                     "created_at_i": int(posted.timestamp()) if posted else None})
        path.append(int(row[1]))
    return rows


def comment_from_item(item: dict) -> dict:
    """A Firebase comment in Algolia's shape. A dead comment says so, with its text only if given."""
    text = item.get("text") or ""
    if item.get("deleted"):
        text = ""
    elif item.get("dead"):
        text = (f"{DEAD} {text}" if text not in HIDDEN_TEXT
                else f"{DEAD} a guest can't read its text; the site shows {text or 'nothing'}")
    return {"id": item["id"], "parent_id": item.get("parent"), "author": item.get("by"), "text": text,
            "created_at_i": item.get("time")}


def thread_from_item(root: dict, *, firebase: dict | None = None, options: list[dict] | None = None,
                     restored: list[dict] | None = None) -> Thread:
    """An Algolia item tree (`/api/v1/items/<id>`) as numbered posts. `restored` adds comments the
    tree leaves out, in the tree's shape."""
    comments = [*tree_comments(root), *(restored or [])]
    comments.sort(key=lambda c: (c.get("created_at_i") or 0, c.get("id") or 0))

    root_id = int(root["id"])
    number = {root_id: 1}
    for i, c in enumerate(comments, 2):
        number[int(c["id"])] = i

    is_comment = root.get("type") == "comment"
    is_pollopt = root.get("type") == "pollopt"
    author = root.get("author")
    op = author if not is_comment and not is_pollopt else None
    link = root.get("url")
    source = firebase if firebase is not None else root
    body = html_to_text(source.get("text") or "")
    score = source.get("score") if firebase is not None else root.get("points")
    title = title_text(root.get("title")) or clip(html_to_label(source.get("text") or "", keep_links=False), 80) or f"Item {root_id}"
    if is_pollopt and score is not None:
        body = f"{body} ({score} votes)"
    option_lines = []
    for option in options or []:
        label = html_to_label(option.get("text") or "", keep_links=False)
        votes = f" ({option['score']} votes)" if option.get("score") is not None else ""
        option_lines.append(f"- {label}{votes} — {ITEM_URL}{option['id']}")
    op_text = "\n\n".join(t for t in (f"Link: {link}" if link else "", body,
                                      "\n".join(option_lines)) if t)
    posts = [Post(1, author or DELETED, op_text or "(no text)",
                  date=when(root.get("created_at_i"), root.get("created_at")),
                  likes=None if is_comment else score, is_op=bool(op), url=f"{ITEM_URL}{root_id}")]
    for c in comments:
        parent = number.get(int(c.get("parent_id") or 0))
        posts.append(Post(number[int(c["id"])], c.get("author") or DELETED,
                          html_to_text(c.get("text") or "") or DELETED,
                          date=when(c.get("created_at_i"), c.get("created_at")),
                          likes=None,
                          is_op=bool(op) and c.get("author") == op,
                          reply_to=parent if parent and parent != 1 else None,
                          url=f"{ITEM_URL}{c['id']}"))
    return Thread(id=root_id, title=title, url=f"{ITEM_URL}{root_id}", link=link, posts=posts,
                  story_id=root.get("story_id"), parent_id=root.get("parent_id"))
