"""Assemble fetched thread posts into one view, with explicit numbering and read coverage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .output import Next, Result
from .paging import RANGE_CHUNK, RANGE_RESET, to_spans
from .posts import Gap, Post, render_thread


@dataclass
class ThreadView:
    items: list[Post | Gap]
    count: int

    def range_steps(self, args: dict | None = None, chunk: int = RANGE_CHUNK) -> list[Next]:
        """`thread --from/--to` steps for up to two `not read` (or `before …`) gaps, forward reads
        before backward ones (paging forward, the posts before a range are usually read), at most
        `chunk` posts each. A gap with no shown post before it, or a `before` cutoff gap, is read
        backwards from its end (the posts just before what was shown); any other gap forwards from
        its start. `args` override the step's arguments; the runner fills in the rest of this
        call's (the id), and RANGE_RESET clears flags that would override a range."""
        steps: list[Next] = []
        seen_post = False
        for item in self.items:
            if isinstance(item, Post):
                seen_post = seen_post or item.number > 0
                continue
            if not (item.reason in ("", "not read") or item.reason.startswith("before ")):
                continue
            if not seen_post or item.reason.startswith("before "):
                lo, hi = max(item.start, item.end - chunk + 1), item.end
                purpose = "read the posts before these"
            else:
                lo, hi = item.start, min(item.end, item.start + chunk - 1)
                later = any(isinstance(i, Post) and i.number > item.end for i in self.items)
                purpose = "read the posts left out" if later else "read the posts after these"
            steps.append(Next("thread", {**RANGE_RESET, **(args or {}), "from_": lo, "to": hi}, purpose))
        # forward steps first: an agent paging through a thread has usually read what comes
        # before its range already, so "read the posts before these" is the less likely next read
        steps.sort(key=lambda step: step.purpose == "read the posts before these")
        return steps[:2]

    def result(self, title: str, *, url: str | None = None, data: dict | None = None,
               notes: list[str] | None = None, suffix: str = "",
               next: list[Next] | None = None) -> Result:
        """Use the same posts and count for text, JSON and the spill summary. `next` is usually
        `range_steps()`, plus any site-specific steps."""
        shown = sum(isinstance(p, Post) for p in self.items)
        summary = "\n".join([title, *([url] if url else []), *(notes or []),
                             f"{shown} of {self.count} posts shown"])
        return Result(data={**(data or {}), "posts": self.items},
                      text=render_thread(title, self.items, url=url, total=self.count, notes=notes) + suffix,
                      summary=summary, next=next or [])


def read_spans(numbers: Iterable[int], highest: int) -> list[tuple[int, int, str]]:
    """Missing numbers in selected spans are deleted or hidden; the rest were not read."""
    return [*((a, min(b, highest), "deleted or hidden")
              for a, b in to_spans(numbers) if a <= highest), (1, highest, "not read")]


def before_span(cutoff: datetime, last: int) -> tuple[int, int, str]:
    """A missing-number span before a cutoff, labelled in UTC."""
    cutoff = cutoff.replace(tzinfo=timezone.utc) if cutoff.tzinfo is None else cutoff.astimezone(timezone.utc)
    return (1, last, f"before {cutoff:%Y-%m-%d %H:%M} UTC")


def thread_view(posts: Iterable[Post], spans: Iterable[tuple[int, int, str]], *,
                count: int | None = None) -> ThreadView:
    """Render exactly these posts over the domain bounded by posts and inclusive spans.

    The first span covering a missing number supplies its reason; uncovered numbers
    default to not read. Adjacent gaps with the same reason merge. Empty/reversed
    spans are ignored. Count defaults to the domain size, independently of gaps.
    """
    selected = {p.number: p for p in posts}
    spans = [(a, b, reason) for a, b, reason in spans if a <= b]
    cuts = {n for a, b, _ in spans for n in (a, b + 1)}
    cuts.update(n for number in selected for n in (number, number + 1))
    boundaries = sorted(cuts)
    items: list[Post | Gap] = []
    for a, b in zip(boundaries, boundaries[1:]):
        if a in selected:
            items.append(selected[a])
        else:
            why = next((reason for lo, hi, reason in spans if lo <= a <= hi), "not read")
            if items and isinstance(items[-1], Gap) and items[-1].end == a - 1 and items[-1].reason == why:
                items[-1].end = b - 1
            else:
                items.append(Gap(a, b - 1, why))
    size = boundaries[-1] - boundaries[0] if boundaries else 0
    return ThreadView(items, count if count is not None else size)
