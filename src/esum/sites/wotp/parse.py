"""Wings of the Points into rows: the transfer CSV the chart is generated from (one line per airline
program, one column per points currency, a cell holding the ratio `points:miles`) and the README's
Note and Change Log sections. Pure functions.

The CSV names currencies by column key (`chase`, `c1`); `LABELS` holds the names the chart draws
for them. A column this module doesn't know shows under its key.
"""

from __future__ import annotations

import csv
import difflib
import io
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date

from esum import one_line

FIXED = ("code", "name", "alliance", "link_order")      # CSV columns that aren't a currency
LABELS = {"chase": "Chase UR", "amex": "AmEx MR", "citi": "Citi TYP", "c1": "Capital One",
          "bilt": "Bilt Rewards", "marriott": "Marriott Bonvoy"}
# other names people and the change log use for a currency
ALIASES = {"chase": ("ur", "ultimate rewards"), "amex": ("mr", "membership rewards", "american express"),
           "citi": ("typ", "thankyou", "thank you"), "c1": ("capitalone", "cap1"), "bilt": (),
           "marriott": ("bonvoy", "mb")}
# A hotel currency reaches most programs at about 3:1, so it doesn't count against a bank
# currency's sole partners.
HOTEL = frozenset({"marriott"})
ALLIANCES = ("star", "oneworld", "skyteam", "none")
_ALLIANCE = {"staralliance": "star", "oneworld": "oneworld", "skyteam": "skyteam",
             "nonaliance": "none", "nonalliance": "none"}       # `nonAliance` is the CSV's spelling
_RATIO = re.compile(r"\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*")
_DATE = re.compile(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})")
_SHARED = re.compile(r"Apart from [^(\n]+\((\w{2})\), (.+?) is also the frequent flyer program for ([^\n.]+)\.")
# the maintainer's mark that the rest of an entry is older changes written down late
_OLDER = re.compile(r"\(and the following (?:are )?old(?:er)? changes\)\s*$", re.I)
_MEMBER = re.compile(r"([^,()]+?)\s*\((\w{2})\)")


class PageChanged(RuntimeError):
    """The CSV or README is not laid out as this parser knows it. Never carries their content."""


@dataclass
class Chart:
    programs: tuple[str, ...]      # currency keys, in the CSV's column order
    rows: list[dict]               # code, name, alliance, one ratio per currency key, routes, best


def fold(text: str | None) -> str:
    """Lower case, accents dropped, spaces collapsed: `Aeroméxico` and `aeromexico` compare equal."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c))
    return " ".join(plain.casefold().split())


def label(program: str) -> str:
    return LABELS.get(program, program)


def miles_per_point(ratio: str | None) -> float | None:
    """`3:1.25` -> 0.4167 miles for one point. None for an empty or unreadable cell."""
    found = _RATIO.fullmatch(ratio or "")
    if not found or not float(found[1]):
        return None
    return float(found[2]) / float(found[1])


def _best(ratios: dict[str, str | None]) -> str | None:
    """The ratio giving the most miles per point and the currencies that have it: `1:1 amex chase`."""
    worth = {p: v for p, r in ratios.items() if (v := miles_per_point(r)) is not None}
    if not worth:
        return None
    top = max(worth.values())
    winners = [p for p, v in worth.items() if abs(v - top) < 1e-9]
    return f"{ratios[winners[0]].strip()} {' '.join(winners)}"


def best_of(row: dict, programs: list[str] | tuple[str, ...]) -> str | None:
    """A row's best ratio among these currencies only."""
    return _best({p: row.get(p) for p in programs})


def parse_chart(text: str) -> Chart:
    """The transfer CSV. An empty cell means that currency doesn't transfer to that program."""
    reader = csv.DictReader(io.StringIO(text.lstrip(chr(0xFEFF))))      # a byte-order mark, if any
    header = [h.strip() for h in reader.fieldnames or [] if h]
    if not {"code", "name", "alliance"} <= set(header):
        raise PageChanged("the transfer CSV has no code, name and alliance columns")
    programs = tuple(h for h in header if h not in FIXED)
    if not programs:
        raise PageChanged("the transfer CSV has no currency columns")
    rows = []
    for line in reader:
        raw = {(k or "").strip(): v for k, v in line.items() if isinstance(v, str)}     # ` amex` is `amex`
        code = (raw.get("code") or "").strip().upper()
        if not code:
            continue
        ratios = {p: (raw.get(p) or "").strip() or None for p in programs}
        kind = (raw.get("alliance") or "").strip()
        rows.append({"code": code, "name": one_line(raw.get("name")),
                     "alliance": _ALLIANCE.get(kind.lower(), kind.lower()) or None, **ratios,
                     "routes": sum(1 for r in ratios.values() if r), "best": _best(ratios)})
    if not rows:
        raise PageChanged("the transfer CSV has no airline rows")
    return Chart(programs, rows)


def reach(row: dict, programs: tuple[str, ...]) -> set[str]:
    """The currencies that transfer to this row's program."""
    return {p for p in programs if row.get(p)}


def resolve_program(term: str, programs: tuple[str, ...]) -> str | None:
    """A currency key from what the user typed: its key, the chart's name for it or a common alias."""
    wanted = fold(term)
    for p in programs:
        if wanted in {p, fold(label(p)), fold(label(p)).replace(" ", ""), *ALIASES.get(p, ())}:
            return p
    return None


def _word_start(term: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(fold(term)))


def match_airlines(chart: Chart, terms: list[str], shared: list[dict]) -> tuple[set[str], list[str], list[str]]:
    """Codes of the rows the terms name, notes on airlines found under another's row, and the terms
    that matched nothing. A term is an airline code, or the start of a word in an airline or program
    name; `shared` (from the README) maps an airline without a row to the row of its program."""
    codes = {r["code"] for r in chart.rows}
    by_code = {s["code"]: s for s in shared}
    found: set[str] = set()
    notes: list[str] = []
    unmatched: list[str] = []

    def under(s: dict) -> bool:
        if s["host"] not in codes:
            return False
        found.add(s["host"])
        note = f"{s['name']} ({s['code']}) uses {s['program']}, listed under {s['host']}"
        if note not in notes:
            notes.append(note)
        return True

    for term in terms:
        code = term.strip().upper()
        if code in codes:
            hits = [code]
            found.add(code)
        elif code in by_code:
            hits = [code] if under(by_code[code]) else []
        elif fold(term):
            pattern = _word_start(term)
            hits = [r["code"] for r in chart.rows if pattern.search(fold(r["name"]))]
            found.update(hits)
            hits += [s["code"] for s in shared if pattern.search(fold(s["name"])) and under(s)]
        else:
            hits = []
        if not hits:        # a term is unmatched when it names nothing, not when an earlier term named the same row
            unmatched.append(term)
    return found, notes, unmatched


def closest(chart: Chart, term: str) -> list[str]:
    """Rows whose code or a word of whose name is nearly what was typed: `EY Etihad (Etihad Guest)`
    for `etihadx`. For the message about a term that matched nothing."""
    words: dict[str, list[dict]] = {}
    for r in chart.rows:
        for word in {w for w in re.split(r"[^a-z0-9]+", fold(r["name"])) if len(w) > 2}:
            words.setdefault(word, []).append(r)
    near = difflib.get_close_matches(fold(term), list(words), n=3, cutoff=0.75)
    return list(dict.fromkeys(f"{r['code']} {r['name']}" for word in near for r in words[word]))[:3]


def spread(rows: list[dict], program: str) -> str:
    """How a currency's ratios spread: `1:1 ×13; 1:0.8 ×3 (EK B6 CX)`. The commonest ratio shows
    its count only; the others name their airlines."""
    counts = Counter(r[program] for r in rows if r.get(program))
    order = sorted(counts, key=lambda ratio: (-counts[ratio], -(miles_per_point(ratio) or 0)))
    parts = []
    for i, ratio in enumerate(order):
        codes = " ".join(r["code"] for r in rows if r.get(program) == ratio)
        parts.append(f"{ratio} ×{counts[ratio]}" + (f" ({codes})" if i else ""))
    return "; ".join(parts)


def program_rows(chart: Chart) -> list[dict]:
    """One row per currency: how many programs it reaches, by alliance, its ratios and the
    programs it alone reaches (see `HOTEL` for how a hotel currency counts)."""
    banks = [p for p in chart.programs if p not in HOTEL]
    out = []
    for p in chart.programs:
        partners = [r for r in chart.rows if r.get(p)]
        rivals = set(chart.programs if p in HOTEL else banks) - {p}
        sole = [r["code"] for r in partners if not reach(r, chart.programs) & rivals]
        out.append({"key": p, "name": label(p), "kind": "hotel" if p in HOTEL else "bank", "partners": len(partners),
                    **{a: sum(1 for r in partners if r["alliance"] == a) for a in ALLIANCES},
                    "ratios": spread(chart.rows, p), "sole": " ".join(sole) or None})
    return out


def section(readme: str, title: str) -> str:
    """The text under a `## title` heading, up to the next `## ` heading."""
    readme = readme.replace("\r\n", "\n")
    found = re.search(rf"^##[ \t]+{re.escape(title)}[ \t]*$", readme, re.M)
    if not found:
        raise PageChanged(f"the README has no `## {title}` section")
    rest = readme[found.end():]
    end = re.search(r"^##[ \t]+\S", rest, re.M)
    return (rest[:end.start()] if end else rest).strip()


def shared_programs(readme: str) -> list[dict]:
    """Airlines the chart leaves out because they use another airline's program, from the README's
    notes: `{code: KL, name: KLM, host: AF, program: Flying Blue}`. Empty when the notes say none."""
    out = []
    for host, program, members in _SHARED.findall(readme):
        for name, code in _MEMBER.findall(members):
            name = re.sub(r"^and\s+", "", name.strip())
            out.append({"code": code.upper(), "name": name, "host": host.upper(), "program": program.strip()})
    return out


def parse_changes(readme: str) -> list[dict]:
    """The Change Log, one row per bullet, in the README's order (newest first): the date of its
    `### ` heading (ISO when it reads as a date, else as written) and the bullet's text. A line
    right under a bullet continues it, as Markdown renders it. `older` is true for the bullets of
    an entry that follow its `(and the following old changes)` mark: they happened before the
    entry's date."""
    rows: list[dict] = []
    day: str | None = None
    open_item = False
    late = False        # past the entry's old-changes mark, on a bullet's line or a line of its own
    for line in section(readme, "Change Log").splitlines():
        text = line.strip()
        if text.startswith("###"):
            heading = text.lstrip("#").strip()
            found = _DATE.fullmatch(heading)
            try:
                day = date(int(found[1]), int(found[2]), int(found[3])).isoformat() if found else heading
            except ValueError:
                day = heading
            open_item = late = False
            continue
        if day is None or not text:
            open_item = False
            continue
        if re.match(r"[*+-]\s+", text):
            rows.append({"date": day, "change": one_line(re.sub(r"^[*+-]\s+", "", text)), "older": late})
            open_item = True
        elif open_item:
            rows[-1]["change"] += " " + one_line(text)
        if _OLDER.search(text):
            late = True
    if not rows:
        raise PageChanged("the README's Change Log has no dated entries")
    return rows


def day_of(row: dict) -> date | None:
    try:
        return date.fromisoformat(row["date"])
    except ValueError:
        return None


def updated(changes: list[dict]) -> str | None:
    """The newest dated change: when the chart was last updated."""
    days = [d for r in changes if (d := day_of(r))]
    return max(days).isoformat() if days else None


def change_patterns(term: str, chart: Chart, shared: list[dict]) -> list[re.Pattern]:
    """What to look for in the log for one query term. The log writes airlines as their codes and
    currencies under several names (UR, MR, TYP, C1), so an airline code is matched in capitals as
    a whole word, plus the airline's name, and a currency by its key, chart name and aliases."""
    code = term.strip().upper()
    names = {r["code"]: r["name"].split(" (")[0] for r in chart.rows}
    names.update({s["code"]: s["name"] for s in shared if s["code"] not in names})
    if code in names:
        return [re.compile(rf"(?<![A-Za-z0-9]){re.escape(code)}(?![A-Za-z0-9])"),
                re.compile(re.escape(names[code]), re.I)]
    program = resolve_program(term, chart.programs)
    if program:
        words = {program, label(program), *ALIASES.get(program, ())}
        return [re.compile(rf"(?<![A-Za-z0-9]){re.escape(w)}(?![A-Za-z0-9])", re.I) for w in sorted(words)]
    return [re.compile(re.escape(term.strip()), re.I)]
