"""Which posts of a thread to read: `medium` (head + tail), `full`, or an explicit `--from/--to` range.

Numbered-page forums use `read_pages` to fetch missing pages and deduplicate floors.
API readers can use `select_numbers`, `to_spans` and `chunked` independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, Iterable, Iterator, Literal, Mapping, Sequence, TypeVar

from .errors import ArgumentError

Mode = Literal["medium", "full"]
"""Use as an action argument type: `mode: Mode = "medium"`."""

HEAD = 30
TAIL = 30

RANGE_CHUNK = 300
"""Posts per suggested next-range read (`ThreadView.range_steps`)."""
RANGE_RESET = {"mode": "medium", "since": None, "posts": None}
"""Common thread flags that override `--from/--to`, cleared in a suggested range read (names an
action doesn't have are ignored). A site with other such flags clears them in its own step args."""

T = TypeVar("T")


class RangePastEnd(ArgumentError):
    """`--from` beyond the thread's highest post number. The runner adds the `help:` command that
    reads the last posts, since only it knows the call's other arguments."""

    def __init__(self, highest: int, start: int):
        super().__init__(f"the thread ends at #{highest}; --from {start} is past the end")
        self.last_range = {"from_": max(1, highest - TAIL + 1), "to": highest}


def select_numbers(total: int, mode: str = "medium", from_: int | None = None,
                   to: int | None = None, head: int = HEAD, tail: int = TAIL) -> list[int]:
    """Post numbers (1-based, ascending) to read from a thread whose highest post number is `total`.

    `from_`/`to` (inclusive, either may be omitted) override `mode`. `medium` is the first `head`
    and the last `tail` posts; `full` is everything. An inverted range is an ArgumentError, and a
    `from_` past `total` (in a nonempty thread) a RangePastEnd.
    """
    for flag, value in (("--from", from_), ("--to", to)):
        if value is not None and value < 0:
            raise ArgumentError(f"{flag} {value} is negative: post numbers start at 1",
                                help=f"Give {flag} a post number of 1 or more")
    if from_ is not None and to is not None and from_ > to:
        raise ArgumentError(f"--from {from_} is after --to {to}", help="Give --from less than or equal to --to")
    if total <= 0:
        return []
    if from_ is not None and from_ > total:
        raise RangePastEnd(total, from_)
    if from_ is not None or to is not None:
        lo = max(1, from_ if from_ is not None else 1)
        hi = min(total, to if to is not None else total)
        return list(range(lo, hi + 1))
    if mode == "full":
        return list(range(1, total + 1))
    if mode != "medium":
        raise ValueError(f"unknown mode {mode!r}; use 'medium' or 'full'")
    return sorted(set(range(1, min(head, total) + 1)) | set(range(max(1, total - tail + 1), total + 1)))


def to_spans(numbers: Iterable[int]) -> list[tuple[int, int]]:
    """Collapse sorted numbers into inclusive (start, end) spans: [1,2,3,7,8] -> [(1,3),(7,8)]."""
    spans: list[tuple[int, int]] = []
    for n in sorted(set(numbers)):
        if spans and n == spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], n)
        else:
            spans.append((n, n))
    return spans


def chunked(seq: Sequence[T], size: int) -> Iterator[list[T]]:
    """Split a sequence into lists of at most `size` items (e.g. post ids per request)."""
    if size <= 0:
        raise ValueError("size must be positive")
    for i in range(0, len(seq), size):
        yield list(seq[i:i + size])


P = TypeVar("P")


@dataclass
class PageRead(Generic[P, T]):
    pages: dict[int, P]
    floors: dict[int, T]
    unavailable: list[int]


def read_pages(load: Callable[[int], P | None], loaded: Mapping[int, P | None],
               wanted: Iterable[int], page_of: Callable[[int], int],
               floors: Callable[[P], Iterable[tuple[int, T]]]) -> PageRead[P, T]:
    """Fetch missing pages once and keep the first item at each floor, in page order.

    `loaded` includes any pages already probed; None marks an unavailable page.
    `floors` exposes each page's numbered items, including unselected quote context.
    Selection, page bounds and site-specific loader policies belong to the caller.
    """
    pages = dict(loaded)
    for n in sorted({page_of(f) for f in wanted} - pages.keys()):
        pages[n] = load(n)
    found: dict[int, T] = {}
    for n in sorted(pages):
        if (page := pages[n]) is not None:
            for number, item in floors(page):
                found.setdefault(number, item)
    return PageRead({n: p for n, p in pages.items() if p is not None}, found,
                    [n for n in sorted(pages) if pages[n] is None])
