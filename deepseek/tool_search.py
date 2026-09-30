"""Deferred tool loading with pure-stdlib BM25 search."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Any, Iterable

from .agent_tools import get_agent_runtime
from .tools import Tool, tool


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _parameter_names(parameters: dict[str, Any]) -> list[str]:
    properties = parameters.get("properties") if isinstance(parameters, dict) else None
    if not isinstance(properties, dict):
        return []
    names: list[str] = []
    for name, schema in properties.items():
        names.append(str(name))
        if isinstance(schema, dict):
            items = schema.get("items")
            if isinstance(items, dict):
                names.extend(_parameter_names({"properties": {"item": items}}))
    return names


class BM25Index:
    """Small BM25 implementation over tool metadata documents."""

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


class DeferredToolRegistry:
    """Tracks eager tools and one-turn visibility for deferred matches."""

    def __init__(self, eager_tools: Iterable[Tool] = (), deferred_tools: Iterable[Tool] = ()):
        self.eager_tools: list[Tool] = []
        self.deferred_tools: list[Tool] = []
        self._index: BM25Index | None = None
        self._signature: tuple[tuple[str, str, str], ...] | None = None
        self._pending_matches: list[Tool] = []
        self._served_pending = False
        self.update(eager_tools, deferred_tools)

    def update(self, eager_tools: Iterable[Tool], deferred_tools: Iterable[Tool]) -> None:
        self.eager_tools = list(eager_tools)
        self.deferred_tools = list(deferred_tools)
        self._pending_matches = []
        self._served_pending = False
        self._index = None
        self._signature = None

    def _documents(self) -> list[list[str]]:
        docs: list[list[str]] = []
        for tool_obj in self.deferred_tools:
            parts = [
                tool_obj.name,
                tool_obj.description,
                " ".join(_parameter_names(tool_obj.parameters)),
            ]
            docs.append(tokenize(" ".join(parts)))
        return docs

    def _ensure_index(self) -> None:
        signature = tuple(
            (
                tool_obj.name,
                tool_obj.description,
                json.dumps(tool_obj.parameters, sort_keys=True, default=str),
            )
            for tool_obj in self.deferred_tools
        )
        if self._index is not None and self._signature == signature:
            return
        self._signature = signature
        self._index = BM25Index(self._documents())

    @staticmethod
    def _normalize_name(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")

    def _exact_matches(self, query: str) -> list[Tool]:
        query_norm = self._normalize_name(query)
        query_tokens = set(tokenize(query))
        matches: list[Tool] = []
        for tool_obj in self.deferred_tools:
            name_norm = self._normalize_name(tool_obj.name)
            if name_norm == query_norm or name_norm in query_tokens:
                matches.append(tool_obj)
        return matches

    def search(self, query: str, limit: int = 5) -> list[Tool]:
        self._ensure_index()
        assert self._index is not None
        limit = max(1, int(limit))
        exact = self._exact_matches(query)
        exact_names = {tool_obj.name for tool_obj in exact}
        ranked: list[Tool] = []
        for idx, score in self._index.rank(query):
            if score <= 0:
                continue
            tool_obj = self.deferred_tools[idx]
            if tool_obj.name in exact_names:
                continue
            ranked.append(tool_obj)
        matches = (exact + ranked)[:limit]
        self._pending_matches = matches
        self._served_pending = False
        return matches

    def visible_tools(self) -> list[Tool]:
        visible = list(self.eager_tools)
        if self._pending_matches and not self._served_pending:
            visible.extend(self._pending_matches)
            self._served_pending = True
        deduped: dict[str, Tool] = {}
        for tool_obj in visible:
            deduped[tool_obj.name] = tool_obj
        return list(deduped.values())

    def finish_iteration(self) -> None:
        if self._served_pending:
            self._pending_matches = []
            self._served_pending = False

    def schema_text(self, limit: int = 5) -> str:
        visible = self.visible_tools()
        return "\n".join(
            f"- {tool_obj.name}: {tool_obj.description}" for tool_obj in visible[:limit]
        )


@tool(deferred=False, read_only=False)
def search_tools(query: str, limit: int = 5) -> str:
    """Search deferred tools by capability and make the best matches available next turn.

    Args:
        query: Capability query or exact tool name.
        limit: Maximum number of matching tools to return.
    """
    runtime = get_agent_runtime()
    registry = runtime.tool_registry if runtime else None
    if registry is None:
        return "Error: deferred tool registry is not available."
    matches = registry.search(query, limit=limit)
    payload = {
        "query": query,
        "note": "Matched tools are available for one turn only. Call search_tools again if you need them later.",
        "tools": [
            {
                "name": tool_obj.name,
                "description": tool_obj.description,
                "parameters": tool_obj.parameters,
            }
            for tool_obj in matches
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)
