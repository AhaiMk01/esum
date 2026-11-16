import pytest

from esum import FORMAT, Tier, Transport, action, engine, site
from esum.core.errors import ModuleFormatError
from esum.core.model import check_format, parse_docstring


def make_site(**kw):
    return site(**{"name": "x", "base_url": "https://x.invalid/", "label": "X", **kw})


def test_tier_three_cannot_exist():
    assert [t.value for t in Tier] == [0, 1, 2]
    with pytest.raises(ValueError):
        Tier(3)
    with pytest.raises(TypeError):
        class More(Tier):  # noqa: F841 - enums with members cannot be extended
            MONEY = 3
    s = make_site()
    with pytest.raises(ValueError, match="never allowed"):
        action(s, tier=3)
    with pytest.raises(TypeError):
        action(s, tier="read")


def test_tier_is_required():
    with pytest.raises(TypeError):
        action(make_site())  # type: ignore[call-arg]


def test_transports():
    assert {t.short for t in Transport} == {"T1", "T2", "T3"}
    with pytest.raises(TypeError):
        make_site(transport="direct")


@pytest.mark.parametrize("bad", ["file:///C:/Users", "ftp://x.invalid", "x.invalid", "https://", "javascript:alert(1)"])
def test_base_url_must_be_a_web_address(bad):
    """Requests are built on base_url, and curl_cffi (T2) would read a file:// one from disk."""
    with pytest.raises(ValueError, match="base_url must be an http:// or https:// URL"):
        make_site(base_url=bad)


def test_base_url_scheme_is_case_insensitive():
    assert make_site(base_url="HTTPS://X.invalid").base_url == "HTTPS://X.invalid"


def test_site_defaults_and_labels():
    s = make_site(label={"en": "English", "zh": "中文"})
    assert s.base_url == "https://x.invalid"
    assert s.transport is Transport.DIRECT
    assert s.description == "English"
    assert s.origin == __name__
    with pytest.raises(ValueError):
        make_site(name="Bad Name")
    with pytest.raises(ValueError):
        make_site(name="list")


def test_action_params_from_signature():
    s = make_site()

    from typing import Literal

    @action(s, tier=Tier.READ)
    def recent_dps(ctx, id: int, mode: Literal["medium", "full"] = "medium", from_: int | None = None,
                   fresh_only: bool = False, tags: list[str] | None = None, ratio: float = 0.5):
        """Recent data points.

        Longer description.

        Args:
            id: Topic id,
                continued here.
            from_: First post.
        """

    act = s.actions["recent-dps"]
    assert recent_dps.esum_action is act
    assert act.summary == "Recent data points."
    assert act.body == "Longer description."
    by = {p.name: p for p in act.params}
    assert [p.flag for p in act.params] == ["--id", "--mode", "--from", "--fresh-only", "--tags",
                                            "--ratio"]
    assert by["id"].required and by["id"].type is int
    assert by["id"].help == "Topic id, continued here."
    assert by["mode"].choices == ("medium", "full") and by["mode"].default == "medium"
    assert by["from_"].type is int and by["from_"].default is None and not by["from_"].required
    assert by["fresh_only"].type is bool
    assert by["tags"].is_list and by["tags"].type is str
    assert act.usage.startswith("--id <int> [--mode medium|full] [--from <int>]")


@pytest.mark.parametrize("src, match", [
    ("def f(id: int):\n    'Doc.'", "first parameter must be `ctx`"),
    ("def f(ctx, id):\n    'Doc.'", "needs a type annotation"),
    ("def f(ctx, id: int):\n    pass", "needs a docstring"),
    ("def f(ctx, json: bool = False):\n    'Doc.'", "global flag --json"),
    ("def f(ctx, d: dict):\n    'Doc.'", "unsupported annotation"),
    ("def f(ctx, *a: int):\n    'Doc.'", r"\*args"),
    ("def f(ctx, b: bool):\n    'Doc.'", "needs a default"),
])
def test_action_rejects_bad_signatures(src, match):
    ns: dict = {}
    exec(src, ns)
    with pytest.raises(TypeError, match=match):
        action(make_site(), tier=Tier.READ)(ns["f"])


def test_duplicate_action_rejected():
    s = make_site()

    def a(ctx):
        """Doc."""

    action(s, tier=Tier.READ)(a)
    with pytest.raises(ValueError, match="twice"):
        action(s, tier=Tier.READ)(a)


def test_engine_actions_merge_with_site_overrides():
    e = engine("eng", description="E")

    @action(e, tier=Tier.READ)
    def read(ctx):
        """Engine read."""

    @action(e, tier=Tier.READ)
    def other(ctx):
        """Engine other."""

    s = make_site(engine="eng")

    @action(s, tier=Tier.READ, name="read")
    def site_read(ctx):
        """Site read."""

    s.engine_obj = e
    merged = s.all_actions
    assert sorted(merged) == ["other", "read"]
    assert merged["read"].summary == "Site read."


def test_check_format():
    assert check_format(FORMAT, "m") == 1
    with pytest.raises(ModuleFormatError, match="no FORMAT"):
        check_format(None, "m")
    with pytest.raises(ModuleFormatError, match="upgrade esum"):
        check_format(FORMAT + 1, "m")
    with pytest.raises(ModuleFormatError, match="no longer supported"):
        check_format(0, "m")
    with pytest.raises(ModuleFormatError, match="integer"):
        check_format("1", "m")


def test_parse_docstring_without_args():
    assert parse_docstring("One line.") == ("One line.", "", {})
    assert parse_docstring(None) == ("", "", {})
