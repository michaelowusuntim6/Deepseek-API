#!/usr/bin/env python3
"""Offline tests for the progressive-disclosure skills system."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from deepseek import skills


def write_skill(base: Path, name: str, description: str = "Test skill") -> None:
    path = base / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nBody for {name}.\n",
        encoding="utf-8",
    )


def test_skills() -> None:
    with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
        os.environ, {"HOME": tmpdir}
    ), patch("os.getcwd", return_value=tmpdir):
        base = Path(tmpdir) / ".deepseek-cli" / "skills"
        write_skill(base, "hello", "Say hello")
        context, warning = skills.skills_preamble(tmpdir)
        assert "hello: Say hello" in context and warning is None
        assert "- hello: Say hello" in context.splitlines()
        body = skills.use_skill.fn("hello")
        assert "Body for hello" in body
        assert "Body for hello" in skills.read_skill_file.fn("hello", "SKILL.md")
        assert skills.read_skill_file.fn("hello", "../SKILL.md").startswith("Error")

        for i in range(35):
            write_skill(base, f"s{i:02d}", "x" * 100)
        _, warning = skills.skills_preamble(tmpdir)
        assert warning and "truncated" in warning
        truncated, _ = skills.skills_preamble(tmpdir)
        skill_lines = [line for line in truncated.splitlines() if line.startswith("- ")]
        assert len(skill_lines) == 30
    print("all skills tests passed")


if __name__ == "__main__":
    test_skills()
