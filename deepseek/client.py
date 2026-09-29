"""
Pure-HTTP DeepSeek chat client.

Speaks chat.deepseek.com's internal API directly using a captured signed-in
session (see `deepseek.auth`). For each message it:

    1. creates a chat session   (POST /api/v0/chat_session/create)
    2. fetches a PoW challenge   (POST /api/v0/chat/create_pow_challenge)
    3. solves it via the WASM    (deepseek.pow.DeepSeekPow)
    4. POSTs the completion       with the x-ds-pow-response header
    5. parses the SSE stream      into text + thinking parts

    from deepseek.auth import get_session
    from deepseek.client import DeepSeekClient

    client = DeepSeekClient(get_session())
    print(client.chat("Hello!"))                 # full reply (with .thinking)
    for chunk in client.stream("Tell a joke"):   # streamed (answer only)
        print(chunk, end="", flush=True)

    # Rich streaming with reasoning trace separated:
    for kind, text in client.stream("...").iter_parts():
        if kind == "thinking": ...
        else: ...

Debug: set DEEPSEEK_SSE_DEBUG=/path/to/sse.log to dump every raw SSE payload.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from typing import Iterator, Literal, Optional

import httpx

from .auth import Session, get_session
from .pow import DeepSeekPow
from .tools import Tool, ToolCall, execute_tool

BASE = "https://chat.deepseek.com"
COMPLETION_PATH = "/api/v0/chat/completion"
DEFAULT_MODEL_TYPE = "default"
_CID_SEP = ":"

PartKind = Literal["thinking", "answer", "tool_call", "tool_result"]

TOOL_SYSTEM_PREAMBLE = """You have access to the following tools. When you want to call a tool, respond with ONLY a single JSON object wrapped in <tool_call></tool_call> tags, and nothing else before or after it. The JSON must have keys "name" (string) and "arguments" (object). Wait for the tool result before continuing. If no tool is needed, respond normally without any tool_call tags.

Available tools:
{tools_schema}

When you receive a tool result, it will appear as a user message prefixed with "TOOL RESULT for <tool_name>:". Use it to continue."""

# ----- Optional debug logging ------------------------------------------------

_DEBUG_LOG_PATH = os.getenv("DEEPSEEK_SSE_DEBUG")
_log_handle = None


def _dlog(msg: str) -> None:
    """Append one line to the debug log if DEEPSEEK_SSE_DEBUG is set."""
    global _log_handle
    if not _DEBUG_LOG_PATH:
        return
    if _log_handle is None:
        try:
            _log_handle = open(_DEBUG_LOG_PATH, "a", buffering=1)
        except Exception:
            _log_handle = False
    if _log_handle:
        try:
            _log_handle.write(msg + "\n")
        except Exception:
            pass


# ----- conversation-id helpers ----------------------------------------------

def _encode_cid(session_id: str, message_id: Optional[int]) -> str:
    if message_id is None:
        return session_id
    return f"{session_id}{_CID_SEP}{message_id}"


def _decode_cid(conversation_id: Optional[str]) -> tuple[Optional[str], Optional[int]]:
    if not conversation_id:
        return None, None
    session_id, _, msg = conversation_id.partition(_CID_SEP)
    parent = int(msg) if msg.isdigit() else None
    return (session_id or None), parent


@dataclass
class Reply:
    """A completed chat reply plus the id to resume the conversation.

    `thinking` holds the DeepThink reasoning trace (empty when thinking is off).
    `tool_call` holds any requested ToolCall (or None if no tool requested).
    `tool_calls_made` records all ToolCalls attempted during chat_with_tools.
    """
    text: str
    conversation_id: str
    thinking: str = ""
    tool_call: Optional[ToolCall] = None
    tool_calls_made: list[ToolCall] = None

    def __post_init__(self):
        if self.tool_calls_made is None:
            self.tool_calls_made = []

    def __str__(self) -> str:
        return self.text


def _biz(data: dict) -> dict:
    if data.get("code") != 0:
        raise RuntimeError(f"DeepSeek API error: {data.get('msg') or data}")
    biz = data.get("data", {}).get("biz_data")
    if biz is None:
        raise RuntimeError(f"Unexpected response shape: {data}")
    return biz


class DeepSeekClient:
    def __init__(self, session: Optional[Session] = None,
                 allow_interactive: bool = True):
        self.session = session or get_session(allow_interactive=allow_interactive)
        self._pow = DeepSeekPow()
        self._pow_lock = threading.Lock()
        self._http = httpx.Client(
            base_url=BASE,
            headers=self._base_headers(),
            cookies=self.session.cookies,
            timeout=httpx.Timeout(120.0, read=300.0),
        )

    def _base_headers(self) -> dict:
        return {
            "authorization": f"Bearer {self.session.token}",
            "accept": "*/*",
            "content-type": "application/json",
            "user-agent": self.session.user_agent,
            "origin": BASE,
            "referer": f"{BASE}/",
            "x-app-version": "2.0.0",
            "x-client-version": "2.0.0",
            "x-client-platform": "web",
            "x-client-locale": "en_US",
            "x-client-bundle-id": "com.deepseek.chat",
            "x-client-timezone-offset": "19800",
        }

    def create_chat_session(self) -> str:
        r = self._http.post("/api/v0/chat_session/create", json={})
        r.raise_for_status()
        return _biz(r.json())["chat_session"]["id"]

    def _pow_header(self, target_path: str = COMPLETION_PATH) -> str:
        r = self._http.post(
            "/api/v0/chat/create_pow_challenge", json={"target_path": target_path}
        )
        r.raise_for_status()
        challenge = _biz(r.json())["challenge"]
        with self._pow_lock:
            return self._pow.make_header(challenge)

    def stream(self, prompt: str, conversation_id: Optional[str] = None,
               model: Optional[str] = None, thinking: bool = False,
               search: bool = False) -> "_Stream":
        if conversation_id and model is not None:
            raise ValueError(
                "`model` cannot be set together with `conversation_id`; a thread's "
                "model is fixed when it is created. Pass `model` only on the first turn."
            )
        session_id, parent_id = _decode_cid(conversation_id)
        if session_id is None:
            session_id = self.create_chat_session()
            model_type: Optional[str] = model or DEFAULT_MODEL_TYPE
        else:
            model_type = None
        return _Stream(self, prompt, session_id, parent_id, model_type, thinking, search)

    def chat(self, prompt: str, conversation_id: Optional[str] = None,
             model: Optional[str] = None, thinking: bool = False,
             search: bool = False) -> Reply:
        s = self.stream(prompt, conversation_id=conversation_id,
                        model=model, thinking=thinking, search=search)
        answer: list[str] = []
        reasoning: list[str] = []
        for kind, text in s.iter_parts():
            if kind == "thinking":
                reasoning.append(text)
            elif kind == "answer":
                answer.append(text)
        return Reply(text="".join(answer),
                     conversation_id=s.conversation_id,
                     thinking="".join(reasoning),
                     tool_call=s.tool_call)

    def _default_manual_approval(self, call: ToolCall) -> tuple[bool, bool]:
        """Prompt user on stdin for approval. Returns (approved, remember)."""
        print(f"\nTool call requested: {call.name}({call.arguments})")
        while True:
            resp = input("Approve tool call? [y]es / [n]o / [a]lways: ").strip().lower()
            if resp in ("y", "yes"):
                return True, False
            if resp in ("n", "no"):
                return False, False
            if resp in ("a", "always"):
                return True, True

    def chat_with_tools(
        self,
        prompt: str,
        tools: list[Tool],
        approval: str | Callable[[ToolCall], bool] = "manual",
        conversation_id: Optional[str] = None,
        model: Optional[str] = None,
        thinking: bool = False,
        search: bool = False,
        max_iterations: int = 8,
    ) -> Reply:
        tools_schema_str = json.dumps([t.schema() for t in tools], indent=2)
        current_prompt = (
            TOOL_SYSTEM_PREAMBLE.format(tools_schema=tools_schema_str)
            + "\n\nUser: "
            + prompt
        )

        current_cid = conversation_id
        current_model = model
        tool_calls_made: list[ToolCall] = []
        always_approved_tools: set[str] = set()

        for iteration in range(max_iterations):
            reply = self.chat(
                current_prompt,
                conversation_id=current_cid,
                model=current_model if iteration == 0 else None,
                thinking=thinking,
                search=search,
            )
            current_cid = reply.conversation_id

            if reply.tool_call is None:
                reply.tool_calls_made = tool_calls_made
                return reply

            call = reply.tool_call
            tool_calls_made.append(call)

            # Check approval
            approved = False
            if call.name in always_approved_tools:
                approved = True
            elif approval == "auto":
                approved = True
            elif approval == "manual":
                appr, remember = self._default_manual_approval(call)
                approved = appr
                if remember and approved:
                    always_approved_tools.add(call.name)
            elif callable(approval):
                approved = bool(approval(call))

            if approved:
                result = execute_tool(call, tools)
            else:
                result = "User rejected this tool call."

            # Build next prompt turn
            call_raw = call.raw if call.raw else json.dumps({"name": call.name, "arguments": call.arguments})
            current_prompt = (
                f"<tool_call>{call_raw}</tool_call>\n\n"
                f"TOOL RESULT for {call.name}:\n{result}"
            )

        reply.tool_calls_made = tool_calls_made
        return reply

    def stream_with_tools(
        self,
        prompt: str,
        tools: list[Tool],
        approval: str | Callable[[ToolCall], bool | tuple[bool, bool]] = "manual",
        conversation_id: Optional[str] = None,
        model: Optional[str] = None,
        thinking: bool = False,
        search: bool = False,
        max_iterations: int = 8,
    ) -> Iterator[tuple[PartKind, str]]:
        """Streaming variant of chat_with_tools yielding (kind, text) events."""
        tools_schema_str = json.dumps([t.schema() for t in tools], indent=2)
        current_prompt = (
            TOOL_SYSTEM_PREAMBLE.format(tools_schema=tools_schema_str)
            + "\n\nUser: "
            + prompt
        )

        current_cid = conversation_id
        current_model = model
        always_approved_tools: set[str] = set()
        self._last_stream_cid: Optional[str] = current_cid

        for iteration in range(max_iterations):
            s = self.stream(
                current_prompt,
                conversation_id=current_cid,
                model=current_model if iteration == 0 else None,
                thinking=thinking,
                search=search,
            )
            for kind, text in s.iter_parts():
                yield (kind, text)

            current_cid = s.conversation_id
            self._last_stream_cid = current_cid

            if s.tool_call is None:
                return

            call = s.tool_call
            approved = False

            if call.name in always_approved_tools:
                approved = True
            elif approval == "auto":
                approved = True
            elif approval == "manual":
                appr, remember = self._default_manual_approval(call)
                approved = appr
                if remember and approved:
                    always_approved_tools.add(call.name)
            elif callable(approval):
                res = approval(call)
                if isinstance(res, tuple):
                    approved, remember = res
                    if remember and approved:
                        always_approved_tools.add(call.name)
                else:
                    approved = bool(res)

            if approved:
                result = execute_tool(call, tools)
            else:
                result = "User rejected this tool call."

            yield ("tool_result", result)

            call_raw = call.raw if call.raw else json.dumps({"name": call.name, "arguments": call.arguments})
            current_prompt = (
                f"<tool_call>{call_raw}</tool_call>\n\n"
                f"TOOL RESULT for {call.name}:\n{result}"
            )

    def close(self) -> None:
        self._http.close()


class _Stream:
    """Streamed reply. Iterating yields answer text; `.iter_parts()` yields
    (kind, text) tuples with thinking, answer, and tool_call. After consumption,
    `.conversation_id` holds the resume token and `.tool_call` holds any tool call."""

    def __init__(self, client: "DeepSeekClient", prompt: str, session_id: str,
                 parent_id: Optional[int], model: Optional[str],
                 thinking: bool, search: bool):
        self._client = client
        self._prompt = prompt
        self._session_id = session_id
        self._parent_id = parent_id
        self._model = model
        self._thinking = thinking
        self._search = search
        self._message_id: Optional[int] = None
        self.tool_call: Optional[ToolCall] = None

    def iter_parts(self) -> Iterator[tuple[PartKind, str]]:
        body = {
            "chat_session_id": self._session_id,
            "parent_message_id": self._parent_id,
            "prompt": self._prompt,
            "ref_file_ids": [],
            "thinking_enabled": self._thinking,
            "search_enabled": self._search,
            "action": None,
            "preempt": False,
        }
        if self._model is not None:
            body["model_type"] = self._model
        headers = {"x-ds-pow-response": self._client._pow_header()}
        meta: dict = {}
        with self._client._http.stream(
            "POST", COMPLETION_PATH, json=body, headers=headers
        ) as resp:
            resp.raise_for_status()
            for kind, text in _parse_sse(resp.iter_lines(), meta):
                if kind == "tool_call":
                    try:
                        data = json.loads(text)
                        self.tool_call = ToolCall(
                            name=data.get("name", ""),
                            arguments=data.get("arguments", {}),
                            raw=text,
                        )
                    except Exception as e:
                        _dlog(f"Failed to parse tool_call JSON: {e}")
                yield (kind, text)

        if meta.get("message_id") is not None:
            self._message_id = meta["message_id"]

    def __iter__(self) -> Iterator[str]:
        """Yield ONLY answer text (backwards compatible with plain iteration)."""
        for kind, text in self.iter_parts():
            if kind == "answer":
                yield text

    @property
    def conversation_id(self) -> str:
        return _encode_cid(self._session_id, self._message_id)


# ----- SSE parsing -----------------------------------------------------------

# Recognised fragment type names. Unknown non-empty types default to "answer".
_THINKING_TYPES = {"THINK", "REASONING", "THINKING", "COT", "CHAIN_OF_THOUGHT"}
_RESPONSE_TYPES = {"RESPONSE", "ANSWER", "TEXT", "CONTENT", "FINAL", "OUTPUT"}
# Matches ".../fragments/<N>/..." or ".../fragments/<N>" at end; N may be -1.
_FRAG_INDEX_RE = re.compile(r"fragments/(-?\d+)(?:/|$)")

_OPEN_TAG = "<tool_call>"
_CLOSE_TAG = "</tool_call>"


def _fragment_kind(frag: dict) -> Optional[PartKind]:
    t = (frag.get("type") or "").upper()
    if t in _THINKING_TYPES:
        return "thinking"
    if t in _RESPONSE_TYPES:
        return "answer"
    return None


def _parse_sse(lines, meta: Optional[dict] = None) -> Iterator[tuple[PartKind, str]]:
    """Turn DeepSeek's SSE completion stream into (kind, text) tuples.

    Recognizes thinking, answer, and tool_call blocks (<tool_call>...</tool_call>).
    """
    fragment_kinds: dict[int, PartKind] = {}
    last_seen: dict[int, str] = {}     # cumulative content per fragment index

    def _highest() -> int:
        return max(fragment_kinds) if fragment_kinds else 0

    def _register(idx: int, kind: Optional[PartKind]) -> None:
        if kind is not None:
            fragment_kinds[idx] = kind
        elif idx not in fragment_kinds:
            if idx == 0:
                fragment_kinds[idx] = "thinking"
            elif (idx - 1) in fragment_kinds:
                prev = fragment_kinds[idx - 1]
                fragment_kinds[idx] = "answer" if prev == "thinking" else "thinking"
            else:
                fragment_kinds[idx] = "answer"

    def _absorb_full(idx: int, full: str) -> Optional[str]:
        prev = last_seen.get(idx, "")
        if not full or full == prev:
            return None
        if prev and full.startswith(prev):
            delta = full[len(prev):]
        else:
            delta = full
        last_seen[idx] = full
        return delta

    # Tool call parsing state machine
    # We buffer answer chunks to detect <tool_call> ... </tool_call>
    answer_buffer = ""
    in_tool_call = False
    tool_call_buffer = ""

    def process_answer_chunk(text: str) -> Iterator[tuple[PartKind, str]]:
        nonlocal answer_buffer, in_tool_call, tool_call_buffer
        combined = answer_buffer + text
        answer_buffer = ""

        pos = 0
        while pos < len(combined):
            if not in_tool_call:
                tag_idx = combined.find(_OPEN_TAG, pos)
                if tag_idx != -1:
                    # Emit everything before <tool_call> as answer
                    if tag_idx > pos:
                        yield ("answer", combined[pos:tag_idx])
                    in_tool_call = True
                    pos = tag_idx + len(_OPEN_TAG)
                else:
                    # No <tool_call> tag found. Keep up to 32 chars in answer_buffer
                    # to handle <tool_call> tag split across chunks.
                    safe_len = len(combined) - pos
                    if safe_len > 32:
                        emit_len = safe_len - 32
                        yield ("answer", combined[pos:pos + emit_len])
                        pos += emit_len
                    answer_buffer = combined[pos:]
                    break
            else:
                close_idx = combined.find(_CLOSE_TAG, pos)
                if close_idx != -1:
                    tool_call_buffer += combined[pos:close_idx]
                    tc_json = tool_call_buffer.strip()
                    tool_call_buffer = ""
                    in_tool_call = False
                    pos = close_idx + len(_CLOSE_TAG)
                    yield ("tool_call", tc_json)
                else:
                    tool_call_buffer += combined[pos:]
                    break

    for line in lines:
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        _dlog(payload)
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue

        v = obj.get("v")
        p = obj.get("p") or ""
        o = obj.get("o") or ""

        # --- 1. Snapshot with a full response object ---
        if isinstance(v, dict) and "response" in v:
            if meta is not None:
                _capture_message_id(meta, v)
            frags = v["response"].get("fragments") or []
            for i, frag in enumerate(frags):
                _register(i, _fragment_kind(frag))
                delta = _absorb_full(i, frag.get("content") or "")
                if delta:
                    kind = fragment_kinds.get(i, "answer")
                    if kind == "answer":
                        yield from process_answer_chunk(delta)
                    else:
                        yield (kind, delta)
            continue

        # --- 2. Fragment registration / update ---
        frags_to_register = []
        if isinstance(v, list) and p.endswith("fragments"):
            frags_to_register = [item for item in v if isinstance(item, dict)]
        elif isinstance(v, dict) and ("type" in v or "content" in v):
            frags_to_register = [v]

        if frags_to_register:
            m = _FRAG_INDEX_RE.search(p)
            for item in frags_to_register:
                if m and not (isinstance(v, list) and p.endswith("fragments")):
                    raw = int(m.group(1))
                    idx = (_highest() + 1) if raw < 0 else raw
                else:
                    idx = (_highest() + 1) if (fragment_kinds and 0 in fragment_kinds) else 0
                _register(idx, _fragment_kind(item))
                content = item.get("content") or ""
                if content:
                    last_seen[idx] = content
                    kind = fragment_kinds[idx]
                    if kind == "answer":
                        yield from process_answer_chunk(content)
                    else:
                        yield (kind, content)
            continue

        # --- 3. message_id capture ---
        if p.endswith("message_id") and isinstance(v, int):
            if meta is not None:
                meta["message_id"] = v
            continue

        # --- 4. Content delta ---
        if isinstance(v, str):
            if p:
                if not p.endswith("content"):
                    continue
                m = _FRAG_INDEX_RE.search(p)
                if m:
                    raw = int(m.group(1))
                    idx = _highest() if raw < 0 else raw
                else:
                    idx = _highest()
            else:
                idx = _highest()
            if idx not in fragment_kinds:
                _register(idx, None)
            last_seen[idx] = last_seen.get(idx, "") + v
            kind = fragment_kinds[idx]
            if kind == "answer":
                yield from process_answer_chunk(v)
            else:
                yield (kind, v)
            continue

    # Flush remaining answer buffer & tool call buffer
    if in_tool_call and tool_call_buffer:
        close_idx = tool_call_buffer.find(_CLOSE_TAG)
        if close_idx != -1:
            tc_json = tool_call_buffer[:close_idx].strip()
            yield ("tool_call", tc_json)
            after = tool_call_buffer[close_idx + len(_CLOSE_TAG):]
            if after:
                yield ("answer", after)
        else:
            _dlog(f"Warning: unclosed <tool_call> block discarded: {tool_call_buffer}")
    if answer_buffer:
        yield ("answer", answer_buffer)


def _capture_message_id(meta: dict, snapshot: dict) -> None:
    """Best-effort: pull the assistant message_id out of a snapshot frame."""
    for container in (snapshot.get("response"), snapshot):
        if isinstance(container, dict):
            mid = container.get("message_id", container.get("id"))
            if isinstance(mid, int):
                meta["message_id"] = mid
                return
