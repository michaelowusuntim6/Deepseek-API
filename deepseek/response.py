"""Response processing for the DeepSeek CLI.

The harness accepts whatever the model naturally produces:

  * only RESPONSE fragments are searched for markers (THINK is never searched),
  * every tool call found in a response is extracted, whatever its format
    (``<tool_call>`` JSON, freeform ``*** Begin Patch`` blocks, or DSML
    ``invoke``/``parameter`` blocks), ordered by position in the text,
  * all of those calls are executed, in order,
  * completion is a heuristic: a valid ``<<DONE>>`` marker, an explicit
    completion phrase, or a substantial final answer,
  * an incomplete response gets a soft ``Continue.`` continuation rather than
    an assertive correction.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterable, Optional, Protocol

from .tools import ToolCall

DONE_TOKEN = "<<DONE>>"
WATCH_TOKEN = "<<WATCH>>"

MAX_CONTINUATIONS_PER_TURN = 5
# Backwards-compatible alias for callers written against the old name.
MAX_NUDGES_PER_TURN = MAX_CONTINUATIONS_PER_TURN

# Phrases that mark a finished task even without the completion marker.
COMPLETION_PHRASES = ("task complete", "task is complete", "all steps complete")

# A substantial answer that trails off with one of these is not complete.
FORWARD_LOOKING_PHRASES = (
    "i'll", "let me", "next i", "now i", "i need to", "i should",
)

SUBSTANTIAL_ANSWER_CHARS = 200

_OPEN_TAG = "<tool_call>"
_CLOSE_TAG = "</tool_call>"

# Freeform apply_patch delimiters (Codex/OpenAI format, no JSON wrapper).
PATCH_BEGIN = "*** Begin Patch"
PATCH_END = "*** End Patch"
PATCH_TOOL_NAME = "apply_patch"


def done_line_indexes(text: str) -> list[int]:
    """Indexes of lines that consist of the completion marker alone."""
    return [i for i, line in enumerate(text.split("\n")) if line.strip() == DONE_TOKEN]


def watch_line_indexes(text: str) -> list[int]:
    """Indexes of lines that consist of the watch marker alone."""
    return [i for i, line in enumerate(text.split("\n")) if line.strip() == WATCH_TOKEN]


def _has_valid_token(text: str, token: str, indexes: list[int]) -> bool:
    if text.count(token) != 1 or len(indexes) != 1:
        return False
    lines = text.split("\n")
    i = indexes[0]
    if i > 0 and lines[i - 1].strip() != "":
        return False
    if i != len(lines) - 1 and lines[i + 1].strip() != "":
        return False
    return True


def has_valid_done(text: str) -> bool:
    """Strict ``<<DONE>>`` validation (clarification 1).

    The response must contain exactly one occurrence, on its own line,
    preceded by a blank line, and followed by a blank line or the end of
    the response.
    """
    return _has_valid_token(text, DONE_TOKEN, done_line_indexes(text))


def has_valid_watch(text: str) -> bool:
    """Strict ``<<WATCH>>`` validation, the same shape as ``<<DONE>>``."""
    return _has_valid_token(text, WATCH_TOKEN, watch_line_indexes(text))


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


def strip_watch(text: str) -> str:
    """Remove every ``<<WATCH>>`` from user-visible output (like ``<<DONE>>``)."""
    kept: list[str] = []
    for line in text.split("\n"):
        if WATCH_TOKEN not in line:
            kept.append(line)
            continue
        remainder = line.replace(WATCH_TOKEN, "").strip()
        if remainder:
            kept.append(line.replace(WATCH_TOKEN, "").rstrip())
            continue
        while kept and kept[-1].strip() == "":
            kept.pop()
    while kept and kept[-1].strip() == "":
        kept.pop()
    return "\n".join(kept)


def strip_control_tokens(text: str) -> str:
    """Strip both control tokens from visible output."""
    return strip_watch(strip_done(text))


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


def tool_call_from_json(payload: str) -> Optional[ToolCall]:
    """Parse a ``{"name": ..., "arguments": ...}`` payload into a ToolCall."""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    name = str(data.get("name") or "")
    if not name:
        return None
    arguments = data.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {"value": arguments}
    # Tolerate a doubly-wrapped payload: {"name": .., "arguments": {"name": .., "arguments": {..}}}
    inner = arguments.get("arguments")
    if isinstance(inner, dict) and set(arguments).issubset({"name", "arguments"}):
        name = str(arguments.get("name") or name)
        arguments = inner
    return ToolCall(name=name, arguments=arguments,
                    raw=json.dumps(data, ensure_ascii=False))


def tool_call_block_spans(text: str) -> list[tuple[int, int, str]]:
    """``(start, stop, payload)`` for every complete <tool_call> block."""
    spans: list[tuple[int, int, str]] = []
    pos = 0
    while True:
        start = text.find(_OPEN_TAG, pos)
        if start == -1:
            return spans
        end = text.find(_CLOSE_TAG, start + len(_OPEN_TAG))
        if end == -1:
            return spans
        stop = end + len(_CLOSE_TAG)
        spans.append((start, stop, text[start + len(_OPEN_TAG):end].strip()))
        pos = stop


def freeform_tool_call(payload: str) -> ToolCall:
    """Wrap a freeform patch block as an apply_patch ToolCall."""
    arguments = {"patch": payload}
    return ToolCall(
        name=PATCH_TOOL_NAME,
        arguments=arguments,
        raw=json.dumps({"name": PATCH_TOOL_NAME, "arguments": arguments},
                       ensure_ascii=False),
    )


def dsml_tool_call_spans(text: str) -> list[tuple[int, int, str]]:
    """``(start, stop, payload)`` for DSML invoke blocks, via the CLI parser."""
    # Imported lazily: deepseek.client imports this module.
    from .client import _DSML_INVOKE_RE, _dsml_calls_to_json

    spans: list[tuple[int, int, str]] = []
    for match in _DSML_INVOKE_RE.finditer(text):
        for payload in _dsml_calls_to_json(match.group(0)):
            spans.append((match.start(), match.end(), payload))
    return spans


def extract_tool_calls(text: str) -> list[ToolCall]:
    """Every tool call in *text*, whatever its format, ordered by position.

    Accepts ``<tool_call>`` JSON blocks, freeform ``*** Begin Patch`` blocks
    and DSML ``invoke`` blocks, mixed freely in one response.
    """
    detected: list[tuple[int, ToolCall]] = []
    json_spans: list[tuple[int, int]] = []
    for start, stop, payload in tool_call_block_spans(text):
        json_spans.append((start, stop))
        call = tool_call_from_json(payload)
        if call is not None:
            detected.append((start, call))
    for start, stop, payload in find_freeform_patches(text):
        # A patch that lives inside a <tool_call> JSON string is part of that
        # call already; do not report it a second time as a freeform block.
        if any(span_start <= start < span_stop for span_start, span_stop in json_spans):
            continue
        detected.append((start, freeform_tool_call(payload)))
    for start, _, payload in dsml_tool_call_spans(text):
        if any(span_start <= start < span_stop for span_start, span_stop in json_spans):
            continue
        call = tool_call_from_json(payload)
        if call is not None:
            detected.append((start, call))
    detected.sort(key=lambda item: item[0])
    return [call for _, call in detected]


def strip_tool_calls(text: str) -> str:
    """Remove every recognised tool-call payload from *text*."""
    spans = tool_call_block_spans(text)
    out: list[str] = []
    cursor = 0
    for start, stop, _ in spans:
        out.append(text[cursor:start])
        cursor = stop
    out.append(text[cursor:])
    return strip_freeform_patches("".join(out))


def _has_forward_looking_tail(text: str) -> bool:
    """True when the answer trails off with an announced next step."""
    tail = text.rstrip().rstrip(".!,…").rstrip()
    if not tail:
        return False
    lowered = tail.lower()
    last_line = tail.splitlines()[-1].strip().lower()
    for phrase in FORWARD_LOOKING_PHRASES:
        if lowered.endswith(phrase) or last_line.startswith(phrase):
            return True
    return False


def is_complete_response(text: str, has_tool_calls: bool = False) -> bool:
    """Heuristic completion detection (Change 3)."""
    if has_tool_calls:
        return False
    if has_valid_done(text):
        return True
    stripped = strip_done(text)
    lowered = stripped.lower()
    if any(phrase in lowered for phrase in COMPLETION_PHRASES):
        return True
    body = stripped.strip()
    if (
        len(body) >= SUBSTANTIAL_ANSWER_CHARS
        and not body.endswith("?")
        and not _has_forward_looking_tail(body)
    ):
        return True
    return False


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
    """Accumulate one response's streamed parts and extract everything."""

    def __init__(self) -> None:
        self.thinking_parts: list[str] = []
        self.raw_answer_parts: list[str] = []
        self.tool_call_parts: list[str] = []
        self.answer_parts: list[str] = []
        self._ordered: list[tuple[str, str]] = []

    def feed(self, kind: str, text: str) -> None:
        if kind == "thinking":
            # THINK fragments are recorded verbatim and never searched.
            self.thinking_parts.append(text)
            return
        if kind == "tool_call":
            # The SSE parser only emits this kind from RESPONSE fragments.
            self.tool_call_parts.append(text)
            self._ordered.append(("tool_call", text))
            return
        if kind != "answer":
            return
        self.raw_answer_parts.append(text)
        self._ordered.append(("answer", text))
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
    def full_text(self) -> str:
        """The whole response with tool-call parts re-inlined, in stream order.

        The SSE parser lifts ``<tool_call>``/DSML blocks out of the answer
        stream, so they are re-wrapped here to recover the model's original
        ordering before the unified extractor runs.
        """
        pieces: list[str] = []
        for kind, text in self._ordered:
            if kind == "tool_call":
                pieces.append(f"{_OPEN_TAG}{text}{_CLOSE_TAG}")
            else:
                pieces.append(text)
        return "".join(pieces)

    @property
    def parsed_tool_calls(self) -> list[ToolCall]:
        """Every tool call in the response, any format, in order."""
        return extract_tool_calls(self.full_text)

    @property
    def tool_calls(self) -> list[str]:
        """Raw JSON payloads for every parsed call (ordered)."""
        return [call.raw for call in self.parsed_tool_calls]

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
    def watch(self) -> bool:
        """True when the response asks the harness to watch a running session."""
        return has_valid_watch(strip_tool_calls(self.full_text))

    @property
    def complete(self) -> bool:
        """Heuristic completion (marker, completion phrase, substantial answer)."""
        calls = self.parsed_tool_calls
        if self.watch:
            return False
        return is_complete_response(strip_tool_calls(self.full_text), bool(calls))

    @property
    def signal(self) -> Optional[str]:
        """``"tool_call"``, ``"done"``, or ``None`` for an incomplete turn."""
        if self.parsed_tool_calls:
            return "tool_call"
        if self.done:
            return "done"
        return None

    def visible_answer(self) -> str:
        """Answer text with tool calls and the completion marker stripped."""
        return strip_control_tokens(strip_tool_calls(self.full_text))


def execute_tool_calls(
    executor: SupportsExecute,
    calls: list,
    tools: list,
    decisions: Optional[list[bool]] = None,
) -> list[tuple[object, str]]:
    """Execute every call in order; a failing call does not stop the rest."""
    results: list[tuple[object, str]] = []
    for index, call in enumerate(calls):
        call_decisions = None
        if decisions is not None:
            call_decisions = [decisions[index]] if index < len(decisions) else [True]
        try:
            outcome = executor.execute([call], tools, decisions=call_decisions)
        except Exception as exc:  # pragma: no cover - defensive
            outcome = [(call, f"Error executing tool '{call.name}': "
                              f"{type(exc).__name__}: {exc}")]
        if outcome:
            results.extend(outcome)
        else:
            results.append((call, f"Error: tool '{call.name}' returned no result."))
    return results


class TurnNudger:
    """Track consecutive incomplete responses within one user turn.

    Each incomplete response earns one soft ``Continue.``; the counter resets
    after any successful tool call and the turn gives up after
    ``max_nudges`` consecutive continuations.
    """

    def __init__(self, max_nudges: int = MAX_NUDGES_PER_TURN) -> None:
        self.max_nudges = int(max_nudges)
        self.consecutive_failures = 0
        self.total_nudges = 0
        self.gave_up = False

    def reset(self) -> None:
        self.consecutive_failures = 0

    def note(self, signal: Optional[str]) -> str:
        """Record one response: ``complete``, ``tool_call``, ``continue`` or ``give_up``."""
        if signal == "tool_call":
            self.reset()
            return "tool_call"
        if signal == "done":
            self.reset()
            return "complete"
        self.consecutive_failures += 1
        self.total_nudges += 1
        if self.consecutive_failures > self.max_nudges:
            self.gave_up = True
            return "give_up"
        return "continue"

    def write_stop_file(self, text: str, directory: Path | str = "/tmp") -> Path:
        path = Path(directory) / f"incomplete_{int(time.time())}.txt"
        path.write_text(text, encoding="utf-8")
        return path
