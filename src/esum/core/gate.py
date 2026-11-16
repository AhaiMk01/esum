"""Writes (D-34, D-36): only tier >= 1 module actions write, and every write is logged.

A write action does not write. It returns the planned writes (`Write(description, run)`), and the
runner hands them here: each `run` is called in order and every outcome is appended to
`~/.esum/writes.jsonl`. ESUM asks no one itself. Approval is the user's yes when the agent
asks before the write, in whatever way its harness asks (D-85). ESUM has no flag,
plan ID or config that writes on its own.

The log records which custom site.py files were loaded into the process (`custom_code`), since a
custom site's code runs with the same access as ESUM's own (D-25).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from .errors import WriteInterrupted, WriteLogError
from .output import Result, to_jsonable
from .paths import writes_log

if TYPE_CHECKING:
    from .model import Action, Site


@dataclass
class Write:
    """One planned write: what it does (for the output and the log), and the call that does it."""

    description: str
    run: Callable[[], Any]


# --- log ---------------------------------------------------------------------------------------------

def log_event(**record: Any) -> None:
    """Append one JSON line to ~/.esum/writes.jsonl. `custom_code` lists the custom site.py files
    imported into this process (empty when only built-in code ran, D-25)."""
    from .registry import loaded_custom_modules

    path = writes_log()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record,
            "custom_code": loaded_custom_modules()}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False, default=_loggable) + "\n")


def _loggable(value: Any) -> Any:
    try:
        return to_jsonable(value)
    except TypeError:
        return repr(value)


def _short(value: Any, cap: int = 500) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=_loggable)
    return text if len(text) <= cap else text[:cap] + "…"


# --- entry point -------------------------------------------------------------------------------------

def as_writes(value: Any) -> list[Write] | None:
    if isinstance(value, Write):
        return [value]
    if isinstance(value, (list, tuple)) and all(isinstance(w, Write) for w in value):
        return list(value)
    return None


def execute_writes(site: "Site", act: "Action", args: dict[str, Any], planned: Any) -> Result:
    """Run each planned write in order and log what was planned and every outcome."""
    writes = as_writes(planned)
    if writes is None:
        raise TypeError(f"{site.name} {act.name}: a tier-{act.tier.value} action must return a "
                        f"Write or a list of Writes, not {type(planned).__name__}")
    base = dict(site=site.name, action=act.name, tier=int(act.tier), args=args)
    done: list[dict[str, Any]] = []

    def record(**event: Any) -> None:
        try:
            log_event(**event)
        except Exception as error:
            raise WriteLogError(str(writes_log()), error) from error

    def skipped(remaining: list[Write]) -> None:
        for rest in remaining:
            try:
                record(kind="write-result", **base, item=rest.description, outcome="skipped")
            except WriteLogError:
                pass  # best effort: one failed log record must not prevent later skipped records

    try:
        record(kind="write", **base, items=[w.description for w in writes])
        if not writes:
            return Result(text="Nothing to write.", data={"written": []})
        for i, w in enumerate(writes):
            try:
                out = w.run()
            except Exception as e:
                try:
                    record(kind="write-result", **base, item=w.description, outcome="error",
                           error=f"{type(e).__name__}: {e}")
                except WriteLogError as log_error:
                    skipped(writes[i + 1:])
                    raise e from log_error
                skipped(writes[i + 1:])
                raise
            done.append({"item": w.description, "result": out})
            try:
                record(kind="write-result", **base, item=w.description, outcome="ok", result=_short(out))
            except WriteLogError:
                skipped(writes[i + 1:])
                raise
    except Exception as e:
        raise WriteInterrupted(e, [d["item"] for d in done]) from e
    text = "\n".join(f"done {n}. {d['item']}" for n, d in enumerate(done, 1))
    return Result(text=text, data={"written": done})
