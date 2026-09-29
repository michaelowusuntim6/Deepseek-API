"""Unofficial OpenAI-compatible client for chat.deepseek.com."""

from .auth import Session, get_session, login
from .client import DeepSeekClient, PartKind, Reply
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
]
