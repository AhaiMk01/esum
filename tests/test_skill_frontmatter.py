"""The skills' frontmatter must be YAML a skill installer can read (`npx skills add` skips a file
it can't parse, and says so only in a warning)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILLS = sorted((Path(__file__).parent.parent / "skills").glob("*/SKILL.md"))


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_a_frontmatter_value_holding_a_colon_and_space_is_quoted(path):
    head = path.read_text(encoding="utf-8").split("---")[1]
    fields = dict(re.findall(r"^(\w[\w-]*): (.*)$", head, re.M))
    assert fields.get("name") == path.parent.name and fields.get("description")
    for key, value in fields.items():
        quoted = len(value) > 1 and value[0] == value[-1] == '"'
        # unquoted, `key: a: b` reads as a nested mapping and the whole file is refused
        assert quoted or ": " not in value, f"{path.parent.name}: quote the {key}"


def test_there_are_skills_to_check():
    assert len(SKILLS) >= 2
