#!/usr/bin/env python3
"""
Offline tests for the four new built-in tools:
  edit_file, grep, find_files, fetch_url

Run:
    PYTHONPATH=. python tests/test_tui_tools.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deepseek_tui import edit_file, grep, find_files, fetch_url


def _call(tool, **kwargs):
    """Call a Tool object with keyword arguments."""
    return tool.fn(**kwargs)


def make_tree(base: Path):
    """Create a small directory tree for testing."""
    (base / "a.py").write_text("hello world\nhello again\nfoo bar\n")
    (base / "b.txt").write_text("unique line\nanother line\n")
    (base / "sub").mkdir()
    (base / "sub" / "c.py").write_text("def main():\n    pass\n")
    # Simulate skip dirs
    (base / "__pycache__").mkdir()
    (base / "__pycache__" / "skip_me.pyc").write_bytes(b"\x00binary")
    (base / ".git").mkdir()
    (base / ".git" / "config").write_text("[core]\n")
    (base / "venv").mkdir()
    (base / "venv" / "skip.py").write_text("should not appear\n")


# ─────────────────────────────────────────────
# edit_file tests
# ─────────────────────────────────────────────

def test_edit_file(base: Path):
    p = base / "edit_test.txt"
    p.write_text("alpha beta alpha\n")

    # Not found → error
    result = _call(edit_file, path=str(p), old_string="gamma", new_string="X")
    assert "not found" in result, f"expected 'not found', got: {result!r}"

    # Ambiguous (2 occurrences) → error
    result = _call(edit_file, path=str(p), old_string="alpha", new_string="X")
    assert "2 times" in result, f"expected ambiguity error, got: {result!r}"

    # replace_all=True on ambiguous → works
    result = _call(edit_file, path=str(p), old_string="alpha", new_string="Z", replace_all=True)
    assert "replaced 2 occurrence(s)" in result, f"got: {result!r}"
    assert p.read_text() == "Z beta Z\n"

    # Unique replacement
    p.write_text("only once here\n")
    result = _call(edit_file, path=str(p), old_string="once", new_string="ONE")
    assert "replaced 1 occurrence(s)" in result, f"got: {result!r}"
    assert "ONE" in p.read_text()

    # Non-existent file
    result = _call(edit_file, path=str(base / "no_such.txt"), old_string="x", new_string="y")
    assert result.startswith("error"), f"got: {result!r}"

    # File > 1 MB
    big = base / "big.txt"
    big.write_bytes(b"x" * (1024 * 1024 + 1))
    result = _call(edit_file, path=str(big), old_string="x", new_string="y")
    assert "exceeds 1 MB" in result, f"got: {result!r}"

    print("  edit_file: all assertions passed")


# ─────────────────────────────────────────────
# grep tests
# ─────────────────────────────────────────────

def test_grep(base: Path):
    # Basic match in a.py
    result = _call(grep, pattern="hello", path=str(base))
    assert "a.py" in result, f"expected a.py in results, got: {result!r}"
    assert "hello" in result, f"got: {result!r}"

    # Format: relpath:lineno: line
    lines = result.strip().split("\n")
    first = lines[0]
    assert ":" in first, f"expected relpath:lineno format, got: {first!r}"
    parts = first.split(":")
    assert parts[1].strip().isdigit() or parts[1].isdigit(), f"lineno not numeric: {first!r}"

    # .git and venv are skipped
    result_all = _call(grep, pattern="should not appear", path=str(base))
    assert "should not appear" not in result_all or result_all.startswith("no matches"), \
        f"venv file should be skipped, got: {result_all!r}"

    result_git = _call(grep, pattern="\\[core\\]", path=str(base))
    assert result_git.startswith("no matches") or ".git" not in result_git, \
        f".git should be skipped, got: {result_git!r}"

    # max_results cap
    result_cap = _call(grep, pattern=".", path=str(base), max_results=2)
    # Should have at most 2 result lines + optional truncation notice
    match_lines = [l for l in result_cap.strip().split("\n") if not l.startswith("…")]
    assert len(match_lines) <= 2, f"max_results not respected: {match_lines}"
    assert "truncated" in result_cap, f"expected truncation notice, got: {result_cap!r}"

    # glob filter
    result_py = _call(grep, pattern="hello", path=str(base), glob="*.py")
    assert "a.py" in result_py
    result_txt = _call(grep, pattern="hello", path=str(base), glob="*.txt")
    assert result_txt.startswith("no matches"), f"hello not in *.txt, got: {result_txt!r}"

    # No matches
    result_none = _call(grep, pattern="ZZZNOMATCH", path=str(base))
    assert result_none.startswith("no matches"), f"got: {result_none!r}"

    # Invalid regex
    result_bad = _call(grep, pattern="[unclosed", path=str(base))
    assert "invalid regex" in result_bad, f"got: {result_bad!r}"

    print("  grep: all assertions passed")


# ─────────────────────────────────────────────
# find_files tests
# ─────────────────────────────────────────────

def test_find_files(base: Path):
    # Find all .py files — should include a.py and sub/c.py, NOT venv/skip.py or __pycache__
    result = _call(find_files, pattern="*.py", path=str(base))
    lines = result.strip().split("\n")
    assert any("a.py" in l for l in lines), f"a.py not found: {lines}"
    assert any("c.py" in l for l in lines), f"sub/c.py not found: {lines}"
    assert not any("venv" in l for l in lines), f"venv should be skipped: {lines}"
    assert not any("__pycache__" in l for l in lines), f"__pycache__ should be skipped: {lines}"

    # Output is sorted
    py_lines = [l for l in lines if not l.startswith("…")]
    assert py_lines == sorted(py_lines), f"output not sorted: {py_lines}"

    # .txt files
    result_txt = _call(find_files, pattern="*.txt", path=str(base))
    assert "b.txt" in result_txt, f"b.txt not found: {result_txt!r}"

    # max_results cap
    result_cap = _call(find_files, pattern="*", path=str(base), max_results=2)
    cap_lines = [l for l in result_cap.strip().split("\n") if not l.startswith("…")]
    assert len(cap_lines) <= 2, f"max_results not respected: {cap_lines}"
    assert "truncated" in result_cap, f"expected truncation notice: {result_cap!r}"

    # No matches
    result_none = _call(find_files, pattern="*.xyz", path=str(base))
    assert result_none.startswith("no files"), f"got: {result_none!r}"

    print("  find_files: all assertions passed")


# ─────────────────────────────────────────────
# fetch_url tests
# ─────────────────────────────────────────────

def test_fetch_url():
    # Real fetch — example.com is stable
    result = _call(fetch_url, url="https://example.com")
    assert isinstance(result, str), "result must be a str"
    assert len(result) > 0, "result must not be empty"
    # Should contain readable text (tags stripped)
    assert "<html" not in result.lower(), f"HTML tags not stripped: {result[:200]!r}"
    print(f"  fetch_url(https://example.com): got {len(result)} chars — OK")

    # Bogus scheme → error string
    result_ftp = _call(fetch_url, url="ftp://example.com/file.txt")
    assert result_ftp.startswith("error"), f"expected error for ftp://, got: {result_ftp!r}"
    print(f"  fetch_url(ftp://...): error returned — OK")

    # Unreachable host → error string (not raise)
    result_bad = _call(fetch_url, url="http://this-host-definitely-does-not-exist-xyz-abc-123.invalid/path")
    assert result_bad.startswith("error"), f"expected error for bad host, got: {result_bad!r}"
    print(f"  fetch_url(bad host): error returned — OK")

    # max_bytes cap
    result_small = _call(fetch_url, url="https://example.com", max_bytes=50)
    assert len(result_small) <= 200, f"response too long: {len(result_small)}"  # some slack for "… (truncated)"
    print(f"  fetch_url max_bytes cap: OK")

    print("  fetch_url: all assertions passed")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        make_tree(base)

        print("Running edit_file tests…")
        test_edit_file(base)

        print("Running grep tests…")
        test_grep(base)

        print("Running find_files tests…")
        test_find_files(base)

    print("Running fetch_url tests (live network)…")
    test_fetch_url()

    print("\nall tool tests passed")


if __name__ == "__main__":
    main()
