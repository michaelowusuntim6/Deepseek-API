#!/usr/bin/env python3
"""Offline end-to-end smoke tests for the Rich CLI entry point."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "deepseek_cli.py"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_help() -> None:
    r = run_cli("--help")
    assert r.returncode == 0, r.stderr
    assert "usage:" in r.stdout
    assert "--tools" in r.stdout
    print("  PASS: --help exits 0 and prints usage")


def test_bad_usage() -> None:
    r = run_cli("--model", "nonsense", "hi")
    assert r.returncode == 3, (r.returncode, r.stderr)
    assert "--model" in r.stderr
    print("  PASS: invalid flag exits 3")


def test_resume_model_conflict() -> None:
    r = run_cli("--resume", "abc:1", "--model", "expert", "hi")
    assert r.returncode == 3, (r.returncode, r.stderr)
    assert "cannot be combined" in r.stderr
    print("  PASS: invalid flag combination exits 3")


if __name__ == "__main__":
    test_help()
    test_bad_usage()
    test_resume_model_conflict()
    print("all CLI e2e smoke tests passed")
