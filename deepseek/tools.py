"""
Tool definitions, schema generation, and execution for DeepSeek function calling.
"""

from __future__ import annotations

import functools
import inspect
import json
import sys
import types
import typing
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, get_type_hints

_WARNED_TYPES: set[str] = set()


@dataclass
class ToolCall:
    """A tool call request emitted by the model."""
    name: str
    arguments: dict
    raw: str

    def __str__(self) -> str:
        return f"{self.name}({self.arguments})"


@dataclass
class Tool:
    """A registered callable tool."""
    name: str
    description: str
    parameters: dict
    fn: Callable
    deferred: bool = True
    eager: bool = False
    read_only: Optional[bool] = None

    def call(self, **kwargs) -> str:
        """Invoke fn, coercing the result to a string."""
        res = self.fn(**kwargs)
        if isinstance(res, str):
            return res
        return str(res)

    def schema(self) -> dict:
        """Return OpenAI-style function schema dict."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


def _parse_docstring(doc: str | None) -> tuple[str, dict[str, str]]:
    """Extract general description (first paragraph before Args:) and per-arg descriptions."""
    if not doc:
        return "", {}

    lines = doc.strip().splitlines()
    desc_lines: list[str] = []
    arg_descs: dict[str, str] = {}
    in_args = False

    current_arg: str | None = None
    current_arg_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("args:"):
            in_args = True
            continue

        if not in_args:
            desc_lines.append(line)
        else:
            # Look for "arg_name: description" or "arg_name (type): description"
            if ":" in stripped and not stripped.startswith(" "):
                if current_arg and current_arg_lines:
                    arg_descs[current_arg] = " ".join(current_arg_lines).strip()
                parts = stripped.split(":", 1)
                param_name = parts[0].split("(")[0].strip()
                current_arg = param_name
                current_arg_lines = [parts[1].strip()]
            elif current_arg:
                current_arg_lines.append(stripped)

    if current_arg and current_arg_lines:
        arg_descs[current_arg] = " ".join(current_arg_lines).strip()

    desc = "\n".join(desc_lines).strip()
    return desc, arg_descs


def _type_to_schema(tp: Any, tool_name: str, param_name: str) -> dict:
    if tp is str or tp is getattr(typing, "Optional", None):
        return {"type": "string"}
    if tp is int:
        return {"type": "integer"}
    if tp is float:
        return {"type": "number"}
    if tp is bool:
        return {"type": "boolean"}
    if tp is list or typing.get_origin(tp) is list:
        item_args = typing.get_args(tp)
        schema: dict[str, Any] = {"type": "array"}
        if item_args:
            schema["items"] = _type_to_schema(item_args[0], tool_name, param_name)
        return schema
    if tp is dict or typing.get_origin(tp) is dict:
        return {"type": "object"}

    # Handle Optional[X] or Union[X, None]
    origin = typing.get_origin(tp)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if args:
            return _type_to_schema(args[0], tool_name, param_name)

    warn_key = f"{tool_name}.{param_name}"
    if warn_key not in _WARNED_TYPES:
        _WARNED_TYPES.add(warn_key)
        sys.stderr.write(
            f"Warning: unsupported or missing type hint '{tp}' for parameter '{param_name}' in tool '{tool_name}'. Defaulting to string.\n"
        )
    return {"type": "string"}


def tool(fn_or_name: Any = None, **kwargs) -> Any:
    """Decorator to mark a Python function as a Tool.

    Usage:
        @tool
        def my_fn(...): ...

        @tool(name="custom_name")
        def my_fn(...): ...
    """
    def decorator(fn: Callable) -> Tool:
        name = kwargs.get("name") or getattr(fn, "__name__", "unnamed_tool")
        doc = getattr(fn, "__doc__", "") or ""
        description, arg_descs = _parse_docstring(doc)

        sig = inspect.signature(fn)
        try:
            type_hints = typing.get_type_hints(fn)
        except Exception:
            type_hints = {}

        properties: dict[str, dict] = {}
        required: list[str] = []

        for param_name, param in sig.parameters.items():
            if param_name in ("self", "cls"):
                continue

            th = type_hints.get(param_name, param.annotation)
            if th is inspect.Parameter.empty:
                th = str

            prop_schema = _type_to_schema(th, name, param_name)
            if param_name in arg_descs:
                prop_schema["description"] = arg_descs[param_name]

            properties[param_name] = prop_schema

            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        parameters = {
            "type": "object",
            "properties": properties,
        }
        if required:
            parameters["required"] = required

        eager = bool(kwargs.get("eager", False))
        deferred = kwargs.get("deferred")
        if deferred is None:
            deferred = not eager
        if eager:
            deferred = False
        read_only = kwargs.get("read_only")
        t = Tool(
            name=name,
            description=description,
            parameters=parameters,
            fn=fn,
            deferred=bool(deferred),
            eager=eager,
            read_only=read_only,
        )
        return t

    if callable(fn_or_name):
        return decorator(fn_or_name)
    return decorator


def execute_tool(call: ToolCall, tools: list[Tool]) -> str:
    """Look up tool by name, call it with arguments, and return string result or error."""
    tool_map = {t.name: t for t in tools}
    if call.name not in tool_map:
        available = ", ".join(sorted(tool_map)) or "(none)"
        return (
            f"Error: tool '{call.name}' is not registered. "
            f"Available tools: {available}"
        )
    target_tool = tool_map[call.name]
    try:
        return target_tool.call(**call.arguments)
    except Exception as e:
        return f"Error executing tool '{call.name}': {type(e).__name__}: {e}"
