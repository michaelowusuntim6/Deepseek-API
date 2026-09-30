"""
tests/test_extensions.py — Offline tests for deepseek/extensions.py.

Run with:
    PYTHONPATH=. python tests/test_extensions.py

Must exit 0 and print "all extension tests passed".
"""

import sys
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deepseek.extensions as ext_mod
from deepseek.extensions import (
    load_extensions,
    load_extension_commands,
    reload_extensions,
)
from deepseek.tools import Tool


# ─── helpers ──────────────────────────────────────────────────────────────────

def make_ext_dir(tmp: Path) -> Path:
    d = tmp / "extensions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_py(directory: Path, name: str, code: str) -> Path:
    p = directory / name
    p.write_text(code, encoding="utf-8")
    return p


def reset(search_paths: list[Path]):
    """Reload the module's cached state and patch search paths."""
    reload_extensions()
    # Patch _search_paths to return only our temp dirs
    patch.object(ext_mod, "_search_paths", return_value=search_paths).start()


def stop_patches():
    patch.stopall()
    reload_extensions()


def assert_tool(tools, name: str, msg: str = ""):
    names = [t.name for t in tools]
    assert name in names, f"Expected tool '{name}' in {names}. {msg}"


def assert_not_tool(tools, name: str, msg: str = ""):
    names = [t.name for t in tools]
    assert name not in names, f"Did NOT expect tool '{name}' in {names}. {msg}"


# ─── tests ────────────────────────────────────────────────────────────────────

def test_single_tool(tmp: Path):
    d = make_ext_dir(tmp / "t1")
    write_py(d, "alpha.py", """\
from deepseek import tool

@tool
def alpha_tool(x: str) -> str:
    \"\"\"Alpha tool.\"\"\"
    return x

def register():
    return alpha_tool
""")
    reset([d])
    tools = load_extensions()
    assert_tool(tools, "alpha_tool", "single tool not loaded")
    print("  PASS: single tool loads")


def test_list_of_tools(tmp: Path):
    d = make_ext_dir(tmp / "t2")
    write_py(d, "multi.py", """\
from deepseek import tool

@tool
def tool_a(x: str) -> str:
    \"\"\"A.\"\"\"
    return x

@tool
def tool_b(x: str) -> str:
    \"\"\"B.\"\"\"
    return x

def register():
    return [tool_a, tool_b]
""")
    reset([d])
    tools = load_extensions()
    assert_tool(tools, "tool_a", "tool_a missing")
    assert_tool(tools, "tool_b", "tool_b missing")
    print("  PASS: list of tools loads all")


def test_no_register_skipped(tmp: Path, capsys_stderr: list):
    d = make_ext_dir(tmp / "t3")
    write_py(d, "noreg.py", """\
# no register() function
x = 42
""")
    reset([d])
    import io
    old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        tools = load_extensions()
    finally:
        captured = sys.stderr.getvalue()
        sys.stderr = old_stderr
    assert len(tools) == 0, f"Expected 0 tools, got {[t.name for t in tools]}"
    assert "no register" in captured or "noreg.py" in captured, \
        f"Expected warning about missing register(). Got: {captured!r}"
    print("  PASS: extension without register() is skipped with warning")


def test_register_raises_skipped(tmp: Path):
    d = make_ext_dir(tmp / "t4")
    write_py(d, "good.py", """\
from deepseek import tool

@tool
def good_tool(x: str) -> str:
    \"\"\"Good.\"\"\"
    return x

def register():
    return good_tool
""")
    write_py(d, "bad.py", """\
def register():
    raise RuntimeError("intentional error")
""")
    reset([d])
    import io
    old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        tools = load_extensions()
    finally:
        captured = sys.stderr.getvalue()
        sys.stderr = old_stderr
    assert_tool(tools, "good_tool", "good_tool should still load despite bad.py")
    assert "intentional error" in captured or "bad.py" in captured, \
        f"Expected error from bad.py in stderr. Got: {captured!r}"
    print("  PASS: register() exception is caught; other extensions still load")


def test_underscore_files_skipped(tmp: Path):
    d = make_ext_dir(tmp / "t5")
    write_py(d, "_helper.py", """\
from deepseek import tool

@tool
def secret_tool(x: str) -> str:
    \"\"\"Should not be loaded.\"\"\"
    return x

def register():
    return secret_tool
""")
    reset([d])
    tools = load_extensions()
    assert_not_tool(tools, "secret_tool", "_helper.py should be skipped")
    print("  PASS: files starting with _ are skipped")


def test_name_collision_later_wins(tmp: Path):
    dir1 = make_ext_dir(tmp / "t6a")
    dir2 = make_ext_dir(tmp / "t6b")
    write_py(dir1, "ext_a.py", """\
from deepseek import tool

@tool
def shared_tool(x: str) -> str:
    \"\"\"From ext_a.\"\"\"
    return "from_a"

def register():
    return shared_tool
""")
    write_py(dir2, "ext_b.py", """\
from deepseek import tool

@tool
def shared_tool(x: str) -> str:
    \"\"\"From ext_b.\"\"\"
    return "from_b"

def register():
    return shared_tool
""")
    # dir1 first, dir2 second — dir2 (later) should win
    reset([dir1, dir2])
    import io
    old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        tools = load_extensions()
    finally:
        captured = sys.stderr.getvalue()
        sys.stderr = old_stderr

    shared = [t for t in tools if t.name == "shared_tool"]
    assert len(shared) == 1, f"Expected 1 shared_tool, got {len(shared)}"
    result = shared[0].call(x="test")
    assert result == "from_b", f"Later path should win: got '{result}'"
    print("  PASS: name collision — later path wins")


def test_commands_loaded(tmp: Path):
    d = make_ext_dir(tmp / "t7")
    write_py(d, "withcmds.py", """\
from deepseek import tool

@tool
def cmd_tool(x: str) -> str:
    \"\"\"Cmd tool.\"\"\"
    return x

COMMANDS = {
    "/mycmd": lambda: "hello",
}

def register():
    return cmd_tool
""")
    reset([d])
    cmds = load_extension_commands()
    assert "/mycmd" in cmds, f"Expected /mycmd in commands: {list(cmds)}"
    print("  PASS: COMMANDS dict is picked up")


def test_builtin_command_clash_skipped(tmp: Path):
    """Extension commands that clash with built-ins are silently skipped."""
    d = make_ext_dir(tmp / "t8")
    write_py(d, "clash.py", """\
from deepseek import tool

@tool
def clash_tool(x: str) -> str:
    \"\"\"Clash.\"\"\"
    return x

COMMANDS = {
    "/mode": lambda: "this should be blocked",
    "/myok": lambda: "this is fine",
}

def register():
    return clash_tool
""")
    reset([d])
    import io
    old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        cmds = load_extension_commands()
    finally:
        captured = sys.stderr.getvalue()
        sys.stderr = old_stderr

    assert "/mode" not in cmds, "/mode is a built-in and should be blocked"
    assert "/myok" in cmds, "/myok should be allowed"
    assert "clashes" in captured or "clash" in captured or "mode" in captured, \
        f"Expected clash warning in stderr. Got: {captured!r}"
    print("  PASS: built-in slash-command clash is skipped with warning")


def test_import_error_skipped(tmp: Path):
    d = make_ext_dir(tmp / "t9")
    write_py(d, "broken.py", """\
import this_module_does_not_exist_xyz_123

def register():
    pass
""")
    write_py(d, "fine.py", """\
from deepseek import tool

@tool
def fine_tool(x: str) -> str:
    \"\"\"Fine.\"\"\"
    return x

def register():
    return fine_tool
""")
    reset([d])
    import io
    old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        tools = load_extensions()
    finally:
        captured = sys.stderr.getvalue()
        sys.stderr = old_stderr

    assert_tool(tools, "fine_tool", "fine_tool should load despite broken.py")
    assert "broken.py" in captured or "ModuleNotFoundError" in captured, \
        f"Expected import error in stderr. Got: {captured!r}"
    print("  PASS: import error is caught; other extensions still load")


# ─── runner ───────────────────────────────────────────────────────────────────

def run_all():
    tests = [
        test_single_tool,
        test_list_of_tools,
        test_no_register_skipped,
        test_register_raises_skipped,
        test_underscore_files_skipped,
        test_name_collision_later_wins,
        test_commands_loaded,
        test_builtin_command_clash_skipped,
        test_import_error_skipped,
    ]

    passed = 0
    failed = 0

    for test_fn in tests:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            try:
                capsys_stderr: list = []
                # single-arg tests pass tmp; two-arg tests also pass capsys_stderr
                import inspect
                params = list(inspect.signature(test_fn).parameters)
                if len(params) == 1:
                    test_fn(tmp)
                else:
                    test_fn(tmp, capsys_stderr)
                passed += 1
            except AssertionError as e:
                print(f"  FAIL: {test_fn.__name__}: {e}")
                failed += 1
            except Exception as e:
                import traceback
                print(f"  ERROR: {test_fn.__name__}: {type(e).__name__}: {e}")
                traceback.print_exc()
                failed += 1
            finally:
                stop_patches()

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
    print("all extension tests passed")


if __name__ == "__main__":
    run_all()
