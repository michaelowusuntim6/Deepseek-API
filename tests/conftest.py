"""Shared pytest fixtures for the DeepSeek CLI test suite."""

import sys
from pathlib import Path

import pytest


@pytest.fixture
def tmp(tmp_path):
    """Alias for tmp_path, used by older tests in this repo."""
    return tmp_path


@pytest.fixture
def base(tmp_path):
    """Fresh scratch directory populated with the CLI tool test tree.

    ``tests/test_cli_tools.py::_make_tree`` is the single definition of the
    tree the grep/find_files/edit_file tests expect (``a.py``, ``b.txt``,
    ``sub/``, plus the skipped ``__pycache__``, ``.git`` and ``venv`` dirs).
    The module's standalone ``main`` runner builds it explicitly; under pytest
    the fixture has to build it, otherwise those tests run against an empty
    directory.
    """
    tests_dir = str(Path(__file__).resolve().parent)
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    from test_cli_tools import _make_tree

    _make_tree(tmp_path)
    return tmp_path


@pytest.fixture
def capsys_stderr(capsys):
    """List-like stderr accumulator for the extension-loading tests.

    The tests that accept this fixture capture stderr themselves (via
    ``io.StringIO``); those tests only need a list argument. Anything pytest
    captures for the test is appended when the fixture tears down so the list
    reflects real stderr lines either way.
    """
    captured: list[str] = []
    yield captured
    err = capsys.readouterr().err
    if err:
        captured.extend(err.splitlines())
