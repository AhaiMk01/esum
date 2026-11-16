"""Paging arguments are checked before any request, and help shows real calls.

The behaviour is tested on the example world: `paged`, a synthetic site with the three paging flags,
and `demo`. One test is a contract over the registry instead: every built-in site that is installed
must refuse an out-of-range paging value before any request.
"""

import pytest

from esum import Tier, action, site
from esum.cli import main
from esum.core.registry import Registry


def run(capsys, monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["esum", *argv])
    code = main()
    return code, capsys.readouterr().out


@pytest.fixture
def paged(example_registry):
    """Add `paged` to the example world: a limit with a stated maximum, a page from 0 and a page from 1."""
    target = site(name="paged", base_url="https://paged.invalid", label="Paged demo site for tests")

    @action(target, tier=Tier.READ)
    def stories(ctx, limit: int = 30):
        """The newest stories.

        Args:
            limit: How many stories (max 100).
        """

    @action(target, tier=Tier.READ)
    def search(ctx, query: str, page: int = 0):
        """Search stories.

        Args:
            query: Words to look for.
            page: Result page, from 0.
        """

    @action(target, tier=Tier.READ)
    def listing(ctx, page: int = 1):
        """One page of a board.

        Args:
            page: Page, from 1.
        """

    example_registry.sites[target.name] = target
    return target


def test_the_message_says_the_range_and_the_value(capsys, paged, example_registry):
    assert main(["paged", "stories", "--limit=500"], registry=example_registry) == 2
    assert "error: esum paged stories: argument --limit: must be 1 to 100, got 500" in capsys.readouterr().out


def test_a_zero_based_page_accepts_zero_and_a_one_based_page_does_not(paged):
    params = {(a.name, p.name): p for a in paged.actions.values() for p in a.params}
    assert params["search", "page"].minimum == 0
    assert params["listing", "page"].minimum == 1


def test_action_help_shows_a_required_call_and_one_with_options(capsys, example_registry):
    assert main(["demo", "thread", "--help"], registry=example_registry) == 0
    lines = capsys.readouterr().out.splitlines()
    start = lines.index("examples:")
    assert lines[start + 1] == "  esum demo thread --id <int>"
    assert lines[start + 2] == "  esum demo thread --id <int> --mode medium --from <int>"


def test_action_help_without_arguments_shows_one_call(capsys, example_registry):
    assert main(["demo", "private", "--help"], registry=example_registry) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[lines.index("examples:") + 1:lines.index("examples:") + 3] == ["  esum demo private", ""]


# --- the contract every installed built-in site meets ------------------------------------------------

def paging_params():
    for site_ in Registry.discover(custom_root=None).sorted_sites():
        for act in site_.all_actions.values():
            for p in act.params:
                if p.name in ("limit", "offset", "page") and p.type is int:
                    yield site_, act, p


PAGING = list(paging_params())
REQUIRED = {"str": "x", "int": "1"}


def call(site_, act):
    flags = [part for p in act.params if p.required
             for part in (p.flag, str(p.choices[0]) if p.choices else REQUIRED[p.type.__name__])]
    return [site_.name, act.name, *flags]


def test_the_registry_yields_paging_flags_for_the_contract():
    """An empty parameter list would skip the contract below, and a world with no site would pass."""
    assert PAGING, "no installed built-in site declares a limit, offset or page flag: nothing was checked"


@pytest.mark.parametrize("site_, act, p", PAGING, ids=lambda v: getattr(v, "name", None))
def test_every_builtin_sites_paging_flags_refuse_an_out_of_range_value_before_any_request(capsys, monkeypatch,
                                                                                         site_, act, p):
    def no_request(*args, **kwargs):
        raise AssertionError("the action ran")

    monkeypatch.setattr(act, "func", no_request)
    code, out = run(capsys, monkeypatch, *call(site_, act), f"{p.flag}={p.minimum - 1}")
    assert code == 2 and (f"must be {p.minimum} to" in out or f"must be at least {p.minimum}" in out)
    if p.maximum is not None:
        code, out = run(capsys, monkeypatch, *call(site_, act), f"{p.flag}={p.maximum + 1}")
        assert code == 2 and f"must be {p.minimum} to {p.maximum}, got {p.maximum + 1}" in out
