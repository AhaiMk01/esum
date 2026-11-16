"""The module contract (module-format.md) lists what the code reserves and provides.

The docs-in-sync test covers generated site docs only; this one keeps the hand-written contract's
reserved names, reserved global flags and transport-read extras, and the `Options` fields in
engines-and-backends.md, in step with the code. Each list is a parenthesized, backticked list after a fixed label, e.g.
"(reserved names: `list`, `docs`, `help`)".
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from esum.core.context import Options
from esum.core.model import RESERVED_FLAGS, RESERVED_NAMES
from esum.transport import SITE_EXTRAS

REFERENCES = Path(__file__).parents[1] / "skills" / "esum-site-author" / "references"
CONTRACT = "module-format.md"
INTERNALS = "engines-and-backends.md"   # engine and transport-backend authoring, split from the contract


def documented(doc: str, label: str) -> set[str]:
    text = (REFERENCES / doc).read_text(encoding="utf-8")
    label_re = r"\s+".join(map(re.escape, label.split()))  # the label may wrap across lines
    found = re.findall(rf"\({label_re}:\s*((?:`[^`]+`[,\s]*)+)\)", text)
    assert len(found) == 1, f"{doc} should list ({label}: `a`, `b`, …) exactly once"
    return set(re.findall(r"`([^`]+)`", found[0]))


@pytest.mark.parametrize("doc, label, actual", [
    (CONTRACT, "reserved names", RESERVED_NAMES),
    (CONTRACT, "reserved global flags", RESERVED_FLAGS),
    (INTERNALS, "Options fields", {f.name for f in dataclasses.fields(Options)}),
    (CONTRACT, "extras every site may set", SITE_EXTRAS),
])
def test_contract_doc_matches_code(doc, label, actual):
    assert documented(doc, label) == set(actual)
