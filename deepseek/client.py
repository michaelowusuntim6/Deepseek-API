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

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from typing import Callable, Iterator, Literal, Optional

import httpx

from . import auth
from .auth import LoginRequired, Session, get_session
from .pow import DeepSeekPow
from .tools import Tool, ToolCall, execute_tool

BASE = "https://chat.deepseek.com"
COMPLETION_PATH = "/api/v0/chat/completion"
DEFAULT_MODEL_TYPE = "default"
_CID_SEP = ":"

PartKind = Literal["thinking", "answer", "tool_call", "tool_result"]

TOOL_SYSTEM_PREAMBLE = """You are a coding agent running in the DeepSeek CLI, a terminal-based coding assistant.

Personality: Concise, direct, friendly. Keep the user informed without unnecessary detail.

Before tool calls, send a brief 1-2 sentence preamble (8-12 words) explaining what you are about to do.

You have access to the following tools. When you want to call one or more tools, respond with ONLY <tool_call></tool_call> blocks and nothing else before or after them. Each block must contain one JSON object with keys "name" (string) and "arguments" (object). Wait for the tool results before continuing. If no tool is needed, respond normally without any tool_call tags.

Call tools with exactly this format and nothing else:

    <tool_call>{{"name": "tool_name", "arguments": {{...}}}}</tool_call>

Do not wrap the call in DSML, XML, <|tool_calls|>, or any other markup. Do not include prose before or after the tool call block.

Available tools:
{tools_schema}

## Model capabilities vs. agent tools
DeepSeek web search is enabled by default. It is a model-side capability: the server decides per prompt whether to search. To disable it, run /search or pass --no-search at startup. It does not appear as a tool. If you need to fetch a URL or search the web as a tool, use exec_command with curl, wget, or git as appropriate.

When a task needs a capability that is not in the tool list, before saying the capability is unavailable:
1. Call search_tools once with a clear query.
2. If that returns nothing useful, try exec_command with a shell command that achieves the goal. Examples:
   - clone a repo: git clone <url> <dest>
   - fetch a page: curl -sL <url>
   - search the web: curl -sL "https://html.duckduckgo.com/html/?q=<query>"
   - download a file: wget <url>
3. Only say the capability is unavailable after both steps fail.

Some tools are not listed. Use search_tools to find tools by capability. If the user asks for a capability that is not in the list above, call search_tools before saying the capability is unavailable. Matched tools are available for one turn only, so call search_tools again if you need them later.

Example: if the user asks for weather and no weather tool is listed above, your next response must be only:
<tool_call>{{"name":"search_tools","arguments":{{"query":"weather"}}}}</tool_call>
If the user asks to display or render an image, first call search_tools with query "image".
If the user asks you to remember, recall, forget, or search memory, first call search_tools with query "memory".

Use update_plan to keep an up-to-date, step-by-step plan. Provide a short list of 1-sentence steps (no more than 5-7 words each) with a status for each step (pending, in_progress, or completed). There should always be exactly one in_progress step until everything is done.

Shell guidelines: Prefer rg over grep. Read files in chunks of <=250 lines. Output is truncated at 10KB or 256 lines.

Use apply_patch to edit files. NEVER try applypatch or apply-patch. Use:
*** Begin Patch
*** Update File: path/to/file
@@ context
-old
+new
*** End Patch

To add a file, use:
*** Begin Patch
*** Add File: /tmp/example.txt
+hello world
*** End Patch

Editing discipline: Fix the root cause, not surface symptoms. Keep changes minimal. Do not fix unrelated bugs. Do not add comments unless requested. Do not commit unless requested.

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
    `tool_calls` holds every requested ToolCall (empty if no tool was requested).
    `tool_call` is retained as a backwards-compatible alias for the first call.
    `tool_calls_made` records all ToolCalls attempted during chat_with_tools.
    """
    text: str
    conversation_id: str
    thinking: str = ""
    tool_call: Optional[ToolCall] = None
    tool_calls: list[ToolCall] = None
    tool_calls_made: list[ToolCall] = None

    def __post_init__(self):
        if self.tool_calls is None:
            self.tool_calls = []
        if self.tool_call is not None and not self.tool_calls:
            self.tool_calls = [self.tool_call]
        if self.tool_calls and self.tool_call is None:
            self.tool_call = self.tool_calls[0]
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
    def __init__(
        self,
        session: Optional[Session] = None,
        allow_interactive: bool = True,
        session_max_age: Optional[float] = None,
        on_session_refresh: Optional[Callable[[str], None]] = None,
    ):
        self._allow_interactive = allow_interactive
        self._session_max_age = session_max_age
        self._on_session_refresh = on_session_refresh
        self._session_lock = threading.Lock()
        self.session = session or get_session(allow_interactive=allow_interactive)
        self._pow = DeepSeekPow()
        self._pow_lock = threading.Lock()
        self._tool_preamble_fingerprints: dict[str, str] = {}
        self._http = httpx.Client(
            base_url=BASE,
            headers=self._base_headers(),
            cookies=self.session.cookies,
            timeout=httpx.Timeout(120.0, read=300.0),
        )

    def _max_session_age(self) -> float:
        return (
            auth.SESSION_MAX_AGE
            if self._session_max_age is None
            else float(self._session_max_age)
        )

    def _replace_session(self, fresh: Session) -> None:
        old_http = self._http
        self.session = fresh
        self._http = httpx.Client(
            base_url=BASE,
            headers=self._base_headers(),
            cookies=self.session.cookies,
            timeout=httpx.Timeout(120.0, read=300.0),
        )
        try:
            old_http.close()
        except Exception:
            pass

    def _ensure_session_fresh(self) -> bool:
        """Refresh an aging session before a request; return True if refreshed."""
        max_age = self._max_session_age()
        if max_age < 0 or self.session.age < max_age:
            return False

        with self._session_lock:
            if self.session.age < max_age:
                return False
            try:
                fresh = get_session(
                    max_age=max_age,
                    allow_interactive=self._allow_interactive,
                )
            except LoginRequired:
                raise
            except Exception as exc:
                raise RuntimeError(
                    f"DeepSeek session refresh failed: {type(exc).__name__}: {exc}"
                ) from exc

            if fresh is None:
                raise RuntimeError(
                    "DeepSeek session refresh failed: no session was returned."
                )

            changed = (
                fresh.token != self.session.token
                or fresh.cookies != self.session.cookies
                or fresh.user_agent != self.session.user_agent
            )
            if changed:
                self._replace_session(fresh)
            else:
                self.session = fresh

        if self._on_session_refresh is not None:
            try:
                self._on_session_refresh("DeepSeek session refreshed.")
            except Exception:
                pass
        return True

    def _tool_prompt(self, prompt: str, tools_schema_str: str,
                     conversation_id: Optional[str],
                     extra_sections: Optional[list[str]] = None) -> str:
        """Prefix the tool preamble only when it is not already in this thread."""
        fingerprint = hashlib.sha256(tools_schema_str.encode("utf-8")).hexdigest()
        session_id, _ = _decode_cid(conversation_id)
        if session_id and self._tool_preamble_fingerprints.get(session_id) == fingerprint:
            return prompt
        preamble = TOOL_SYSTEM_PREAMBLE.format(tools_schema=tools_schema_str)
        sections = [section for section in (extra_sections or []) if section.strip()]
        if sections:
            preamble += "\n\n" + "\n\n".join(sections)
        return preamble + "\n\nUser: " + prompt

    def _remember_tool_prompt(self, tools_schema_str: str,
                              conversation_id: Optional[str]) -> None:
        session_id, _ = _decode_cid(conversation_id)
        if session_id:
            fingerprint = hashlib.sha256(tools_schema_str.encode("utf-8")).hexdigest()
            self._tool_preamble_fingerprints[session_id] = fingerprint

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
        self._ensure_session_fresh()
        r = self._http.post("/api/v0/chat_session/create", json={})
        r.raise_for_status()
        return _biz(r.json())["chat_session"]["id"]

    def _pow_header(self, target_path: str = COMPLETION_PATH) -> str:
        self._ensure_session_fresh()
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
                     tool_calls=s.tool_calls)

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

    def _approval_decision(
        self,
        call: ToolCall,
        approval: str | Callable[[ToolCall], bool | tuple[bool, bool]],
        always_approved_tools: set[str],
    ) -> bool:
        if call.name in always_approved_tools:
            return True
        if approval == "auto":
            return True
        if approval == "manual":
            approved, remember = self._default_manual_approval(call)
            if remember and approved:
                always_approved_tools.add(call.name)
            return approved
        if callable(approval):
            res = approval(call)
            if isinstance(res, tuple):
                approved, remember = res
                if remember and approved:
                    always_approved_tools.add(call.name)
                return bool(approved)
            return bool(res)
        raise ValueError(f"Unknown approval mode: {approval!r}")

    @staticmethod
    def _tool_result_prompt(pairs: list[tuple[ToolCall, str]]) -> str:
        call_blocks = []
        result_blocks = []
        for call, result in pairs:
            call_raw = call.raw if call.raw else json.dumps(
                {"name": call.name, "arguments": call.arguments}
            )
            call_blocks.append(f"<tool_call>{call_raw}</tool_call>")
            result_blocks.append(f"TOOL RESULT for {call.name}:\n{result}")
        return "\n\n".join(call_blocks + result_blocks)

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
        current_prompt = self._tool_prompt(prompt, tools_schema_str, conversation_id)

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
            self._remember_tool_prompt(tools_schema_str, current_cid)

            if not reply.tool_calls:
                reply.tool_calls_made = tool_calls_made
                return reply

            pairs: list[tuple[ToolCall, str]] = []
            for call in reply.tool_calls:
                tool_calls_made.append(call)
                approved = self._approval_decision(call, approval, always_approved_tools)
                result = (
                    execute_tool(call, tools)
                    if approved
                    else "User rejected this tool call."
                )
                pairs.append((call, result))

            current_prompt = self._tool_result_prompt(pairs)

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
        current_prompt = self._tool_prompt(prompt, tools_schema_str, conversation_id)

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
            self._remember_tool_prompt(tools_schema_str, current_cid)

            if not s.tool_calls:
                return

            pairs: list[tuple[ToolCall, str]] = []
            for call in s.tool_calls:
                approved = self._approval_decision(call, approval, always_approved_tools)
                result = (
                    execute_tool(call, tools)
                    if approved
                    else "User rejected this tool call."
                )
                pairs.append((call, result))
                yield ("tool_result", result)

            current_prompt = self._tool_result_prompt(pairs)

    def close(self) -> None:
        self._http.close()


class _Stream:
    """Streamed reply. Iterating yields answer text; `.iter_parts()` yields
    (kind, text) tuples with thinking, answer, and tool_call. After consumption,
    `.conversation_id` holds the resume token, `.tool_calls` holds every tool call,
    and `.tool_call` aliases the first call for backwards compatibility."""

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
        self.tool_calls: list[ToolCall] = []

    def iter_parts(self) -> Iterator[tuple[PartKind, str]]:
        self._client._ensure_session_fresh()
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
        if _DEBUG_LOG_PATH:
            _dlog(
                "[request-prompt] "
                + json.dumps({"chat_session_id": self._session_id, "prompt": self._prompt},
                             ensure_ascii=False)
            )
        meta: dict = {}
        with self._client._http.stream(
            "POST", COMPLETION_PATH, json=body, headers=headers
        ) as resp:
            resp.raise_for_status()
            for kind, text in _parse_sse(resp.iter_lines(), meta):
                if kind == "tool_call":
                    try:
                        data = _extract_tool_call_json(text)
                        name = data.get("name", "")
                        arguments = data.get("arguments", {})
                        if isinstance(arguments, str):
                            try:
                                arguments = json.loads(arguments)
                            except json.JSONDecodeError:
                                pass
                        if (
                            isinstance(arguments, dict)
                            and isinstance(arguments.get("arguments"), dict)
                            and set(arguments).issubset({"name", "arguments"})
                        ):
                            name = arguments.get("name", name) or name
                            arguments = arguments["arguments"]
                        self.tool_calls.append(
                            ToolCall(
                                name=name,
                                arguments=arguments,
                                raw=json.dumps(data, ensure_ascii=False),
                            )
                        )
                        text = json.dumps(data, ensure_ascii=False)
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

    @property
    def tool_call(self) -> Optional[ToolCall]:
        """Backwards-compatible alias for the first requested tool call."""
        return self.tool_calls[0] if self.tool_calls else None


# ----- SSE parsing -----------------------------------------------------------

# Recognised fragment type names. Unknown non-empty types default to "answer".
_THINKING_TYPES = {"THINK", "REASONING", "THINKING", "COT", "CHAIN_OF_THOUGHT"}
_RESPONSE_TYPES = {"RESPONSE", "ANSWER", "TEXT", "CONTENT", "FINAL", "OUTPUT"}
# Matches ".../fragments/<N>/..." or ".../fragments/<N>" at end; N may be -1.
_FRAG_INDEX_RE = re.compile(r"fragments/(-?\d+)(?:/|$)")

_OPEN_TAG = "<tool_call>"
_CLOSE_TAG = "</tool_call>"
_DSML_BAR = "\uff5c\uff5c"
_DSML_CALLS_OPEN = f"<{_DSML_BAR}DSML{_DSML_BAR} calls>"
_DSML_CALLS_CLOSE = f"</{_DSML_BAR}DSML{_DSML_BAR} calls>"
_DSML_INVOKE_RE = re.compile(
    rf'<{_DSML_BAR}DSML{_DSML_BAR} invoke\s+name="([^"]+)">(.*?)'
    rf'</{_DSML_BAR}DSML{_DSML_BAR} invoke>',
    re.DOTALL,
)
_DSML_PARAM_RE = re.compile(
    rf'<{_DSML_BAR}DSML{_DSML_BAR} parameter\s+name="([^"]+)"'
    rf'(?:\s+string="(true|false)")?>(.*?)'
    rf'</{_DSML_BAR}DSML{_DSML_BAR} parameter>',
    re.DOTALL,
)


def _dsml_calls_to_json(text: str) -> Iterator[str]:
    """Translate DeepSeek DSML invoke blocks into <tool_call> JSON strings."""
    for match in _DSML_INVOKE_RE.finditer(text):
        name = match.group(1)
        body = match.group(2)
        arguments: dict = {}
        for param in _DSML_PARAM_RE.finditer(body):
            param_name = param.group(1)
            string_attr = param.group(2)
            raw_value = (param.group(3) or "").strip()
            if string_attr == "true":
                value = raw_value
            else:
                try:
                    value = json.loads(raw_value)
                except json.JSONDecodeError:
                    value = raw_value
            arguments[param_name] = value
        yield json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False)


def _extract_tool_call_json(text: str) -> Optional[dict]:
    """Extract the first complete JSON object from a possibly DSML-suffixed string."""
    start = text.find("{")
    if start < 0:
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "name" not in obj:
        return None
    if "arguments" not in obj:
        obj["arguments"] = {}
    if isinstance(obj["arguments"], str):
        try:
            obj["arguments"] = json.loads(obj["arguments"])
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj["arguments"], dict) else None


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

    # Tool call parsing state machine. It recognizes the project's
    # <tool_call> JSON wrapper and DeepSeek's native DSML invoke form.
    answer_buffer = ""
    in_tool_call = False
    tool_call_buffer = ""
    in_dsml = False
    dsml_buffer = ""
    strip_finished = False

    def process_answer_chunk(text: str) -> Iterator[tuple[PartKind, str]]:
        nonlocal answer_buffer, in_tool_call, tool_call_buffer
        nonlocal in_dsml, dsml_buffer, strip_finished
        combined = answer_buffer + text
        answer_buffer = ""

        pos = 0
        while pos < len(combined):
            if strip_finished:
                probe = combined[pos:].lstrip()
                if probe.startswith("FINISHED"):
                    pos = len(combined) - len(probe) + len("FINISHED")
                    strip_finished = False
                    continue

            if in_tool_call:
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
                continue

            if in_dsml:
                close_idx = combined.find(_DSML_CALLS_CLOSE, pos)
                if close_idx != -1:
                    dsml_buffer += combined[pos:close_idx]
                    for tc_json in _dsml_calls_to_json(dsml_buffer):
                        yield ("tool_call", tc_json)
                    dsml_buffer = ""
                    in_dsml = False
                    strip_finished = True
                    pos = close_idx + len(_DSML_CALLS_CLOSE)
                else:
                    dsml_buffer += combined[pos:]
                    break
                continue

            tag_idx = combined.find(_OPEN_TAG, pos)
            dsml_idx = combined.find(_DSML_CALLS_OPEN, pos)
            if tag_idx == -1 and dsml_idx == -1:
                # Keep enough tail bytes to recognize either wrapper when it is
                # split across streaming chunks.
                keep = max(len(_OPEN_TAG), len(_DSML_CALLS_OPEN), 32)
                safe_len = len(combined) - pos
                if safe_len > keep:
                    emit_len = safe_len - keep
                    yield ("answer", combined[pos:pos + emit_len])
                    pos += emit_len
                answer_buffer = combined[pos:]
                break

            if tag_idx != -1 and (dsml_idx == -1 or tag_idx < dsml_idx):
                if tag_idx > pos:
                    yield ("answer", combined[pos:tag_idx])
                in_tool_call = True
                pos = tag_idx + len(_OPEN_TAG)
                continue

            if dsml_idx > pos:
                yield ("answer", combined[pos:dsml_idx])
            in_dsml = True
            pos = dsml_idx + len(_DSML_CALLS_OPEN)

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

        # --- 0. BATCH envelopes ---
        # DeepSeek wraps several protocol operations in one SSE frame. Search
        # responses in particular register the RESPONSE fragment inside a
        # "response" BATCH; ignoring that leaves the answer attached to the
        # SEARCH fragment and hidden as "thinking".
        if (
            o == "BATCH"
            and isinstance(v, list)
            and all(isinstance(item, dict) and "p" in item for item in v)
        ):
            for op in v:
                op_p = op.get("p") or ""
                op_v = op.get("v")
                if op_p.endswith("fragments") and isinstance(op_v, list):
                    for item in op_v:
                        if not isinstance(item, dict):
                            continue
                        idx = (_highest() + 1) if fragment_kinds else 0
                        _register(idx, _fragment_kind(item))
                        content = item.get("content") or ""
                        if content:
                            last_seen[idx] = content
                            kind = fragment_kinds[idx]
                            if kind == "answer":
                                yield from process_answer_chunk(content)
                            else:
                                yield (kind, content)
                elif op_p.endswith("content") and isinstance(op_v, str):
                    idx = _highest()
                    if idx not in fragment_kinds:
                        _register(idx, None)
                    kind = fragment_kinds[idx]
                    if kind == "answer":
                        yield from process_answer_chunk(op_v)
                    else:
                        yield (kind, op_v)
            continue

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

    # Flush remaining answer buffer & tool call buffer. Reprocess the buffered
    # tail first so a complete <tool_call> or DSML block that arrived at the end
    # is not leaked into the visible answer.
    yield from process_answer_chunk("")
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
    if in_dsml and dsml_buffer:
        close_idx = dsml_buffer.find(_DSML_CALLS_CLOSE)
        if close_idx != -1:
            for tc_json in _dsml_calls_to_json(dsml_buffer[:close_idx]):
                yield ("tool_call", tc_json)
            after = dsml_buffer[close_idx + len(_DSML_CALLS_CLOSE):]
            if after and after.lstrip().startswith("FINISHED"):
                after = after.lstrip()[len("FINISHED"):]
            if after:
                yield ("answer", after)
        else:
            _dlog(f"Warning: unclosed DSML block discarded: {dsml_buffer}")
    if answer_buffer:
        if strip_finished:
            probe = answer_buffer.lstrip()
            if probe.startswith("FINISHED"):
                answer_buffer = probe[len("FINISHED"):]
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
