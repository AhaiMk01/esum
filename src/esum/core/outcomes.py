"""One translation from exceptions to public failure outcomes."""
from __future__ import annotations

import shlex
from typing import NamedTuple, Sequence

from .errors import (ArgumentError, CheckNeeded, HttpError, KeystoreError, LoginNeeded, NoSuchItem,
                     SiteRefused, SiteNotLoaded, TransportFailure, WriteInterrupted, WriteLogError)


class FailureOutcome(NamedTuple):
    message: str
    help: str
    exit_code: int


def failure_outcome(error: Exception, argv: Sequence[str] = (), replay: Sequence[str] | None = None) -> FailureOutcome:
    """Return (message, help, exit code), appending completed writes last. `argv` is the command
    without global flags; `replay` is the whole command line as given, global flags included, for
    help that says to rerun it (defaults to `argv`)."""
    if isinstance(error, WriteInterrupted):
        written = []
        while isinstance(error, WriteInterrupted):
            written.extend(error.written)
            error = error.cause
        written = list(dict.fromkeys(written))
        outcome = failure_outcome(error, argv, replay)
        done = "; ".join(written) if written else "none"
        help = (f"{outcome.help}. This was a write. Already written: {done}. Read each item's state and rerun "
                "the write only for items not yet applied, never the whole command as is")
        return FailureOutcome(outcome.message, help, outcome.exit_code)
    if isinstance(error, (LoginNeeded, CheckNeeded)):
        code = error.exit_code
    else:
        code = 2 if isinstance(error, ArgumentError) else 1
    if isinstance(error, (LoginNeeded, CheckNeeded)):
        message = str(error).removeprefix("esum: ")
    elif isinstance(error, (ArgumentError, NoSuchItem, SiteRefused, SiteNotLoaded, KeystoreError, TransportFailure)):
        message = str(error)
    else:
        message = f"{type(error).__name__}: {error}"
    return FailureOutcome(message, _next_step(error, argv, argv if replay is None else replay), code)


def _next_step(e: Exception, argv: Sequence[str], replay: Sequence[str]) -> str:
    """What to try after a failure ESUM can't name more exactly. A module's own `help` comes first."""
    if own := getattr(e, "help", None):
        return own
    command = " ".join(["esum", *argv[:2]])
    site = command.split(" ")[1] if " " in command else "<site>"
    status = e.status if isinstance(e, HttpError) else None
    if isinstance(e, WriteLogError):
        return (f"The write log could not be saved to {e.path}. The listed items were written; "
                "restore access to the log before attempting more writes")
    if isinstance(e, ArgumentError):
        # ESUM's own check (or a site answer an action turned into one), never an HTTP refusal
        return f"Change the argument the error names: `{command} --help` lists each flag and its format"
    if isinstance(e, NoSuchItem) or status in (404, 410):
        return "The site has no such item: check the id or name. It may have been removed"
    if isinstance(e, SiteRefused) or status in (401, 403):
        return f"The site refused this call: `esum docs {site}` says what it needs"
    if status == 429:
        wait = max(30, round(e.wait or 0))
        return f"The site rate-limited ESUM: wait about {wait} s, then rerun `{shlex.join(['esum', *replay])}`"
    if status is not None and status >= 500:
        return "The site failed: rerun in a minute"
    if status is not None:
        return f"The site rejected the arguments: `{command} --help` lists them"
    return ("Rerun it once. If it fails the same way, the site may have changed: the esum-site-author "
            "skill repairs a site, and ESUM_DEBUG=1 prints a traceback")
