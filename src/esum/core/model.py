"""Module-format primitives (module format v1): tiers, transports, engines, sites and actions.

Engines and sites are plain objects created by `engine()` / `site()` at module level; the registry
finds them by scanning modules, so nothing is registered globally at import time. `@action` attaches
an action to an engine or a site and derives CLI flags from the function's typed signature.
"""

from __future__ import annotations

import enum
import inspect
import re
import sys
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from .errors import ModuleFormatError
from .urls import is_web_url

FORMAT = 1
"""The module-format version this ESUM implements. Modules declare `FORMAT = 1` at module level."""

SUPPORTED_FORMATS = frozenset({1})


class Tier(enum.IntEnum):
    """What an action may change. Tier 3 (moves money or points) does not exist, on purpose:
    `Tier(3)` raises, and an enum with members cannot be subclassed to add it."""

    READ = 0
    REVERSIBLE = 1
    PUBLIC = 2

    @property
    def label(self) -> str:
        return {0: "read", 1: "reversible write, ask the user first",
                2: "public write, ask the user first"}[self.value]


class Transport(enum.Enum):
    """How a site is reached. Declared per site, never per call (D-5); a site's `fallback` takes over
    for the rest of a call after a bot check: Cloudflare's, AWS WAF's or the site's declared one (D-40)."""

    DIRECT = "direct"              # T1: plain HTTP, no browser
    BROWSER_HTTP = "browser_http"  # T2: curl_cffi with Chrome's fingerprint (+ browser cookies)
    IN_PAGE = "in_page"            # T3: generic fetch() in a tab of the site's origin

    @property
    def label(self) -> str:
        return {"direct": "T1 direct HTTP", "browser_http": "T2 Chrome-fingerprint HTTP",
                "in_page": "T3 in-page fetch"}[self.value]

    @property
    def short(self) -> str:
        return {"direct": "T1", "browser_http": "T2", "in_page": "T3"}[self.value]

    @property
    def rank(self) -> int:
        return {"direct": 1, "browser_http": 2, "in_page": 3}[self.value]


RESERVED_FLAGS = frozenset({"json", "browser", "cdp", "fields", "no-spill", "max-retries", "min-interval", "help", "h",
                            "version"})
RESERVED_NAMES = frozenset({"list", "docs", "help"})
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def check_format(value: Any, where: str) -> int:
    """Validate a module's declared FORMAT. Refuses missing, unknown or newer formats."""
    if value is None:
        raise ModuleFormatError(f"{where}: no FORMAT declared; add `FORMAT = {FORMAT}` at module level")
    if not isinstance(value, int) or isinstance(value, bool):
        raise ModuleFormatError(f"{where}: FORMAT must be an integer, got {value!r}")
    if value > FORMAT:
        raise ModuleFormatError(f"{where}: targets module format {value}, but this esum supports up "
                                f"to {FORMAT}; upgrade esum")
    if value not in SUPPORTED_FORMATS:
        raise ModuleFormatError(f"{where}: module format {value} is no longer supported (this esum "
                                f"supports {sorted(SUPPORTED_FORMATS)}); port the module")
    return value


def _check_name(name: str, what: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"{what} name {name!r} must match {_NAME_RE.pattern}")
    if name in RESERVED_NAMES:
        raise ValueError(f"{what} name {name!r} is reserved for an esum command")
    return name


def _check_base_url(base_url: str) -> None:
    """Requests are built on `base_url`, so it must be a web URL (see core/urls.py)."""
    if not (isinstance(base_url, str) and is_web_url(base_url)):
        raise ValueError(f"base_url must be an http:// or https:// URL with a host, got {base_url!r}")


def _caller_module(depth: int = 2) -> str:
    return sys._getframe(depth).f_globals.get("__name__", "?")


def _check_origin(origin: str | None, depth: int = 3) -> str:
    """The module that registers an object: `origin` when given (a factory passes its caller's
    `__name__`), else the module that called `engine()` / `site()`."""
    if origin is None:
        return _caller_module(depth)
    if not isinstance(origin, str) or not origin:
        raise TypeError(f"origin must be a module name (pass __name__), got {origin!r}")
    return origin


# --- parameters -------------------------------------------------------------------------------------

_SCALARS = (int, float, str)


@dataclass(frozen=True)
class Param:
    """One CLI flag generated from an action argument."""

    name: str                    # Python parameter name (`from_`)
    flag: str                    # CLI flag (`--from`)
    type: type                   # int, float, str or bool (element type for lists)
    default: Any
    required: bool
    choices: tuple | None = None
    is_list: bool = False
    help: str = ""
    minimum: int | None = None   # both enforced before the action runs: a value is never quietly changed
    maximum: int | None = None

    @property
    def type_label(self) -> str:
        if self.type is bool:
            return "flag"
        base = "|".join(str(c) for c in self.choices) if self.choices else self.type.__name__
        return f"{base} …" if self.is_list else base

    @property
    def usage(self) -> str:
        if self.type is bool:
            text = f"--[no-]{self.flag[2:]}"
        else:
            meta = "|".join(str(c) for c in self.choices) if self.choices else f"<{self.type.__name__}>"
            text = f"{self.flag} {meta}" + (" …" if self.is_list else "")
        return text if self.required else f"[{text}]"


def _strip_optional(tp: Any) -> tuple[Any, bool]:
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) == 1 and len(typing.get_args(tp)) == 2:
            return args[0], True
    return tp, False


def _analyze_type(tp: Any, where: str) -> tuple[type, tuple | None, bool]:
    """-> (scalar type, choices, is_list). Raises TypeError for annotations flags can't express."""
    tp, _ = _strip_optional(tp)
    if typing.get_origin(tp) is list:
        (elem,) = typing.get_args(tp) or (str,)
        base, choices, nested = _analyze_type(elem, where)
        if nested or base is bool:
            raise TypeError(f"{where}: list elements must be int, float, str or a Literal")
        return base, choices, True
    if typing.get_origin(tp) is Literal:
        choices = typing.get_args(tp)
        kinds = {type(c) for c in choices}
        if len(kinds) != 1 or not kinds <= {int, str}:
            raise TypeError(f"{where}: Literal choices must all be str or all be int")
        return kinds.pop(), tuple(choices), False
    if tp is bool or tp in _SCALARS:
        return tp, None, False
    raise TypeError(f"{where}: unsupported annotation {tp!r}; use int, float, str, bool, a Literal, "
                    f"list[...] of those, or X | None")


def _bounds(name: str, tp: type, default: Any, help_text: str) -> tuple[int | None, int | None]:
    """A paging argument's range. The maximum is the `(max N)` its help states, so the two can't differ."""
    if tp is not int or name not in ("limit", "offset", "page"):
        return None, None
    stated = re.search(r"\bmax (\d+)\)", help_text)
    return (0 if name == "offset" or (name == "page" and default == 0) else 1,
            int(stated.group(1)) if stated else None)


def parse_docstring(doc: str | None) -> tuple[str, str, dict[str, str]]:
    """Google-style docstring -> (summary, body, {arg: help}). The summary is the first paragraph."""
    text = inspect.cleandoc(doc or "")
    args: dict[str, str] = {}
    body_lines: list[str] = []
    in_args = False
    current: str | None = None
    arg_indent = 0
    for line in text.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped in ("Args:", "Arguments:", "Parameters:"):
            in_args, current, arg_indent = True, None, 0
            continue
        if in_args:
            if stripped and current and indent > arg_indent:
                args[current] = f"{args[current]} {stripped}".strip()
                continue
            m = re.match(r"^\s+(\w+)\s*(?:\([^)]*\))?:\s*(.*)$", line)
            if m:
                current, arg_indent = m.group(1), indent
                args[current] = m.group(2).strip()
                continue
            if not stripped:
                current = None
                continue
            in_args = False
        body_lines.append(line)
    paragraphs = "\n".join(body_lines).strip().split("\n\n")
    summary = " ".join(paragraphs[0].split()) if paragraphs and paragraphs[0] else ""
    body = "\n\n".join(p.strip() for p in paragraphs[1:] if p.strip())
    return summary, body, args


# --- actions, engines, sites -------------------------------------------------------------------------

@dataclass(eq=False)
class Action:
    name: str                    # CLI name (`user-posts`)
    func: Callable[..., Any]
    tier: Tier
    params: list[Param]
    summary: str
    body: str
    owner: "Engine | Site"

    @property
    def usage(self) -> str:
        return " ".join(p.usage for p in self.params)


def _build_action(func: Callable, owner: "Engine | Site", tier: Tier, name: str | None) -> Action:
    where = f"{getattr(func, '__module__', '?')}.{func.__qualname__}"
    sig = inspect.signature(func)
    plist = list(sig.parameters.values())
    if not plist or plist[0].name != "ctx":
        raise TypeError(f"{where}: an action's first parameter must be `ctx`")
    try:
        hints = typing.get_type_hints(func)
    except Exception as e:  # unresolvable annotation
        raise TypeError(f"{where}: cannot resolve annotations: {e}") from e
    summary, body, arg_help = parse_docstring(func.__doc__)
    if not summary:
        raise TypeError(f"{where}: an action needs a docstring (its first line is the CLI/doc summary)")
    params: list[Param] = []
    for p in plist[1:]:
        if p.kind not in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
            raise TypeError(f"{where}: *args/**kwargs are not supported in actions")
        if p.name not in hints:
            raise TypeError(f"{where}: parameter {p.name!r} needs a type annotation")
        flag_name = p.name.rstrip("_").replace("_", "-")
        if flag_name in RESERVED_FLAGS:
            raise TypeError(f"{where}: parameter {p.name!r} clashes with the global flag --{flag_name}")
        base, choices, is_list = _analyze_type(hints[p.name], f"{where}({p.name})")
        required = p.default is inspect.Parameter.empty
        default = None if required else p.default
        if base is bool and required:
            raise TypeError(f"{where}: bool parameter {p.name!r} needs a default")
        doc = arg_help.get(p.name, "")
        low, high = _bounds(p.name, base, default, doc)
        params.append(Param(name=p.name, flag=f"--{flag_name}", type=base, default=default,
                            required=required, choices=choices, is_list=is_list,
                            help=doc, minimum=low, maximum=high))
    cli_name = _check_name((name or func.__name__).rstrip("_").replace("_", "-"), "action")
    return Action(name=cli_name, func=func, tier=tier, params=params, summary=summary, body=body,
                  owner=owner)


def _coerce_tier(tier: Any) -> Tier:
    if isinstance(tier, bool) or not isinstance(tier, int):
        raise TypeError(f"tier must be a Tier, got {tier!r}")
    try:
        return Tier(tier)
    except ValueError:
        raise ValueError(f"tier {tier!r} does not exist. Valid tiers: 0 READ, 1 REVERSIBLE, 2 PUBLIC. "
                         f"Actions that move money or points (tier 3) are never allowed.") from None


def action(owner: "Engine | Site", *, tier: Tier, name: str | None = None):
    """Declare an action on an engine or a site. `tier` is required.

    The function's first parameter is `ctx` (a `Context`); every other parameter becomes a CLI flag
    (`from_` -> `--from`, `max_pages` -> `--max-pages`). Returns the function unchanged, with the
    `Action` attached as `func.esum_action`, so parsers stay directly testable.
    """
    if not isinstance(owner, (Engine, Site)):
        raise TypeError("@action needs the engine or site object as its first argument")
    tier = _coerce_tier(tier)

    def decorate(func: Callable) -> Callable:
        act = _build_action(func, owner, tier, name)
        if act.name in owner.actions:
            raise ValueError(f"{owner.name}: action {act.name!r} is declared twice")
        owner.actions[act.name] = act
        func.esum_action = act  # type: ignore[attr-defined]
        return func

    return decorate


@dataclass(eq=False)
class Engine:
    name: str
    description: str
    actions: dict[str, Action] = field(default_factory=dict)
    origin: str = ""             # module that registers it (where it was created, or `origin=`)
    format: int | None = None    # filled by the registry from the module's FORMAT
    ported_from: str | None = None
    extra_keys: frozenset[str] = frozenset()  # site extras its actions read (the module's EXTRAS)


def engine(name: str, *, description: str, origin: str | None = None) -> Engine:
    """Declare a platform engine (discourse, wordpress, …). Engine actions run for every site on it.

    `origin` names the module that registers the engine; pass `origin=__name__` when a helper
    function in another module creates it."""
    return Engine(name=_check_name(name, "engine"), description=description,
                  origin=_check_origin(origin))


@dataclass(eq=False)
class Site:
    name: str
    engine: str | None
    base_url: str | None
    transport: Transport | None
    label: dict[str, str]
    fallback: Transport | None = None  # escalate to it on a Cloudflare challenge (D-40)
    charset: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)
    actions: dict[str, Action] = field(default_factory=dict)  # the site's own actions
    origin: str = ""
    kind: Literal["module", "markdown"] = "module"
    builtin: bool = True
    source: Path | None = None     # site.py / module file / SITE.md
    format: int | None = None
    ported_from: str | None = None
    meta: dict[str, str] = field(default_factory=dict)  # Markdown frontmatter
    engine_obj: Engine | None = None  # resolved by the registry
    extra_keys: frozenset[str] | None = None  # the module's EXTRAS; None when it declares none

    @property
    def description(self) -> str:
        return self.label.get("en") or next(iter(self.label.values()), "")

    @property
    def transport_short(self) -> str:
        """`T2`, or `T2→T3` when the site escalates on a Cloudflare challenge."""
        if self.transport is None:
            return "-"
        return self.transport.short + (f"→{self.fallback.short}" if self.fallback else "")

    @property
    def transport_label(self) -> str:
        if self.transport is None:
            return "none"
        return self.transport.label + (f"; {self.fallback.label} after a bot check (Cloudflare, AWS WAF)"
                                       if self.fallback else "")

    @property
    def all_actions(self) -> dict[str, Action]:
        """Engine actions overlaid with the site's own (a site action overrides an engine action)."""
        merged = dict(self.engine_obj.actions) if self.engine_obj else {}
        merged.update(self.actions)
        return merged

    @property
    def kind_label(self) -> str:
        if self.kind == "markdown":
            return "markdown"
        return self.engine or "standalone"


def site(name: str, *, base_url: str, label: str | dict[str, str], engine: str | None = None,
         transport: Transport = Transport.DIRECT, charset: str | None = None,
         headers: dict[str, str] | None = None, extras: dict[str, Any] | None = None,
         fallback: Transport | None = None, origin: str | None = None) -> Site:
    """Declare a site: a thin config on an engine (`engine="discourse"`) or a standalone module.

    `charset` overrides the response charset (e.g. FlyerTalk's ISO-8859-1); `headers` are sent with
    every request; `extras` is config the engine reads via `ctx.site.extras` (the registry reports
    keys nothing declares). `fallback` is a stronger transport a call switches to when a request
    meets a Cloudflare challenge (D-40). `origin` names the module that registers the site; pass
    `origin=__name__` when a helper function in another module creates it.
    """
    if not isinstance(transport, Transport):
        raise TypeError(f"transport must be a Transport, got {transport!r}")
    if fallback is not None and (not isinstance(fallback, Transport) or fallback.rank <= transport.rank):
        raise ValueError(f"fallback must be a Transport stronger than {transport.short}, got {fallback!r}")
    if engine is not None:
        _check_name(engine, "engine")
    _check_base_url(base_url)
    labels = {"en": label} if isinstance(label, str) else dict(label)
    if not labels:
        raise ValueError("site label must not be empty")
    return Site(name=_check_name(name, "site"), engine=engine, base_url=base_url.rstrip("/"),
                transport=transport, label=labels, charset=charset, headers=dict(headers or {}),
                extras=dict(extras or {}), fallback=fallback, origin=_check_origin(origin))
