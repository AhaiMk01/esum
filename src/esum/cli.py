"""The `esum` command. Per-action flags are generated from each action's typed signature.

    esum list                              built-in + custom sites
    esum docs <site> [--write]             a site's doc (generated or Markdown); --write saves it
    esum docs --all --write                regenerate every built-in site's doc
    esum <site> [--help]                   a site's actions and arguments
    esum <site> <action> --arg value …     run an action

Global flags, accepted anywhere: --json --fields=<a,b|all> --browser=managed|attach|native --cdp=<url>
--no-spill --max-retries=<n> --min-interval=<seconds> (also ESUM_MAX_RETRIES / ESUM_MIN_INTERVAL, for batches)

Output follows AXI (D-43): lists are TOON tables with next-step hints, errors go to stdout as
`error:` / `help:` lines, and running `esum` alone shows the sites.
"""

from __future__ import annotations

import argparse
import difflib
import io
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Sequence, TextIO

import toon_format as toon

from . import __version__
from .core import docs as docsmod
from .core.context import Options
from .core.errors import EXIT_OK, EXIT_USAGE, ArgumentError, SiteNotLoaded
from .core.outcomes import failure_outcome
from .core.model import Action, Param, Site
from .core.output import dumps
from .core.text import sanitize_text
from .core.registry import PendingSite, Registry, get_registry
from .core.runner import run_action

GLOBAL_HELP = ("global flags: --json  --fields=<a,b|all>  --browser=managed|attach|native  --cdp=<url>  "
               "--no-spill  --max-retries=<n>  --min-interval=<seconds>")
BROWSER_MODES = ("managed", "attach", "native")
GLOBAL_FLAGS = tuple(re.findall(r"--[a-z-]+", GLOBAL_HELP))   # what extract_globals parses (a test checks)
DESCRIPTION = ("Read websites (forums, deal blogs, HN, Reddit) for coding agents: search, list and read "
               "whole threads; account writes only after asking the user")


class UsageError(ArgumentError):
    """Invalid CLI syntax, with the same outcome as an invalid action argument."""


class Parser(argparse.ArgumentParser):
    """argparse that reports a bad flag as a usage error naming the valid flags (AXI §6)."""

    def __init__(self, *args, valid: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.valid = valid

    def parse_known_args(self, args=None, namespace=None):  # type: ignore[override]
        self._seen = list(sys.argv[1:] if args is None else args)
        return super().parse_known_args(args, namespace)

    def _suggestion(self) -> tuple[str, str] | None:
        """`did you mean --query?` for the first typed flag this parser doesn't know (argparse reports
        a missing required flag before an unknown one, so the typo would go unmentioned)."""
        own = [o for o in self._option_string_actions if o.startswith("--")]
        candidates = own + [g for g in GLOBAL_FLAGS if g not in own]   # a misspelled global flag lands here too
        for arg in getattr(self, "_seen", []):
            if arg == "--":
                break
            flag = arg.partition("=")[0]
            if not flag.startswith("--") or flag in own or any(o.startswith(flag) for o in own):
                continue   # argparse accepts a unique prefix of its own flags
            if match := difflib.get_close_matches(flag, candidates, n=1, cutoff=0.7):
                return flag, match[0]
        return None

    def error(self, message: str):  # type: ignore[override]
        typo = self._suggestion()
        if message.startswith("unrecognized arguments:"):
            message = f"unknown argument {message.partition(':')[2].strip()} for `{self.prog}`"
            message += f"; did you mean {typo[1]}?" if typo else ""
        elif typo:   # the typo first: fixing it usually supplies the flag argparse calls missing
            message = f"{typo[0]} is not a flag for `{self.prog}`: did you mean {typo[1]}? ({message})"
        else:
            message = f"{self.prog}: {message}"
        raise UsageError(message, help=f"valid flags: {self.valid or 'none'}; `{self.prog} --help` for details")


def _number(name: str, value: str | None) -> int | float:
    """`--max-retries` takes a whole number, `--min-interval` seconds (decimals allowed)."""
    try:
        number = int(value) if "retries" in name.lower() else float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise UsageError(f"{name} needs a number, got {value!r}", help="Run `esum --help` for the global flags") from None
    if number < 0:
        raise UsageError(f"{name} can't be negative", help="Run `esum --help` for the global flags")
    return number


def extract_globals(argv: Sequence[str]) -> tuple[Options, list[str]]:
    """Pull the global flags out of argv wherever they appear (before or after the action)."""
    opts = Options()
    rest: list[str] = []
    it = iter(argv)
    for arg in it:
        if arg == "--":
            rest.append(arg)
            rest.extend(it)
            break
        name, eq, value = arg.partition("=")
        if name == "--json" and not eq:
            opts.json = True
        elif name == "--no-spill" and not eq:
            opts.no_spill = True
        elif name == "--fields":
            if not eq:
                value = next(it, None)
            if not value:
                raise UsageError("--fields needs a value", help="--fields=<a,b> or --fields=all")
            opts.fields = value
        elif name in ("--browser", "--cdp"):
            if not eq:
                value = next(it, None)
                if value is None:
                    raise UsageError(f"{name} needs a value", help="Run `esum --help` for the global flags")
            if name == "--browser":
                if value not in BROWSER_MODES:
                    raise UsageError(f"--browser must be one of {', '.join(BROWSER_MODES)}", help="Run `esum --help` for the global flags")
                opts.browser = value  # type: ignore[assignment]
            else:
                opts.cdp = value
        elif name in ("--max-retries", "--min-interval"):
            if not eq:
                value = next(it, None)
            setattr(opts, name[2:].replace("-", "_"), _number(name, value))
        else:
            rest.append(arg)
    for name, env in (("--max-retries", "ESUM_MAX_RETRIES"), ("--min-interval", "ESUM_MIN_INTERVAL")):
        attr = name[2:].replace("-", "_")
        if getattr(opts, attr) is None and os.environ.get(env):
            setattr(opts, attr, _number(env, os.environ[env]))
    if opts.cdp and opts.browser == "native":
        raise UsageError("--cdp doesn't apply to --browser=native, which finds your everyday Chrome itself", help="Run `esum --help` for the global flags")
    if opts.cdp and opts.browser == "managed":
        opts.browser = "attach"  # a CDP URL only makes sense for an attached Chrome
    return opts, rest


# --- help --------------------------------------------------------------------------------------------

def _bin() -> str:
    exe = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else Path(sys.executable)
    try:
        return "~/" + exe.relative_to(Path.home()).as_posix()
    except ValueError:
        return exe.as_posix()


def _site_rows(reg: Registry) -> list[dict]:
    return [{"name": s.name + ("" if s.builtin else "*"), "engine": s.kind_label,
             "transport": s.transport_short if s.transport else "?" if isinstance(s, PendingSite) else "-",
             "description": s.description} for s in reg.sorted_sites()]


def _home(reg: Registry) -> str:
    """`esum` alone: what this is and the sites, so the next call can be a real one (AXI §8)."""
    rows = _site_rows(reg)
    doc: dict = {"bin": _bin(), "description": DESCRIPTION, "version": __version__}
    doc["sites"] = rows if rows else "0 installed: custom sites go in ~/.esum/sites/ (esum-site-author skill)"
    hints = ["Run `esum docs <site>` before a site's first call: actions, arguments and quirks",
             "Run `esum <site> <action> --help` for one action's flags",
             "Add `--json` for JSON; exit codes: 0 ok, 1 error, 2 usage, 3 LOGIN NEEDED, 5 CHECK NEEDED"]
    text = toon.dumps(doc) + f"\nhelp[{len(hints)}]:\n" + "\n".join(f"  {h}" for h in hints)
    return text + "".join(f"\nnot loaded: {p.where}: {p.error}" for p in reg.problems)


def _top_help(reg: Registry) -> str:
    lines = [f"esum {__version__}: read websites for coding agents; writes need the user's approval.",
             "", "usage:",
             "  esum list                            sites and their engines",
             "  esum docs <site> [--write]           a site's doc; --write regenerates the saved file",
             "  esum docs --all --write              regenerate every built-in site's doc",
             "  esum keys [ui]                       saved site keys; ui manages them in a local page",
             "  esum <site> --help                   a site's actions and arguments",
             "  esum <site> <action> --arg value …   run an action",
             "", GLOBAL_HELP, "",
             "Exit codes: 0 ok, 1 error, 2 usage, 3 LOGIN NEEDED, 5 CHECK NEEDED."]
    sites = reg.sorted_sites()
    if sites:
        lines += ["", "sites:"]
        width = max(len(s.name) for s in sites)
        lines += [f"  {s.name.ljust(width)}  {s.description}" for s in sites]
    return "\n".join(lines)


def _site_help(site: Site) -> str:
    """`esum <site>`: the site and its actions as TOON, with the next calls (AXI §8, §9)."""
    doc: dict = {"site": site.name, "description": site.description, "engine": site.kind_label,
                 "base_url": site.base_url or "none", "transport": site.transport_label}
    actions = site.all_actions
    if site.kind == "markdown":
        doc["actions"] = f"0: a Markdown site; read `esum docs {site.name}` and follow it"
        hints = [f"Run `esum docs {site.name}` for the site's instructions"]
    elif not actions:
        doc["actions"] = "0: this site has no actions"
        hints = []
    else:
        doc["actions"] = [{"name": name, "tier": "read" if actions[name].tier == 0 else
                           f"tier {actions[name].tier.value}: write, ask the user first",
                           "summary": actions[name].summary,
                           "usage": f"esum {site.name} {name}" + (f" {actions[name].usage}" if actions[name].usage else "")}
                          for name in sorted(actions)]
        hints = [f"Run `esum {site.name} <action> --help` for one action's flags and examples",
                 f"Run `esum docs {site.name}` for the full doc, quirks included"]
    hints.append(GLOBAL_HELP)
    return toon.dumps(doc) + f"\nhelp[{len(hints)}]:\n" + "\n".join(f"  {h}" for h in hints)


# --- argument parsing generated from signatures ------------------------------------------------------

def _bounded(p: Param):
    def parse(text: str) -> int:
        value = int(text)
        if value < p.minimum or (p.maximum is not None and value > p.maximum):
            wanted = f"{p.minimum} to {p.maximum}" if p.maximum is not None else f"at least {p.minimum}"
            raise argparse.ArgumentTypeError(f"must be {wanted}, got {value}")
        return value
    parse.__name__ = "int"   # argparse names it in "invalid int value"
    return parse


def _add_param(parser: argparse.ArgumentParser, p: Param) -> None:
    kw: dict = {"dest": p.name, "help": p.help.replace("%", "%%") if p.help else None}
    if p.type is bool:
        parser.add_argument(p.flag, action=argparse.BooleanOptionalAction, default=p.default, **kw)
        return
    kw["type"] = p.type if p.minimum is None else _bounded(p)
    if p.choices:
        kw["choices"] = p.choices
    if p.is_list:
        kw["nargs"] = "+"
    if p.required:
        kw["required"] = True
    else:
        kw["default"] = p.default
    kw["metavar"] = None if p.choices else p.type.__name__.upper()
    parser.add_argument(p.flag, **kw)


def _examples(site: Site, act: Action) -> str:
    """One call with the required flags, and one showing two of the others."""
    def flag(p: Param) -> str:
        return f"{p.flag} {p.choices[0] if p.choices else f'<{p.type.__name__}>'}"
    base = " ".join([f"esum {site.name} {act.name}", *(flag(p) for p in act.params if p.required)])
    extra = [flag(p) for p in act.params if not p.required and p.type is not bool][:2]
    calls = [base, " ".join([base, *extra])] if extra else [base]
    return "examples:\n" + "\n".join(f"  {call}" for call in calls)


def action_parser(site: Site, act: Action) -> argparse.ArgumentParser:
    desc = act.summary + (f"\n\n{act.body}" if act.body else "")
    if act.tier:
        desc += f"\n\nTier {act.tier.value} ({act.tier.label}): a write. Ask the user before running it, and run it only after their yes."
    valid = ", ".join(f"--[no-]{p.flag[2:]}" if p.type is bool else p.flag for p in act.params)
    parser = Parser(prog=f"esum {site.name} {act.name}", description=desc,
                    epilog=f"{_examples(site, act)}\n\n{GLOBAL_HELP}",
                    formatter_class=argparse.RawDescriptionHelpFormatter, valid=valid)
    for p in act.params:
        _add_param(parser, p)
    return parser


# --- commands ----------------------------------------------------------------------------------------

def cmd_list(reg: Registry, opts: Options, out: TextIO) -> int:
    sites = reg.sorted_sites()
    if opts.json:
        out.write(dumps({
            "sites": [{"name": s.name, "engine": s.engine, "kind": s.kind,
                       "transport": s.transport.value if s.transport else None,
                       "base_url": s.base_url, "description": s.description,
                       "custom": not s.builtin, "loaded": not isinstance(s, PendingSite),
                       "actions": None if isinstance(s, PendingSite) else sorted(s.all_actions)}
                      for s in sites],
            "problems": [{"where": p.where, "error": p.error} for p in reg.problems]}) + "\n")
        return EXIT_OK
    doc: dict = {"sites": _site_rows(reg) if sites else
                 "0 installed: custom sites go in ~/.esum/sites/ (esum-site-author skill)"}
    if any(not s.builtin for s in sites):
        doc["note"] = "* custom site from ~/.esum/sites; transport ? = custom module, imported only when used"
    if reg.problems:
        doc["not_loaded"] = [f"{p.where}: {p.error}" for p in reg.problems]
    out.write(toon.dumps(doc) + "\n")
    return EXIT_OK


def cmd_docs(reg: Registry, args: list[str], out: TextIO) -> int:
    parser = Parser(prog="esum docs", description="Print or regenerate site docs.", valid="<site>, --write, --all")
    parser.add_argument("site", nargs="?")
    parser.add_argument("--write", action="store_true", help="save the regenerated doc")
    parser.add_argument("--all", action="store_true", help="every built-in site (with --write)")
    ns = parser.parse_args(args)
    if ns.all:
        if not ns.write or ns.site:
            raise UsageError("use `esum docs --all --write`", help="Run `esum docs --all --write` to regenerate every built-in site doc")
        for name in sorted(reg.builtins):  # never imports a custom module
            out.write(f"wrote {docsmod.write_site_doc(reg.builtins[name])}\n")
        return EXIT_OK
    if not ns.site:
        raise UsageError("usage: esum docs <site> [--write]", help="Run `esum docs <site>` to read a site doc; `esum list` lists the sites")
    site = _get_site(reg, ns.site)
    if ns.write:
        out.write(f"wrote {docsmod.write_site_doc(site)}\n")
    else:
        out.write(docsmod.site_doc(site).rstrip("\n") + "\n")
    return EXIT_OK


def cmd_keys(reg: Registry, opts: Options, args: list[str], out: TextIO) -> int:
    """`esum keys`: which sites have a saved key, where, and since when (never a key);
    `esum keys ui`: the local page where the user manages them (D-79)."""
    from .core import keystore
    parser = Parser(prog="esum keys", description="List saved site keys, or manage them in a local page.",
                    valid="ui, --no-open")
    parser.add_argument("command", nargs="?", choices=["ui"])
    parser.add_argument("--no-open", action="store_true", help="print the page's link without opening a browser")
    ns = parser.parse_args(args)
    if ns.command == "ui":
        from .core import keys_ui, terminal
        if not terminal.interactive():
            raise UsageError("`esum keys ui` is for the user, in their own terminal",
                             help="Tell the user to run `esum keys ui` themselves")
        sites = {s.name: {"label": s.description.split(":", 1)[0], "entry": s.extras.get("key_entry")}
                 for s in reg.builtins.values()}
        page = keys_ui.KeysPage(sites, lambda site, action, secret: run_captured(reg, [site, action], secret=secret))
        keys_ui.serve(page, out, open_browser=not ns.no_open)
        return EXIT_OK
    rows = keystore.entries()
    if opts.json:
        out.write(dumps({"keys": rows}) + "\n")
        return EXIT_OK
    table = [{"site": r["site"], "store": r["store"], "created": r.get("created", ""),
              "scopes": ",".join(r.get("scopes") or [])} for r in rows]
    doc: dict = {"keys": table if table else "0 saved"}
    hints = ["The user runs `esum <site> login` in their own terminal to save a key",
             "The user runs `esum keys ui` to add, replace or remove keys in a local page"]
    out.write(toon.dumps(doc) + f"\nhelp[{len(hints)}]:\n" + "\n".join(f"  {h}" for h in hints) + "\n")
    return EXIT_OK


def _get_site(reg: Registry, name: str) -> Site:
    site = reg.get(name)
    if site is None:
        if broken := [p for p in reg.problems if p.site == name]:
            raise SiteNotLoaded(f"site {name!r} failed to load" + "".join(f"\n{p.where}: {p.error}" for p in broken),
                                help="Fix that module (the esum-site-author skill repairs a site), then rerun "
                                     "this command")
        raise UsageError(f"unknown site or command {name!r}",
                         help="Run `esum list` for the sites; commands: list, docs, keys")
    return site


def cmd_site(reg: Registry, opts: Options, args: list[str], out: TextIO, *, secret: str | None = None) -> int:
    site = _get_site(reg, args[0])
    rest = args[1:]
    if not rest or rest[0] in ("-h", "--help", "help"):
        out.write(_site_help(site) + "\n")
        return EXIT_OK
    actions = site.all_actions
    act = actions.get(rest[0])
    if act is None:
        raise UsageError(f"{site.name} has no action {rest[0]!r}",
                         help=f"actions: {', '.join(sorted(actions)) or 'none'}; `esum {site.name} --help` for details")
    ns = action_parser(site, act).parse_args(rest[1:])
    flags = {a.partition("=")[0] for a in rest[1:] if a.startswith("--")}
    typed = frozenset(p.name for p in act.params if p.flag in flags or f"--no-{p.flag[2:]}" in flags)
    run_action(site, act, {p.name: getattr(ns, p.name) for p in act.params}, opts, stream=out, typed=typed,
               **({} if secret is None else {"secret": secret}))
    return EXIT_OK


def run_captured(reg: Registry, argv: list[str], *, secret: str | None = None) -> tuple[int, str]:
    """Run `esum <site> <action> …` in this process; its exit code and what it would have printed,
    errors included. `secret` answers the action's `ctx.ask_secret`: the keys page passes the key
    the user typed there, so it never crosses a process or the terminal."""
    out = io.StringIO()
    try:
        code = cmd_site(reg, Options(), argv, out, secret=secret)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else EXIT_USAGE
    except Exception as e:
        outcome = failure_outcome(e, argv)
        report_error(out, outcome.message, outcome.help, False)
        code = outcome.exit_code
    return code, out.getvalue()


def _utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


VERSION_FLAGS = ("-v", "-V", "--version")


def report_error(out: TextIO, message: str, help: str | None, as_json: bool) -> None:
    """Errors go to stdout in the output's own format, so the agent reads them (AXI §6). The
    message is the site's or the network's own text; `help` is the next thing to try (D-52)."""
    if as_json:
        out.write(json.dumps({"error": message, **({"help": help} if help else {})}, ensure_ascii=False) + "\n")
        return
    message = sanitize_text(message).replace("\n", "\n  ")   # continuation lines can't pose as `help:`
    out.write(f"error: {message}\n" + (f"help: {help}\n" if help else ""))


def main(argv: Sequence[str] | None = None, *, registry: Registry | None = None) -> int:
    _utf8_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    out = sys.stdout
    as_json = "--json" in argv
    command_argv: list[str] = []
    replay: list[str] | None = None
    try:
        opts, args = extract_globals(argv)
        command_argv, replay = args, argv   # a rerun hint keeps the global flags (browser, retries)
        if args[:1] and args[0] in VERSION_FLAGS:
            out.write(f"{__version__}\n")
            return EXIT_OK
        # Discovery imports built-ins only; a custom module is imported when its site is looked up.
        reg = registry or get_registry()
        if not args:
            out.write(_home(reg) + "\n")
            return EXIT_OK
        if args[0] in ("-h", "--help", "help"):
            out.write(_top_help(reg) + "\n")
            return EXIT_OK
        command, rest = args[0], args[1:]
        if command == "list":
            return cmd_list(reg, opts, out)
        if command == "docs":
            return cmd_docs(reg, rest, out)
        if command == "keys":
            return cmd_keys(reg, opts, rest, out)
        if command.startswith("-"):
            raise UsageError(f"unknown flag {command!r}", help="Run `esum --help` for the commands and global flags")
        return cmd_site(reg, opts, args, out)
    except SystemExit as e:  # argparse --help
        return e.code if isinstance(e.code, int) else EXIT_USAGE
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        if os.environ.get("ESUM_DEBUG"):
            traceback.print_exc()
        outcome = failure_outcome(e, command_argv, replay)
        report_error(out, outcome.message, outcome.help, as_json)
        return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
