"""Translate between OpenAI's chat-completions shapes and our DeepSeek client.

DeepSeek's protocol has no system/role channel — just a single `prompt` string.
So we flatten the OpenAI `messages` array into one prompt, and wrap DeepSeek's
text output back into OpenAI response/stream objects.
"""

from __future__ import annotations

import json
import secrets
import time
import uuid
from typing import Iterable, List, Optional, Union

from .schemas import ChatMessage

_ROLE_LABELS = {"system": "System", "user": "User", "assistant": "Assistant"}


def render_tools_preamble(tools: list[dict], tool_choice: Union[str, dict, None] = None) -> Optional[str]:
    """Render system preamble explaining available tools to DeepSeek."""
    if tool_choice == "none":
        return None

    schemas = []
    for t in tools:
        if isinstance(t, dict) and t.get("type") == "function" and "function" in t:
            fn = t["function"]
            schemas.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        elif isinstance(t, dict) and "name" in t:
            schemas.append({
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {"type": "object", "properties": {}}),
            })

    lines = [
        "You have access to the following tools. When you want to call a tool,",
        'respond with ONLY a single JSON object wrapped in <tool_call></tool_call>',
        'tags, and nothing else before or after it. The JSON must have keys',
        '"name" (string) and "arguments" (object). Wait for the tool result',
        'before continuing. If no tool is needed, respond normally without any',
        'tool_call tags.',
        '',
        'Available tools:',
        json.dumps(schemas, indent=2),
        '',
        'When you receive a tool result, it will appear as a user message',
        'prefixed with "TOOL RESULT for <tool_name>:". Use it to continue.',
    ]

    if tool_choice == "required":
        lines.append("You MUST call one of the tools listed above.")
    elif isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        fn_name = tool_choice.get("function", {}).get("name")
        if fn_name:
            lines.append(f"You MUST call the {fn_name} tool.")

    return "\n".join(lines)


def _text_of(content) -> str:
    """Extract plain text from a message's content (string or list-of-parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for p in content:
        if isinstance(p, dict) and p.get("type") == "text":
            parts.append(p.get("text", ""))
    return "\n".join(parts)


def messages_to_prompt(messages: List[ChatMessage]) -> str:
    """Flatten a chat history into a single prompt DeepSeek can answer."""
    if len(messages) == 1 and messages[0].role == "user" and not messages[0].tool_calls:
        return _text_of(messages[0].content)

    # Build lookup table for tool_call_id -> tool_name
    tc_id_to_name: dict[str, str] = {}
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                if isinstance(tc, dict):
                    t_id = tc.get("id")
                    t_name = tc.get("function", {}).get("name")
                    if t_id and t_name:
                        tc_id_to_name[t_id] = t_name

    lines = []
    for m in messages:
        if m.role == "tool":
            t_name = tc_id_to_name.get(m.tool_call_id or "", m.tool_call_id or m.name or "unknown_tool")
            tool_res_text = _text_of(m.content)
            lines.append(f"User: TOOL RESULT for {t_name}:\n{tool_res_text}")
        elif m.role == "assistant":
            parts = []
            text_content = _text_of(m.content)
            if text_content:
                parts.append(text_content)
            if m.tool_calls:
                for tc in m.tool_calls:
                    if isinstance(tc, dict) and "function" in tc:
                        fn = tc["function"]
                        fn_name = fn.get("name", "")
                        args_val = fn.get("arguments", {})
                        if isinstance(args_val, str):
                            try:
                                args_obj = json.loads(args_val)
                            except Exception:
                                args_obj = {"raw": args_val}
                        else:
                            args_obj = args_val
                        tc_json = json.dumps({"name": fn_name, "arguments": args_obj})
                        parts.append(f"<tool_call>{tc_json}</tool_call>")
            lines.append(f"Assistant: {' '.join(parts)}")
        else:
            label = _ROLE_LABELS.get(m.role, m.role.capitalize())
            lines.append(f"{label}: {_text_of(m.content)}")

    lines.append("Assistant:")
    return "\n\n".join(lines)


def _now() -> int:
    return int(time.time())


def _id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def _est_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) — DeepSeek's web API gives us no count."""
    return max(1, len(text) // 4)


def completion_response(model: str, content: str, prompt: str,
                        conversation_id: str = None) -> dict:
    """A full (non-streaming) OpenAI chat.completion object."""
    pt, ct = _est_tokens(prompt), _est_tokens(content)
    return {
        "id": _id(),
        "object": "chat.completion",
        "created": _now(),
        "model": model,
        "conversation_id": conversation_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
        },
    }


def completion_response_with_tool_call(
    model: str,
    tool_call: Any,
    conversation_id: Optional[str] = None,
    prompt: str = "",
) -> dict:
    """A full (non-streaming) OpenAI chat.completion object containing a tool call."""
    call_id = "call_" + secrets.token_hex(12)
    args_str = json.dumps(tool_call.arguments)
    pt, ct = _est_tokens(prompt), _est_tokens(args_str)
    return {
        "id": _id(),
        "object": "chat.completion",
        "created": _now(),
        "model": model,
        "conversation_id": conversation_id,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_call.name,
                                "arguments": args_str,
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
        },
    }


def stream_chunks(model: str, stream: Any) -> Iterable[str]:
    """Yield OpenAI SSE lines (`data: {...}\\n\\n`) for a streamed completion."""
    cid, created = _id(), _now()

    def frame(delta: dict, finish=None, extra: dict = None) -> str:
        obj = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if extra:
            obj.update(extra)
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    yield frame({"role": "assistant", "content": ""})

    emitted_tool_call = False

    if hasattr(stream, "iter_parts"):
        for kind, text in stream.iter_parts():
            if not text:
                continue
            if kind == "tool_call":
                emitted_tool_call = True
                try:
                    data = json.loads(text)
                    fn_name = data.get("name", "")
                    fn_args = json.dumps(data.get("arguments", {}))
                except Exception:
                    fn_name = "unknown"
                    fn_args = text

                call_id = "call_" + secrets.token_hex(12)

                # Chunk 1: announces tool_call with id, type, name, empty args
                yield frame({
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": fn_name,
                                "arguments": "",
                            },
                        }
                    ]
                })

                # Chunk 2: arguments fragment (no id/name/type)
                yield frame({
                    "tool_calls": [
                        {
                            "index": 0,
                            "function": {
                                "arguments": fn_args,
                            },
                        }
                    ]
                })
            elif kind == "answer":
                yield frame({"content": text})
    else:
        for d in stream:
            if d:
                yield frame({"content": d})

    conversation_id = getattr(stream, "conversation_id", None)
    finish_reason = "tool_calls" if emitted_tool_call else "stop"
    yield frame({}, finish=finish_reason, extra={"conversation_id": conversation_id})
    yield "data: [DONE]\n\n"
