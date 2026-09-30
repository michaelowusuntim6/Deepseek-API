"""Unofficial OpenAI-compatible client for chat.deepseek.com."""

from .auth import Session, get_session, login
from .client import DeepSeekClient, PartKind, Reply
from .extensions import (
    extension_report,
    load_extension_commands,
    load_extensions,
    reload_extensions,
)
from .pow import DeepSeekPow
from .tools import Tool, ToolCall, execute_tool, tool

__all__ = [
    "Session",
    "get_session",
    "login",
    "DeepSeekClient",
    "PartKind",
    "Reply",
    "DeepSeekPow",
    "tool",
    "Tool",
    "ToolCall",
    "execute_tool",
    "load_extensions",
    "load_extension_commands",
    "extension_report",
    "reload_extensions",
]
