#!/usr/bin/env python3
"""Regression tests for the shared BM25 implementation."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from deepseek.bm25 import BM25, tokenize


def test_bm25() -> None:
    assert tokenize("Hello, World-42") == ["hello", "world", "42"]

    single = BM25([tokenize("python pytest testing")])
    assert single.search_with_scores("pytest")[0][0] == 0

    docs = [
        tokenize("python pytest testing framework"),
        tokenize("javascript react browser"),
        tokenize("rust cargo systems programming"),
    ]
    index = BM25(docs)
    ranked = index.search_with_scores("pytest framework", top_k=2)
    assert ranked[0][0] == 0
    assert ranked[0][1] > ranked[1][1]
    assert ranked[0][2] == 4

    exact = BM25.exact_name_matches("use get_weather now", ["get_weather", "other"])
    assert exact == [0]

    assert index.search_with_scores("") == [
        (0, 0.0, 4), (1, 0.0, 3), (2, 0.0, 4)
    ]
    print("all bm25 tests passed")


if __name__ == "__main__":
    test_bm25()
