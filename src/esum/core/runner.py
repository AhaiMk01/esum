"""Running one action: build the context, call it, route writes through the gate, print the result."""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:
    from ..browser.session import BrowserSession

from ..transport import BackendFactory, HttpClient
from .context import Context, Options
from .gate import as_writes, execute_writes
from .errors import ArgumentError
from .model import Action, Site, Tier
from .output import FieldsError, Listing, as_result, emit
from .paging import RANGE_RESET, RangePastEnd

_PLACEHOLDER = re.compile(r"<[a-z_]+>")


def command_line(site: Site, act: Action, args: dict[str, Any], *, placeholders: bool = True,
                 include_defaults: frozenset[str] = frozenset()) -> str:
    """`esum <site> <action> --flag value …` for the arguments that differ from their defaults, and
    those named in `include_defaults` (the flags the caller typed, so a next page keeps them).
    With `placeholders`, a value such as `<id>` stays unquoted, so the agent sees what to fill in;
    every other value is quoted for the shell and kept on one line."""
    parts = ["esum", site.name, act.name]
    for p in act.params:
        value = args.get(p.name, p.default)
        if value is None:
            if p.required:
                raise ValueError(f"{site.name} {act.name}: a command needs {p.flag}")
            continue
        if p.type is bool:
            if value != p.default or p.name in include_defaults:
                parts.append(p.flag if value else f"--no-{p.flag[2:]}")
            continue
        if value == p.default and not p.required and p.name not in include_defaults:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        text = [str(v) if placeholders and _PLACEHOLDER.fullmatch(str(v)) else shlex.quote(" ".join(str(v).split()))
                for v in values]
        # a value that starts with a minus must be glued on, or it reads as a flag
        if len(text) == 1 and text[0].startswith("-"):
            parts.append(f"{p.flag}={text[0]}")
        else:
            parts += [p.flag, *text]
    return " ".join(parts)


def other_site_command(site: str, action: str, args: dict[str, Any]) -> str | None:
    """The command for an action of another built-in site, to print as a next step or in a `help`
    line: `esum matrix airports --query <city>`. None when that site is not installed here (a
    distribution can leave sites out), so the caller prints nothing rather than a command that
    can't run. Only a built-in site counts: no custom code loads to print a hint (D-25). A site
    that is here without that action is a mistake in the caller: `ValueError`."""
    from .registry import get_registry
    elsewhere = get_registry().sites.get(site)
    if elsewhere is None or not elsewhere.builtin:
        return None
    target = elsewhere.all_actions.get(action)
    if target is None:
        raise ValueError(f"a next step names the unknown action {site} {action!r}")
    return command_line(elsewhere, target, args)


def run_action(site: Site, act: Action, kwargs: dict[str, Any], options: Options,
               stream: TextIO | None = None, *, session: "BrowserSession | None" = None,
               backend_factory: BackendFactory | None = None, typed: frozenset[str] = frozenset(),
               secret: str | None = None) -> None:
    """Run one action and print its result. `typed` names the arguments the caller gave on the
    command line; a next page repeats them even when they equal the default. `secret` answers the
    action's `ctx.ask_secret` (the keys page passes what the user typed there)."""
    if options.fields and act.tier >= Tier.REVERSIBLE:   # refuse before the write, not after it
        raise FieldsError(f"--fields applies to lists; `esum {site.name} {act.name}` is a write",
                          help="Run it without --fields")
    if kwargs.get("from_") is not None and kwargs.get("to") is not None and kwargs["from_"] > kwargs["to"]:
        # before any request: every thread action reads an inverted range the same way
        raise ArgumentError(f"--from {kwargs['from_']} is after --to {kwargs['to']}",
                            help="Give --from less than or equal to --to")
    http = HttpClient(site, options, dedupe=act.tier == Tier.READ, session=session,
                      backend_factory=backend_factory)
    ctx = Context(site=site, action=act, options=options, http=http, secret=secret)
    try:
        returned = act.func(ctx, **kwargs)
        if act.tier >= Tier.REVERSIBLE:
            result = execute_writes(site, act, kwargs, returned)
        else:
            if as_writes(returned) and returned:
                raise TypeError(f"{site.name} {act.name}: a tier-0 (read) action returned writes; "
                                f"declare the action with tier=Tier.REVERSIBLE")
            result = as_result(returned)
    except RangePastEnd as error:
        if error.help is None:
            last = command_line(site, act, {**kwargs, **RANGE_RESET, **error.last_range}, placeholders=False)
            error.help = f"Run `{last}` to read the last posts"
        raise
    finally:
        ctx.close()

    def command(action: str, args: dict[str, Any], other: str | None = None) -> str | None:
        if other is not None and other != site.name:
            try:
                return other_site_command(other, action, args)     # None: that site isn't here, no step
            except ValueError as error:
                raise ValueError(f"{site.name} {act.name}: {error}") from None
        target = site.all_actions.get(action)
        if target is None:
            raise ValueError(f"{site.name} {act.name}: a next step names the unknown action {action!r}")
        # the next page repeats this call's arguments, which are the user's, never placeholders;
        # another action gets only the step's own arguments
        if target is act:
            line = command_line(site, act, {**kwargs, **args}, placeholders=False,
                                include_defaults=typed)
            # only the next page is sure to be a list: another step on this action may print one item
            if options.fields and isinstance(result, Listing) and args == result.more:
                line += " --fields " + shlex.quote(options.fields)
            return line
        return command_line(site, target, args)

    emit(result, site=site.name, action=act.name, as_json=options.json, stream=stream,
         fields=options.fields, command=command, spill=not options.no_spill)
