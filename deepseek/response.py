"""Response processing for the DeepSeek CLI (Strategy.md Parts I-III).

This module owns the completion protocol:

  * only RESPONSE fragments are searched for markers (THINK is never searched),
  * a tool call always wins over the completion marker,
  * the ``<<DONE>>`` marker is honoured only under its strict line rules,
  * exactly one tool call is executed per response; any further calls in the
    same response get a synthetic error result instead of being executed,
  * a response with neither a tool call nor a valid marker is nudged.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable, Optional, Protocol

DONE_TOKEN = "<<DONE>>"

# Exact nudge text (clarification 2). No hardcoded commands or example paths.
NUDGE_MESSAGE = (
    "No tool call or <<DONE>> detected. Finish properly: either emit a "
    "<tool_call> block now, or write <<DONE>> on its own line. Do not write prose."
)

MAX_NUDGES_PER_TURN = 5

MULTI_CALL_ERROR = (
    "TOOL RESULT for {name}:\n"
    "ERROR: Multiple tool calls in one response. Only the first was executed. "
    "Re-emit this call separately in the next turn."
)

_OPEN_TAG = "<tool_call>"
_CLOSE_TAG = "</tool_call>"


def multi_call_error(name: str) -> str:
    """Synthetic result returned for a tool call beyond the first one."""
    return MULTI_CALL_ERROR.format(name=name)


def done_line_indexes(text: str) -> list[int]:
    """Indexes of lines that consist of the completion marker alone."""
    return [i for i, line in enumerate(text.split("\n")) if line.strip() == DONE_TOKEN]


def has_valid_done(text: str) -> bool:
    """Strict ``<<DONE>>`` validation (clarification 1).

    The response must contain exactly one occurrence, on its own line,
    preceded by a blank line, and followed by a blank line or the end of
    the response.
    """
    if text.count(DONE_TOKEN) != 1:
        return False
    lines = text.split("\n")
    indexes = done_line_indexes(text)
    if len(indexes) != 1:
        return False
    i = indexes[0]
    if i == 0 or lines[i - 1].strip() != "":
        return False
    if i != len(lines) - 1 and lines[i + 1].strip() != "":
        return False
    return True


def strip_done(text: str) -> str:
    """Remove every ``<<DONE>>`` from user-visible output.

    A marker standing alone on a line takes its surrounding blank lines with
    it; an inline marker is removed in place so its line keeps any prose.
    """
    kept: list[str] = []
    for line in text.split("\n"):
        if DONE_TOKEN not in line:
            kept.append(line)
            continue
        remainder = line.replace(DONE_TOKEN, "").strip()
        if remainder:
            kept.append(line.replace(DONE_TOKEN, "").rstrip())
            continue
        while kept and kept[-1].strip() == "":
            kept.pop()
    while kept and kept[-1].strip() == "":
        kept.pop()
    return "\n".join(kept)


def strip_tool_call_blocks(text: str) -> str:
    """Remove ``<tool_call>...</tool_call>`` blocks from visible text."""
    out: list[str] = []
    pos = 0
    while True:
        start = text.find(_OPEN_TAG, pos)
        if start == -1:
            out.append(text[pos:])
            return "".join(out)
        out.append(text[pos:start])
        end = text.find(_CLOSE_TAG, start + len(_OPEN_TAG))
        if end == -1:
            return "".join(out)
        pos = end + len(_CLOSE_TAG)


def tool_call_blocks(text: str) -> list[str]:
    """Raw payloads of ``<tool_call>...</tool_call>`` blocks found in text."""
    blocks: list[str] = []
    pos = 0
    while True:
        start = text.find(_OPEN_TAG, pos)
        if start == -1:
            return blocks
        end = text.find(_CLOSE_TAG, start + len(_OPEN_TAG))
        if end == -1:
            return blocks
        blocks.append(text[start + len(_OPEN_TAG):end].strip())
        pos = end + len(_CLOSE_TAG)


class SupportsExecute(Protocol):
    def execute(self, calls, tools, decisions=None): ...


class ResponseProcessor:
    """Accumulate one response's streamed parts and apply the protocol."""

    def __init__(self) -> None:
        self.thinking_parts: list[str] = []
        self.raw_answer_parts: list[str] = []
        self.tool_call_parts: list[str] = []
        self.answer_parts: list[str] = []

    def feed(self, kind: str, text: str) -> None:
        if kind == "thinking":
            # THINK fragments are recorded verbatim and never searched.
            self.thinking_parts.append(text)
            return
        if kind == "tool_call":
            # The SSE parser only emits this kind from RESPONSE fragments.
            self.tool_call_parts.append(text)
            return
        if kind != "answer":
            return
        self.raw_answer_parts.append(text)
        visible = strip_tool_call_blocks(text)
        if visible:
            self.answer_parts.append(visible)

    def feed_all(self, parts: Iterable[tuple[str, str]]) -> "ResponseProcessor":
        for kind, text in parts:
            self.feed(kind, text)
        return self

    @property
    def thinking(self) -> str:
        return "".join(self.thinking_parts)

    @property
    def raw_answer(self) -> str:
        return "".join(self.raw_answer_parts)

    @property
    def tool_calls(self) -> list[str]:
        """Tool-call payloads found in RESPONSE fragments."""
        calls = list(self.tool_call_parts)
        if not calls:
            # Defensive: a RESPONSE fragment may contain an unfenced block if a
            # caller bypasses the SSE parser.
            calls = tool_call_blocks(self.raw_answer)
        return calls

    @property
    def done(self) -> bool:
        return has_valid_done(strip_tool_call_blocks(self.raw_answer))

    @property
    def signal(self) -> Optional[str]:
        """``"tool_call"``, ``"done"``, or ``None`` for an incomplete turn."""
        if self.tool_calls:
            return "tool_call"
        if self.done:
            return "done"
        return None

    def visible_answer(self) -> str:
        """Answer text with tool calls and the completion marker stripped."""
        return strip_done(strip_tool_call_blocks(self.raw_answer))


def split_tool_calls(calls: list) -> tuple[list, list[tuple[object, str]]]:
    """Return ``(to_execute, synthetic_errors)`` for one response's calls."""
    if len(calls) <= 1:
        return list(calls), []
    return list(calls[:1]), [(call, multi_call_error(call.name)) for call in calls[1:]]


def execute_first_tool_call(
    executor: SupportsExecute,
    calls: list,
    tools: list,
    decisions: Optional[list[bool]] = None,
) -> list[tuple[object, str]]:
    """Execute only the first call; synthesize errors for the rest."""
    to_execute, results = split_tool_calls(calls)
    if not to_execute:
        return results
    first_decisions = decisions[:1] if decisions is not None else None
    executed = executor.execute(to_execute, tools, decisions=first_decisions)
    return list(executed) + results


class TurnNudger:
    """Track consecutive incomplete responses within one user turn.

    The counter resets after any successful tool call; every incomplete
    response nudges the model and increments it. The turn gives up once it
    reaches ``max_nudges`` consecutive failures.
    """

    def __init__(self, max_nudges: int = MAX_NUDGES_PER_TURN) -> None:
        self.max_nudges = int(max_nudges)
        self.consecutive_failures = 0
        self.total_nudges = 0
        self.gave_up = False

    def note(self, signal: Optional[str]) -> str:
        """Record one response: ``complete``, ``tool_call``, ``nudge``, ``give_up``."""
        if signal == "tool_call":
            self.consecutive_failures = 0
            return "tool_call"
        if signal == "done":
            self.consecutive_failures = 0
            return "complete"
        self.consecutive_failures += 1
        self.total_nudges += 1
        if self.consecutive_failures >= self.max_nudges:
            self.gave_up = True
            return "give_up"
        return "nudge"

    def write_stop_file(self, text: str, directory: Path | str = "/tmp") -> Path:
        path = Path(directory) / f"prose_stop_{int(time.time())}.txt"
        path.write_text(text, encoding="utf-8")
        return path
