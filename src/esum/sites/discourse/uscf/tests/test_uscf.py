"""USCardForum (uscf, on the Discourse engine): what its config declares, on the engine's fake forum
(`support.discourse`, answering from tests/fixtures/discourse/) at this site's address. The engine's own
tests are in the checkout's tests/test_parsers_discourse.py and tests/test_discourse_*.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from esum.core.errors import EXIT_LOGIN_NEEDED, EXIT_OK
from esum.core.registry import Registry
from esum.sites.discourse.uscf import CATEGORIES

from support.discourse import KEY, TOPIC, cli, json_resp, load, log_in, site_fake, terminal_is_stdin  # noqa: F401

USCF = "https://www.uscardforum.com"


@pytest.fixture
def forum_registry() -> Registry:
    """What the engine's `cli` fixture runs: here the built-in sites, uscf among them."""
    return Registry.discover(custom_root=None)


@pytest.fixture
def uscf(site_fake):
    """The fake forum at uscf's address; `.used` records each client's transport and cookie policy."""
    return site_fake(USCF)


def test_uscf_site_config(forum_registry):
    s = forum_registry.sites["uscf"]
    assert s.transport.value == "browser_http" and s.base_url == USCF
    assert s.extras["browser_cookies"] is False and s.extras["write_transport"] == "in_page"
    assert {"search", "thread", "categories", "bookmark"} <= set(s.all_actions)


def test_uscf_static_categories_need_no_request(cli, uscf):
    code, out, _ = cli("uscf", "categories")
    lines = out.splitlines()
    assert code == EXIT_OK and f"categories[{len(CATEGORIES)}]{{id,path,slug}}:" in lines
    assert "  5,玩卡 / 信用卡,credit-cards" in lines
    assert uscf.requests == []


def test_uscf_reads_over_cookieless_t2_and_bookmarks_over_t3(cli, uscf):
    assert cli("uscf", "thread", "--id", str(TOPIC))[0] == EXIT_OK
    assert uscf.used == [("T2", False)]  # no browser for reads
    assert cli("uscf", "bookmark", "--topic", str(TOPIC))[0] == EXIT_OK
    assert set(uscf.used[1:]) == {("T3", True)}
    assert all(r.url.startswith(USCF) for r in uscf.requests)
    assert any(r.method == "POST" for r in uscf.requests)


def test_uscf_category_names_in_listings(cli, uscf):
    code, out, _ = cli("uscf", "latest", "--category", "credit-cards")
    assert code == EXIT_OK and "category: 玩卡 / 信用卡" in out.splitlines()
    assert uscf.requests[-1].url == f"{USCF}/c/5/l/latest.json"


def test_searching_a_member_only_category_as_a_guest_is_login_needed(cli, uscf):
    code, out, _ = cli("uscf", "search", "--query", "Hyatt", "--category", "钛金")
    assert code == EXIT_LOGIN_NEEDED and "钛金 is a member-only category" in out
    assert not any("/search.json" in p for p in uscf.paths())


def test_forum_wide_reads_say_a_guest_sees_less(cli, uscf):
    note = "scope: \"guest view: member-only categories are not included\""
    assert note in cli("uscf", "search", "--query", "bookmark reminder")[1].splitlines()
    assert note in cli("uscf", "posts", "--limit", "1")[1].splitlines()
    assert note not in cli("uscf", "search", "--query", "x", "--category", "信用卡")[1].splitlines()


def test_posts_rows_name_their_category(cli, uscf):
    data = load("latest_posts.json")["latest_posts"]
    data[0]["category_id"] = 5
    url = f"{USCF}/posts.json"
    uscf.overrides[url] = json_resp(url, {"latest_posts": data})
    out = cli("--json", "uscf", "posts", "--limit", "1")[1]
    assert json.loads(out)["data"]["posts"][0]["category"] == "玩卡 / 信用卡"


def test_top_and_hot_say_a_guest_sees_less(cli, uscf):
    note = "scope: \"guest view: member-only categories are not included\""
    assert note in cli("uscf", "top")[1].splitlines() and note in cli("uscf", "hot")[1].splitlines()


# --- with a saved User API Key (D-46) ------------------------------------------------------------------

def test_a_read_key_leaves_the_bookmark_on_t3_without_the_key(cli, uscf, monkeypatch):
    log_in(cli, monkeypatch, "uscf", "read")
    uscf.used.clear()
    uscf.requests.clear()
    assert cli("uscf", "bookmark", "--topic", str(TOPIC))[0] == EXIT_OK
    assert set(uscf.used) == {("T3", True)}
    assert not any("User-Api-Key" in r.headers for r in uscf.requests)   # never into a page


def test_a_write_key_bookmarks_over_t2_without_a_browser_or_csrf(cli, uscf, monkeypatch):
    log_in(cli, monkeypatch, "uscf", "read,write")
    uscf.used.clear()
    uscf.requests.clear()
    assert cli("uscf", "bookmark", "--topic", str(TOPIC))[0] == EXIT_OK
    assert ("T3", True) not in uscf.used
    post = next(r for r in uscf.requests if r.method == "POST")
    assert post.headers["User-Api-Key"] == KEY and "X-CSRF-Token" not in post.headers
    assert "/session/csrf.json" not in uscf.paths()


def test_bookmark_reads_key_file_once(cli, uscf, monkeypatch, isolated):
    log_in(cli, monkeypatch, "uscf", "read,write")
    key_file = isolated / "auth" / "uscf.json"
    reads = []
    read_text = Path.read_text

    def tracked(path, *args, **kwargs):
        if path == key_file:
            reads.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracked)
    assert cli("uscf", "bookmark", "--topic", str(TOPIC))[0] == EXIT_OK
    assert reads == [key_file]
