#!/usr/bin/env python3
"""
Offline tests for the CLI built-in tools:
  edit_file, grep, find_files

Run:
    PYTHONPATH=. venv/bin/python tests/test_cli_tools.py
"""

import tempfile
from pathlib import Path

from deepseek_cli import edit_file, find_files, grep


def _call(tool_obj, **kwargs):
    return tool_obj.fn(**kwargs)


def _make_tree(base: Path) -> None:
    (base / "a.py").write_text("hello world\nhello again\nfoo bar\n")
    (base / "b.txt").write_text("unique line\nanother line\n")
    (base / "sub").mkdir()
    (base / "sub" / "c.py").write_text("def main():\n    pass\n")
    (base / "__pycache__").mkdir()
    (base / "__pycache__" / "skip_me.pyc").write_bytes(b"\x00binary")
    (base / ".git").mkdir()
    (base / ".git" / "config").write_text("[core]\n")
    (base / "venv").mkdir()
    (base / "venv" / "skip.py").write_text("should not appear\n")


def test_edit_file(base: Path) -> None:
    p = base / "edit_test.txt"
    p.write_text("alpha beta alpha\n")

    assert "not found" in _call(edit_file, path=str(p), old_string="gamma", new_string="X")
    assert "2 times" in _call(edit_file, path=str(p), old_string="alpha", new_string="X")
    assert "replaced 2 occurrence(s)" in _call(
        edit_file, path=str(p), old_string="alpha", new_string="Z", replace_all=True
    )
    assert p.read_text() == "Z beta Z\n"

    p.write_text("only once here\n")
    assert "replaced 1 occurrence(s)" in _call(
        edit_file, path=str(p), old_string="once", new_string="ONE"
    )
    assert "ONE" in p.read_text()

    assert _call(
        edit_file, path=str(base / "no_such.txt"), old_string="x", new_string="y"
    ).startswith("error")

    big = base / "big.txt"
    big.write_bytes(b"x" * (1024 * 1024 + 1))
    assert "exceeds 1 MB" in _call(
        edit_file, path=str(big), old_string="x", new_string="y"
    )
    print("  PASS: edit_file")


def test_grep(base: Path) -> None:
    result = _call(grep, pattern="hello", path=str(base))
    assert "a.py" in result
    assert ":" in result.splitlines()[0]

    result_all = _call(grep, pattern="should not appear", path=str(base))
    assert "should not appear" not in result_all or result_all.startswith("no matches")
    assert _call(grep, pattern="\\[core\\]", path=str(base)).startswith("no matches")

    result_cap = _call(grep, pattern=".", path=str(base), max_results=2)
    assert len([l for l in result_cap.splitlines() if not l.startswith("…")]) <= 2
    assert "truncated" in result_cap

    assert "a.py" in _call(grep, pattern="hello", path=str(base), glob="*.py")
    assert _call(grep, pattern="hello", path=str(base), glob="*.txt").startswith("no matches")
    assert _call(grep, pattern="ZZZNOMATCH", path=str(base)).startswith("no matches")
    assert "invalid regex" in _call(grep, pattern="[unclosed", path=str(base))
    print("  PASS: grep")


def test_find_files(base: Path) -> None:
    result = _call(find_files, pattern="*.py", path=str(base))
    lines = result.strip().splitlines()
    assert any("a.py" in line for line in lines)
    assert any("c.py" in line for line in lines)
    assert not any("venv" in line for line in lines)
    assert not any("__pycache__" in line for line in lines)
    assert [l for l in lines if not l.startswith("…")] == sorted(
        [l for l in lines if not l.startswith("…")]
    )
    assert "b.txt" in _call(find_files, pattern="*.txt", path=str(base))

    result_cap = _call(find_files, pattern="*", path=str(base), max_results=2)
    assert len([l for l in result_cap.splitlines() if not l.startswith("…")]) <= 2
    assert "truncated" in result_cap
    assert _call(find_files, pattern="*.xyz", path=str(base)).startswith("no files")
    print("  PASS: find_files")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        _make_tree(base)
        test_edit_file(base)
        test_grep(base)
        test_find_files(base)
    print("all CLI tool tests passed")


if __name__ == "__main__":
    main()
