#!/usr/bin/env python3
"""Offline tests for the Markdown memory extension."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import importlib.util


def load_memory_module(tmp: Path):
    spec = importlib.util.spec_from_file_location(
        "memory_ext_test", ROOT / "examples" / "extensions" / "memory.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_memory() -> None:
    with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
        os.environ, {"DEEPSEEK_CLI_HOME": tmpdir}
    ):
        memory = load_memory_module(Path(tmpdir))
        result = memory.memory_write.fn("long_term", "The project uses Python 3.11 and pytest.")
        assert "wrote long_term" in result
        assert "pytest" in (Path(tmpdir) / "memory" / "MEMORY.md").read_text()

        found = memory.memory_search.fn("pytest")
        assert "Python 3.11" in found

        for i in range(250):
            memory.memory_write.fn("long_term", f"entry {i}")
        lines = (Path(tmpdir) / "memory" / "MEMORY.md").read_text().splitlines()
        assert len(lines) <= 200
        archives = list((Path(tmpdir) / "memory" / "archive").glob("*.md"))
        assert archives

        injected = memory.context_provider(cwd="/tmp/sample", agents_summary="pytest project")
        assert len(injected) <= 2000
    print("all memory tests passed")


if __name__ == "__main__":
    test_memory()
