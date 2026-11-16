from dataclasses import dataclass

import pytest

from esum import read_pages


@dataclass
class Floor:
    number: int
    text: str = ""


def reader(routes, wanted, loaded=None):
    asked = []

    def load(n):
        asked.append(n)
        return routes[n]

    result = read_pages(load, {1: routes[1]} if loaded is None else loaded, wanted,
                        lambda n: (n - 1) // 10 + 1,
                        lambda page: ((f.number, f) for f in page))
    return result, asked


@pytest.mark.parametrize(("lo", "hi", "asked"), [(1, 10, []), (10, 11, [2]), (20, 21, [2, 3]),
                                              (21, 21, [3])])
def test_page_boundaries_and_last_floor(lo, hi, asked):
    routes = {n: [Floor(f) for f in range((n - 1) * 10 + 1, min(n * 10, 21) + 1)]
              for n in (1, 2, 3)}
    result, requests = reader(routes, range(lo, hi + 1))
    assert requests == asked
    assert [n for n in result.floors if lo <= n <= hi] == list(range(lo, hi + 1))


def test_already_loaded_pages_are_not_fetched_again_or_mutated():
    loaded = {3: [Floor(21)], 1: [Floor(1)]}
    result, asked = reader({2: [Floor(11)]}, [11, 21, 21], loaded)
    assert asked == [2]
    assert list(result.floors) == [1, 11, 21]
    assert set(loaded) == {1, 3}


def test_moved_floors_keep_first_copy_and_unselected_quote_context():
    result, asked = reader({1: [Floor(1), Floor(11, "first")],
                            2: [Floor(11, "moved"), Floor(12)]}, [11, 12])
    assert asked == [2]
    assert [(n, f.text) for n, f in result.floors.items()] == [(1, ""), (11, "first"), (12, "")]


def test_empty_pages_and_empty_selection():
    result, asked = reader({1: [], 2: []}, [11])
    assert result.floors == {} and asked == [2]
    assert result.unavailable == []
    result, asked = reader({1: [Floor(0)]}, [])
    assert list(result.floors) == [0] and asked == []


def test_unavailable_pages_are_reported_once_and_later_pages_still_load():
    result, asked = reader({1: [Floor(1)], 2: None, 3: [Floor(21)]}, [11, 11, 21])
    assert asked == [2, 3]
    assert result.unavailable == [2]
    assert set(result.pages) == {1, 3}
    assert list(result.floors) == [1, 21]


def test_already_unavailable_page_is_not_retried():
    result, asked = reader({}, [11, 11], {1: [], 2: None})
    assert asked == [] and result.unavailable == [2]


def test_loader_errors_propagate():
    def load(n):
        raise RuntimeError("site refused")

    with pytest.raises(RuntimeError, match="site refused"):
        read_pages(load, {}, [11], lambda n: 2, lambda page: {})


def test_duplicate_floor_on_one_page_keeps_first_occurrence():
    result, asked = reader({1: [Floor(1, "first"), Floor(1, "repeat")]}, [1])
    assert result.floors[1].text == "first" and asked == []
