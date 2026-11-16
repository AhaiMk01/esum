"""Helpers the offline tests share: `tests/` here, and each built-in site's own `tests/` folder.

pytest puts `tests/` on `sys.path` (`pythonpath` in `pyproject.toml`), so both import this package as
`support`. An engine's test helpers are in `support/<engine>.py` and its scrubbed page captures in
`tests/fixtures/<engine>/`. A site config's tests may use both: a site needs its engine anyway. Nothing
here, and no test in `tests/`, depends on a built-in site.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def engine_fixtures(engine: str) -> Path:
    """The folder of an engine's own fixtures, `tests/fixtures/<engine>/`."""
    return FIXTURES / engine


@cache
def _builtin_sites() -> frozenset[str]:
    from esum.core.registry import Registry
    return frozenset(Registry.discover(custom_root=None).builtins)


def _missing(names: tuple[str, ...]) -> str | None:
    missing = [name for name in names if name not in _builtin_sites()]
    return "needs the built-in site " + ", ".join(missing) + ", which is not installed here" if missing else None


def needs_site(*names: str):
    """Mark a test that asserts a step on another built-in site (D-93): it runs only where that site is
    installed, and is skipped, naming the missing site, in a tree that leaves it out.

        @needs_site("aerolopa")
        def test_detail_points_to_aerolopa(...): ...
    """
    reason = _missing(names)
    return pytest.mark.skipif(reason is not None, reason=reason or "")


def require_site(*names: str) -> None:
    """The same inside a test, before its last assertions, when only those are about the other site:
    what the test asserted about its own site so far has run."""
    reason = _missing(names)
    if reason:
        pytest.skip(reason)


@pytest.fixture
def engine_sites(isolated):
    """`engine_sites("discuz", board='base_url="https://board.invalid", label="A test board"')` -> a
    registry holding the engines and those synthetic site configs on one of them, written as custom
    sites: each value is the rest of that site's `site(...)` call. No built-in site is in it, so an
    engine's tests can't lean on one. A test module imports this fixture by name."""
    import sys

    from esum.core.registry import Registry

    def make(engine: str, **configs: str) -> Registry:
        for name, arguments in configs.items():
            path = isolated / "sites" / engine / name / "site.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("from esum import Transport, site\n\nFORMAT = 1\n\n"
                            f"{name} = site(name={name!r}, engine={engine!r}, {arguments})\n", encoding="utf-8")
        return Registry.discover(site_packages=())

    yield make
    for name in [m for m in sys.modules if m.startswith("esum_custom_sites")]:
        del sys.modules[name]


def find_fixture(name: str, *folders: Path) -> Path:
    """`name` in the first of `folders` that has it: a site config's test passes its own
    `tests/fixtures/` and then its engine's, so it reads the engine's captures without a copy."""
    for folder in folders:
        if (folder / name).is_file():
            return folder / name
    raise FileNotFoundError(f"no fixture {name} in " + ", ".join(map(str, folders)))


# One plain synthetic site config per engine, for tests of what every site on an engine does.
ENGINE_WORLD = {
    "discourse": ("forum", 'base_url="https://forum.invalid", label="A test Discourse forum"'),
    "discuz": ("board", 'base_url="https://board.invalid", label="A test Discuz! board"'),
    "vbulletin": ("lounge", 'base_url="https://lounge.invalid/forum", label="A test vBulletin forum"'),
    "wordpress": ("blog", 'base_url="https://blog.invalid", label="A test WordPress blog"'),
}


@pytest.fixture
def engine_world(engine_sites, fake_http):
    """(registry, backend) like the root conftest's `offline_site`, but the registry holds the engines
    and `forum`, `board`, `lounge` and `blog`, one synthetic site on each, and no built-in site."""
    registry = None
    for engine, (name, arguments) in ENGINE_WORLD.items():
        registry = engine_sites(engine, **{name: arguments})
    return registry, fake_http
