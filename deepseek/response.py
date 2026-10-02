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

import json
import time
from pathlib import Path
from typing import Iterable, Optional, Protocol

from .tools import ToolCall

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

# Freeform apply_patch delimiters (Codex/OpenAI format, no JSON wrapper).
PATCH_BEGIN = "*** Begin Patch"
PATCH_END = "*** End Patch"
PATCH_TOOL_NAME = "apply_patch"


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


def find_freeform_patches(text: str) -> list[tuple[int, int, str]]:
    """Return ``(start, stop, payload)`` for every complete patch block.

    Only complete blocks (with a matching ``*** End Patch``) are returned;
    an unterminated ``*** Begin Patch`` is ignored.
    """
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    while True:
        start = text.find(PATCH_BEGIN, cursor)
        if start == -1:
            return spans
        end = text.find(PATCH_END, start + len(PATCH_BEGIN))
        if end == -1:
            return spans
        stop = end + len(PATCH_END)
        spans.append((start, stop, text[start:stop]))
        cursor = stop


def freeform_patch_payloads(text: str) -> list[str]:
    return [payload for _, _, payload in find_freeform_patches(text)]


def strip_freeform_patches(text: str) -> str:
    """Remove complete freeform patch blocks, leaving surrounding prose."""
    spans = find_freeform_patches(text)
    if not spans:
        return text
    out: list[str] = []
    cursor = 0
    for start, stop, _ in spans:
        out.append(text[cursor:start])
        cursor = stop
    out.append(text[cursor:])
    return "".join(out)


def _partial_marker_hold(text: str, marker: str) -> int:
    """Length of the longest suffix of *text* that starts *marker*."""
    for n in range(min(len(marker) - 1, len(text)), 0, -1):
        if text.endswith(marker[:n]):
            return n
    return 0


class PatchStreamFilter:
    """Suppress freeform patch blocks from a *streamed* answer.

    The marker may be split across chunks (``"*** Beg"`` then
    ``"in Patch\\n"``), so the filter holds back any trailing text that could
    still turn out to be a marker and drops everything up to the matching
    ``*** End Patch``. If the stream ends with the block still unterminated,
    :meth:`flush` releases the held text unchanged so an incomplete block
    behaves exactly like :func:`strip_freeform_patches` (i.e. stays visible).
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._in_patch = False

    def feed(self, text: str) -> str:
        self._buffer += text
        out: list[str] = []
        while True:
            if self._in_patch:
                index = self._buffer.find(PATCH_END)
                if index == -1:
                    return "".join(out)
                self._buffer = self._buffer[index + len(PATCH_END):]
                self._in_patch = False
                continue
            index = self._buffer.find(PATCH_BEGIN)
            if index != -1:
                out.append(self._buffer[:index])
                self._buffer = self._buffer[index:]
                self._in_patch = True
                continue
            hold = _partial_marker_hold(self._buffer, PATCH_BEGIN)
            if hold:
                out.append(self._buffer[:-hold])
                self._buffer = self._buffer[-hold:]
            else:
                out.append(self._buffer)
                self._buffer = ""
            return "".join(out)

    def flush(self) -> str:
        text = self._buffer
        self._buffer, self._in_patch = "", False
        return text


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
        """Tool-call payloads found in RESPONSE fragments.

        Includes freeform patch blocks, which are synthesised into the JSON
        form so callers that only look at signals see them as tool calls.
        """
        calls = list(self.tool_call_parts)
        if not calls:
            # Defensive: a RESPONSE fragment may contain an unfenced block if a
            # caller bypasses the SSE parser.
            calls = tool_call_blocks(self.raw_answer)
        calls.extend(
            json.dumps({"name": PATCH_TOOL_NAME, "arguments": {"patch": payload}},
                       ensure_ascii=False)
            for payload in freeform_patch_payloads(self.raw_answer)
        )
        return calls

    @property
    def freeform_patch_calls(self) -> list[ToolCall]:
        """Complete freeform patch blocks, synthesised as apply_patch calls."""
        return [
            ToolCall(
                name=PATCH_TOOL_NAME,
                arguments={"patch": payload},
                raw=json.dumps(
                    {"name": PATCH_TOOL_NAME, "arguments": {"patch": payload}},
                    ensure_ascii=False,
                ),
            )
            for payload in freeform_patch_payloads(self.raw_answer)
        ]

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
        text = strip_tool_call_blocks(self.raw_answer)
        return strip_done(strip_freeform_patches(text))


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
