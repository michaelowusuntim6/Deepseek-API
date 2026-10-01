#!/usr/bin/env python3
"""Tests for prompt_toolkit slash completions."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from prompt_toolkit.document import Document

from deepseek.repl import SlashCommandCompleter


def complete(completer: SlashCommandCompleter, text: str):
    return list(completer.get_completions(Document(text), None))


def test_repl_completions() -> None:
    completer = SlashCommandCompleter(
        [("/model", "Model"), ("/mode", "Mode"), ("/new", "New")],
        {"/model": [("chat", "Fast"), ("expert", "Strong")]},
    )
    assert [c.text for c in complete(completer, "/")] == ["/new", "/mode", "/model"]
    assert [c.text for c in complete(completer, "/mo")] == ["/mode", "/model"]
    assert [c.text for c in complete(completer, "/model")][0] == "/model"
    assert [c.text for c in complete(completer, "/model ")] == ["chat", "expert"]
    assert [c.text for c in complete(completer, "/model ch")] == ["chat"]
    assert complete(completer, "plain text") == []
    print("all repl tests passed")


if __name__ == "__main__":
    test_repl_completions()
