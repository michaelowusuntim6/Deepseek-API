"""Shared pure-stdlib BM25 implementation."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable


def tokenize(text: str) -> list[str]:
    """Lowercase and split on non-alphanumeric characters."""
    return re.findall(r"[a-z0-9]+", text.lower())


class BM25:
    """Small BM25 index over tokenized documents."""

    def __init__(self, documents: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.documents = documents
        self.k1 = k1
        self.b = b
        self.doc_freqs: list[Counter[str]] = [Counter(doc) for doc in documents]
        self.doc_lengths = [len(doc) for doc in documents]
        self.avgdl = (sum(self.doc_lengths) / len(self.doc_lengths)) if documents else 0.0
        df: Counter[str] = Counter()
        for doc in self.doc_freqs:
            df.update(doc.keys())
        n = len(documents)
        self.idf = {
            term: math.log((n - freq + 0.5) / (freq + 0.5) + 1.0)
            for term, freq in df.items()
        }

    def score(self, query: str, index: int) -> float:
        if not self.documents:
            return 0.0
        terms = tokenize(query)
        freq = self.doc_freqs[index]
        dl = self.doc_lengths[index] or 1
        score = 0.0
        for term in terms:
            if term not in freq:
                continue
            idf = self.idf.get(term, 0.0)
            tf = freq[term]
            denom = tf + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1.0))
            score += idf * (tf * (self.k1 + 1)) / denom
        return score

    def rank(self, query: str) -> list[tuple[int, float]]:
        scored = [(idx, self.score(query, idx)) for idx in range(len(self.documents))]
        return sorted(scored, key=lambda item: (-item[1], item[0]))

    def search_with_scores(
        self, query: str, top_k: int | None = None
    ) -> list[tuple[int, float, int]]:
        """Return (doc_id, score, token_count), highest score first."""
        scored = [
            (idx, score, self.doc_lengths[idx])
            for idx, score in self.rank(query)
        ]
        if top_k is not None:
            scored = scored[: max(0, int(top_k))]
        return scored

    @staticmethod
    def exact_name_matches(query: str, names: Iterable[str]) -> list[int]:
        """Return doc indices whose normalized name exactly appears in query."""
        query_norm = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")
        query_tokens = set(tokenize(query))
        matches: list[int] = []
        for idx, name in enumerate(names):
            name_norm = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
            if (
                name_norm == query_norm
                or name_norm in query_norm
                or name_norm in query_tokens
            ):
                matches.append(idx)
        return matches
