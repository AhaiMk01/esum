"""Finds engines and sites. There is no central list: adding a module is enough (D-22).

Built-ins: every module under the `esum.engines` and `esum.sites` packages is imported (recursively;
`__init__.py` in subfolders is optional, names starting with `_` are skipped, and so is every folder
named `tests`, which holds a site's offline tests and fixtures), and every `Engine` / `Site` object
created in that module is registered. A folder under `esum/sites/` holding a `SITE.md`
is a Markdown site.

Custom sites (the user's own, no trust gate in v1, D-25):

    ~/.esum/sites/<engine>/<site>/site.py   a site config on a known engine
    ~/.esum/sites/<site>/site.py            a standalone module (may import siblings: `from . import x`)
    ~/.esum/sites/<site>/SITE.md            a Markdown-only site

Custom Python is imported lazily (D-25). Discovery only reads the folder layout (a custom module's
folder name is its site name) and parses SITE.md frontmatter; a custom `site.py` is imported only
when its site is looked up with `get()`. So `esum list` and every built-in site run with no custom
code in the process, where it could touch built-in writes or the write log.

A custom site never replaces a built-in one: a custom folder with a built-in site's name is
recorded as a problem and the built-in keeps running. Folders and modules that fail to load are
recorded as problems instead of breaking every other site.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from ..transport import SITE_EXTRAS
from .errors import ModuleFormatError
from .model import Engine, Site, check_format, _check_name
from .paths import custom_sites_dir

CUSTOM_PACKAGE = "esum_custom_sites"


@dataclass
class Problem:
    where: str
    error: str
    site: str | None = None  # the site name it stops, when the registry knows it


@dataclass
class PendingSite:
    """A custom module site known from its folder only; its code is imported on first `get()`."""

    name: str
    engine: str | None
    folder: Path
    builtin: bool = False
    kind: str = "module"
    transport: None = None
    base_url: None = None

    @property
    def source(self) -> Path:
        return self.folder / "site.py"

    @property
    def description(self) -> str:
        return f"custom module, loaded on use (`esum {self.name} --help`)"

    @property
    def kind_label(self) -> str:
        return self.engine or "standalone"


_loaded_custom: list[str] = []


def loaded_custom_modules() -> list[str]:
    """The custom site.py files imported into this process so far (recorded with every write)."""
    return list(_loaded_custom)


class Registry:
    def __init__(self) -> None:
        self.engines: dict[str, Engine] = {}
        self.sites: dict[str, Site] = {}            # loaded: built-in, Markdown, used custom sites
        self.pending: dict[str, PendingSite] = {}   # custom modules not imported yet
        self.builtins: dict[str, Site] = {}         # every built-in site
        self.problems: list[Problem] = []
        self._claimed: set[int] = set()             # ids of objects a module registered (or tried to)
        self._strays: dict[int, tuple[Engine | Site, str]] = {}  # created elsewhere, seen in a module

    # --- discovery -----------------------------------------------------------------------------

    @classmethod
    def discover(cls, *, engine_packages: tuple[str, ...] = ("esum.engines",),
                 site_packages: tuple[str, ...] = ("esum.sites",),
                 custom_root: Path | None | bool = True) -> "Registry":
        """Load built-in engines and sites, then find custom sites without importing them
        (`custom_root=True` means `~/.esum/sites`; None/False skips custom sites)."""
        reg = cls()
        for pkg in engine_packages:
            reg._scan_package(pkg)
        for pkg in site_packages:
            reg._scan_package(pkg, markdown=True)
        if custom_root is True:
            custom_root = custom_sites_dir()
        if custom_root:
            reg._scan_custom(Path(custom_root))
        reg._report_strays()
        reg._resolve()
        return reg

    def _scan_package(self, package: str, markdown: bool = False) -> None:
        try:
            pkg = importlib.import_module(package)
        except Exception as e:
            self.problems.append(Problem(package, f"{type(e).__name__}: {e}"))
            return
        for root in map(Path, getattr(pkg, "__path__", [])):
            for path in sorted(root.rglob("*.py")):
                rel = path.relative_to(root)
                if any(_skipped_folder(part) for part in rel.parts[:-1]):
                    continue
                if path.name.startswith("_") and path.name != "__init__.py":
                    continue
                parts = list(rel.with_suffix("").parts)
                if parts[-1] == "__init__":
                    parts.pop()
                if not parts:
                    continue  # the package's own __init__ (already imported)
                self._load_module(".".join([package, *parts]), builtin=True)
            if markdown:
                for md in sorted(root.rglob("SITE.md")):
                    if not any(_skipped_folder(p) for p in md.relative_to(root).parts[:-1]):
                        self._load_markdown(md, builtin=True)

    def _load_module(self, name: str, *, builtin: bool, expect_engine: str | None = None,
                     source: Path | None = None, expect_name: str | None = None) -> None:
        try:
            module = importlib.import_module(name)
        except Exception as e:
            self.problems.append(Problem(str(source or name), f"{type(e).__name__}: {e}"))
            return
        self._collect(module, builtin=builtin, expect_engine=expect_engine, source=source,
                      expect_name=expect_name)

    def _collect(self, module: ModuleType, *, builtin: bool, expect_engine: str | None,
                 source: Path | None, expect_name: str | None = None) -> None:
        objects = [v for v in vars(module).values() if isinstance(v, (Engine, Site))]
        found = [v for v in objects if v.origin == module.__name__]
        where = str(source or module.__name__)
        self._claimed.update(id(v) for v in found)
        for v in objects:
            if v.origin != module.__name__:
                self._strays.setdefault(id(v), (v, where))
        if expect_name is not None and not any(isinstance(v, Site) and v.name == expect_name
                                               for v in found):
            self.problems.append(Problem(where, f"defines no site named {expect_name!r} (a custom "
                                                f"site.py defines the site named after its folder)"))
            return
        if not found:
            return
        try:
            fmt = check_format(getattr(module, "FORMAT", None), where)
        except Exception as e:
            self.problems.append(Problem(where, str(e)))
            return
        ported = getattr(module, "PORTED_FROM", None)
        declared = getattr(module, "EXTRAS", None)
        if declared is not None and (not isinstance(declared, (tuple, list, set, frozenset))
                                     or not all(isinstance(k, str) for k in declared)):
            self.problems.append(Problem(where, f"EXTRAS must be a tuple of extras key names, got "
                                                f"{declared!r}"))
            declared = None
        for obj in found:
            obj.format = fmt
            obj.ported_from = ported
            if declared is not None:
                obj.extra_keys = frozenset(declared)
            if isinstance(obj, Engine):
                if obj.name in self.engines:
                    self.problems.append(Problem(where, f"engine {obj.name!r} is defined twice"))
                    continue
                self.engines[obj.name] = obj
                continue
            obj.builtin = builtin
            obj.source = source or Path(module.__file__ or "")
            if expect_name is not None and obj.name != expect_name:
                self.problems.append(Problem(where, f"defines site {obj.name!r}, but a custom site.py "
                                                    f"defines only the site named after its folder "
                                                    f"({expect_name!r})"))
                continue
            if expect_engine is not None and obj.engine != expect_engine:
                self.problems.append(Problem(where, f"site {obj.name!r} is under the {expect_engine!r} "
                                                    f"folder but declares engine={obj.engine!r}"))
                continue
            self._add_site(obj, where)

    def _add_site(self, obj: Site | PendingSite, where: str) -> None:
        existing = self.sites.get(obj.name)
        if existing is not None and existing.builtin and not obj.builtin:
            self.problems.append(Problem(where, f"{obj.name!r} is a built-in site, and a custom site "
                                                f"can't replace one; rename this folder to use it as "
                                                f"a site of its own"))
            return
        if obj.name in self.pending or existing is not None:
            self.problems.append(Problem(where, f"site {obj.name!r} is defined twice"))
            return
        if isinstance(obj, PendingSite):
            self.pending[obj.name] = obj
            return
        if obj.builtin:
            self.builtins[obj.name] = obj
        self.sites[obj.name] = obj

    def _scan_custom(self, root: Path) -> None:
        if not root.is_dir():
            return
        for entry in sorted(p for p in root.iterdir() if p.is_dir()):
            if entry.name.startswith((".", "_")):
                continue
            if entry.name in self.engines:
                for sub in sorted(p for p in entry.iterdir() if p.is_dir()):
                    if not sub.name.startswith((".", "_")):
                        self._find_custom_dir(sub, engine=entry.name)
            else:
                self._find_custom_dir(entry, engine=None)

    def _find_custom_dir(self, folder: Path, engine: str | None) -> None:
        """Register a custom folder without running any of its code."""
        if (folder / "site.py").is_file():
            try:
                name = _check_name(folder.name, "site")
            except Exception as e:
                self.problems.append(Problem(str(folder), f"{e} (a custom site's folder name is its "
                                                          f"site name)", site=folder.name))
                return
            self._add_site(PendingSite(name=name, engine=engine, folder=folder), str(folder))
        elif (folder / "SITE.md").is_file():
            self._load_markdown(folder / "SITE.md", builtin=False)
        else:
            self.problems.append(Problem(str(folder), "no site.py or SITE.md in this folder"))

    def _load_custom(self, pending: PendingSite) -> Site | None:
        """Import one custom site.py."""
        del self.pending[pending.name]
        folder, engine = pending.folder, pending.engine
        pkg_name = f"{CUSTOM_PACKAGE}.{_ident(engine or '_')}.{_ident(folder.name)}"
        _loaded_custom.append(str(pending.source))
        first_problem = len(self.problems)
        try:
            _ensure_package(CUSTOM_PACKAGE, None)
            _ensure_package(f"{CUSTOM_PACKAGE}.{_ident(engine or '_')}", None)
            _ensure_package(pkg_name, folder)
        except Exception as e:
            self.problems.append(Problem(str(folder), f"{type(e).__name__}: {e}"))
        else:
            self._load_module(f"{pkg_name}.site", builtin=False, expect_engine=engine,
                              source=pending.source, expect_name=pending.name)
            self._report_strays()
        for problem in self.problems[first_problem:]:
            problem.site = pending.name
        site = self.sites.get(pending.name)
        return site if site is not None and self._resolve_site(site) else None

    def _load_markdown(self, path: Path, *, builtin: bool) -> None:
        try:
            text = path.read_text(encoding="utf-8")
            meta = parse_frontmatter(text)
            raw = meta.get("format")
            fmt = check_format(int(raw) if raw and raw.isdigit() else raw, str(path))
            name = _check_name(meta.get("name") or path.parent.name, "site")
            if not meta.get("description"):
                raise ValueError("SITE.md frontmatter needs a description")
        except Exception as e:
            error = str(e) if isinstance(e, ModuleFormatError) else f"{type(e).__name__}: {e}"
            self.problems.append(Problem(str(path), error, site=path.parent.name))
            return
        site = Site(name=name, engine=None, base_url=meta.get("base_url"), transport=None,
                    label={"en": meta["description"]}, kind="markdown", builtin=builtin,
                    source=path, format=fmt, meta=meta, origin=str(path))
        self._add_site(site, str(path))

    def _report_strays(self) -> None:
        """An Engine/Site object a module holds but did not create registers nothing there. That is
        right for an import of an object its own module registers; anything else (a site built by a
        factory function in another module) would vanish silently, so it is a problem."""
        for key, (obj, where) in list(self._strays.items()):
            del self._strays[key]
            if key in self._claimed:
                continue
            kind = "engine" if isinstance(obj, Engine) else "site"
            self.problems.append(Problem(where, f"{kind} {obj.name!r} was created in module "
                                                f"{obj.origin!r}, which does not register it, so it is "
                                                f"ignored; pass origin=__name__ to {kind}() from the "
                                                f"module that should register it"))

    def _check_extras(self, site: Site) -> None:
        """Report extras keys nothing declares (a typo would silently fall back to the default).
        Keys every site may set (SITE_EXTRAS) plus the engine's and the site module's EXTRAS are
        known. A standalone site is checked only when its module declares EXTRAS."""
        if site.engine_obj is None and site.extra_keys is None:
            return
        engine_keys = site.engine_obj.extra_keys if site.engine_obj else frozenset()
        known = SITE_EXTRAS | engine_keys | (site.extra_keys or frozenset())
        unknown = sorted(set(site.extras) - known)
        if unknown:
            self.problems.append(Problem(str(site.source or site.origin),
                                         f"site {site.name!r}: unknown extras key(s) "
                                         f"{', '.join(map(repr, unknown))}; known: "
                                         f"{', '.join(sorted(known))}"))

    def _resolve_site(self, site: Site) -> bool:
        if site.kind != "module":
            return True
        if site.engine is None:
            self._check_extras(site)
            return True
        eng = self.engines.get(site.engine)
        if eng is None:
            self.problems.append(Problem(str(site.source or site.origin),
                                         f"site {site.name!r} needs engine {site.engine!r}, which is "
                                         f"not installed"))
            del self.sites[site.name]
            if site.builtin:
                self.builtins.pop(site.name, None)
            return False
        site.engine_obj = eng
        self._check_extras(site)
        return True

    def _resolve(self) -> None:
        for site in list(self.sites.values()):
            self._resolve_site(site)

    # --- lookup --------------------------------------------------------------------------------

    def get(self, name: str) -> Site | None:
        """The site called `name`. A custom module site is imported here, on first use."""
        pending = self.pending.get(name)
        if pending is not None:
            return self._load_custom(pending)
        return self.sites.get(name)

    def names(self) -> list[str]:
        return sorted(set(self.sites) | set(self.pending))

    def sorted_sites(self) -> list[Site | PendingSite]:
        """Every site, for listing. Imports nothing: a custom module not used yet is a
        `PendingSite` (name and engine folder only)."""
        return [self.pending.get(k) or self.sites[k] for k in self.names()]


def _skipped_folder(name: str) -> bool:
    """A folder the built-in scan never enters: private (`_x`), hidden, or a site's `tests/`."""
    return name.startswith(("_", ".")) or name == "tests"


def _ident(name: str) -> str:
    return re.sub(r"\W", "_", name)


def _ensure_package(name: str, folder: Path | None) -> ModuleType:
    """A synthetic package so custom site.py files can use relative imports of their siblings."""
    module = sys.modules.get(name)
    path = [str(folder)] if folder else []
    if module is None:
        spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
        spec.submodule_search_locations = path
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
    else:
        module.__path__ = path  # type: ignore[attr-defined]
        for sub in [k for k in sys.modules if k.startswith(name + ".")]:
            if folder is not None:
                del sys.modules[sub]  # reload a custom site from its (possibly new) folder
    return module


def parse_frontmatter(text: str) -> dict[str, str]:
    """`---` delimited `key: value` lines at the top of a Markdown file (no YAML dependency)."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return meta
        if ":" in line and not line.startswith((" ", "#")):
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip("\"'")
    return {}  # unterminated frontmatter


_registry: Registry | None = None


def get_registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = Registry.discover()
    return _registry
