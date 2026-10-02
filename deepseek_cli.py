#!/usr/bin/env python3
"""
DeepSeek CLI — a Rich terminal client for chat.deepseek.com.

Examples:
    python deepseek_cli.py "say hello in one word"
    echo "hi" | python deepseek_cli.py
    python deepseek_cli.py
    python deepseek_cli.py --json "hi"
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import fnmatch
import html
import inspect
import io
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Callable
from html.parser import HTMLParser

try:
    import readline
except ImportError:  # pragma: no cover - Windows without pyreadline
    readline = None  # type: ignore[assignment]

from rich import box
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.padding import Padding
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deepseek import (
    DeepSeekClient,
    Tool,
    ToolCall,
    extension_report,
    load_extension_commands,
    load_extensions,
    reload_extensions,
    tool,
)
from deepseek.agent_tools import (
    AgentRuntime,
    ToolExecutor,
    apply_patch,
    exec_command,
    request_user_input,
    set_agent_runtime,
    update_plan,
    write_stdin,
)
from deepseek.auth import LoginRequired
from deepseek.compaction import CompactionManager
from deepseek.client import TOOL_SYSTEM_PREAMBLE
from deepseek.response import (
    DONE_TOKEN,
    MAX_CONTINUATIONS_PER_TURN,
    PatchStreamFilter,
    ResponseProcessor,
    TurnNudger,
    execute_tool_calls,
    strip_done,
)
from deepseek.agents_md import discover_agents, generate_draft
from deepseek.plan_store import PlanStore, render_checklist
from deepseek.skills import (
    clear_active_skills,
    list_skills,
    read_skill_file,
    skills_preamble,
    use_skill,
)
from deepseek.repl import create_session
from deepseek.tool_search import DeferredToolRegistry, search_tools
from deepseek.extensions import load_context_providers


__version__ = "0.4.0"

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_AUTH = 2
EXIT_USAGE = 3
EXIT_INTERRUPTED = 130
EXIT_SIGPIPE = 141

# Sent when a response is incomplete: it asks for a decision instead of the
# bare "Continue." that used to leave the model guessing.
CONTINUE_PROMPT = (
    "Are you done? If so, write <<DONE>> on its own line "
    "(with a blank line before it). If not, emit a tool call to "
    "continue the task. Do not write prose without one of these."
)


MODEL_CHOICES = {
    "chat": ("deepseek-chat", "default"),
    "expert": ("deepseek-expert", "expert"),
}

_BUILTIN_TOOL_NAMES = frozenset({
    "exec_command", "write_stdin", "apply_patch", "search_tools",
    "update_plan", "request_user_input",
    "read_file", "list_dir", "write_file", "run_shell",
    "edit_file", "grep", "find_files", "fetch_url",
})

_SKIP_DIRS = {
    "__pycache__", ".git", "venv", "node_modules", ".venv",
    ".mypy_cache", ".tox",
}


class UsageErrorParser(argparse.ArgumentParser):
    """ArgumentParser variant that uses exit code 3 for usage errors."""

    json_errors = False

    def error(self, message: str) -> None:
        if self.json_errors:
            sys.stdout.write(json.dumps({"kind": "error", "message": message}) + "\n")
            self.exit(EXIT_USAGE)
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


class WorkingIndicator:
    VERBS = ["Triangulating", "Dreaming", "Modelling", "Reasoning",
             "Cogitating", "Synthesizing", "Consulting", "Formulating", "Weaving"]

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._stopped = False

    def start(self):
        if not self._started:
            self._started = True
            self._stopped = False
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, daemon=True)
            _ACTIVE_INDICATORS.add(self)
            self._thread.start()

    def stop(self):
        if getattr(self, "_stopped", False):
            return
        self._stopped = True
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        _ACTIVE_INDICATORS.discard(self)
        sys.stderr.write("\r\033[2K")
        sys.stderr.flush()
        self._started = False

    def _run(self):
        idx = 0
        while not self._stop.is_set():
            sys.stderr.write(f"\r\033[2K{self.VERBS[idx % len(self.VERBS)]}…")
            sys.stderr.flush()
            idx += 1
            self._stop.wait(1.5)

_ACTIVE_INDICATORS: set[WorkingIndicator] = set()


def _stop_active_indicators() -> None:
    for indicator in list(_ACTIVE_INDICATORS):
        indicator.stop()


atexit.register(_stop_active_indicators)


# ----- Built-in tools --------------------------------------------------------

@tool
def read_file(path: str) -> str:
    """Read contents of a local file (capped at 32KB).

    Args:
        path: Path to the file.
    """
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"Error: File '{path}' does not exist."
        if not p.is_file():
            return f"Error: '{path}' is not a file."
        size = p.stat().st_size
        max_bytes = 32 * 1024
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_bytes)
        if size > max_bytes:
            content += f"\n\n[Notice: File truncated from {size} bytes to 32KB]"
        return content
    except Exception as e:
        return f"Error reading file '{path}': {type(e).__name__}: {e}"


@tool
def list_dir(path: str = ".") -> str:
    """List directory contents (sorted, capped at 200 entries).

    Args:
        path: Directory path to list.
    """
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"Error: Directory '{path}' does not exist."
        if not p.is_dir():
            return f"Error: '{path}' is not a directory."
        entries = sorted([e.name + ("/" if e.is_dir() else "") for e in p.iterdir()])
        count = len(entries)
        if count > 200:
            entries = entries[:200]
            entries.append(f"... ({count - 200} more entries omitted)")
        return "\n".join(entries)
    except Exception as e:
        return f"Error listing directory '{path}': {type(e).__name__}: {e}"


@tool
def write_file(path: str, content: str) -> str:
    """Write string content to a file.

    Args:
        path: Path to the target file.
        content: Text content to write.
    """
    try:
        p = Path(path).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return f"wrote {len(content.encode('utf-8'))} bytes to {path}"
    except Exception as e:
        return f"Error writing file '{path}': {type(e).__name__}: {e}"


@tool
def run_shell(command: str) -> str:
    """Run a shell command with a 30s timeout and output capped at 8KB.

    Args:
        command: Shell command string to execute.
    """
    try:
        res = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30.0,
        )
        output = f"Exit Code: {res.returncode}\n"
        if res.stdout:
            output += f"STDOUT:\n{res.stdout}\n"
        if res.stderr:
            output += f"STDERR:\n{res.stderr}\n"
        output_bytes = output.encode("utf-8")
        max_bytes = 8 * 1024
        if len(output_bytes) > max_bytes:
            output = (
                output_bytes[:max_bytes].decode("utf-8", errors="replace")
                + "\n[Output truncated at 8KB]"
            )
        return output
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 30 seconds."
    except Exception as e:
        return f"Error running shell command: {type(e).__name__}: {e}"


@tool
def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Edit a file by replacing old_string with new_string (max 1 MB).

    Args:
        path: Path to the file to edit.
        old_string: Exact text to find and replace.
        new_string: Replacement text.
        replace_all: If True, replace all occurrences; otherwise error if more than one found.
    """
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"error: file '{path}' does not exist"
        if not p.is_file():
            return f"error: '{path}' is not a file"
        size = p.stat().st_size
        if size > 1024 * 1024:
            return f"error: file '{path}' is {size} bytes — exceeds 1 MB limit"
        content = p.read_bytes().decode("utf-8", errors="replace")
        count = content.count(old_string)
        if count == 0:
            return f"error: old_string not found in {path}"
        if count > 1 and not replace_all:
            return (
                f"error: old_string appears {count} times in {path}; "
                f"pass replace_all=True or provide more context"
            )
        updated = (
            content.replace(old_string, new_string)
            if replace_all
            else content.replace(old_string, new_string, 1)
        )
        p.write_bytes(updated.encode("utf-8"))
        replaced = count if replace_all else 1
        return f"edited {path}: replaced {replaced} occurrence(s)"
    except Exception as e:
        return f"error editing file '{path}': {type(e).__name__}: {e}"


@tool
def grep(pattern: str, path: str = ".", glob: str = "*", max_results: int = 100) -> str:
    """Search text files recursively for a regex pattern (case-sensitive, like grep).

    Args:
        pattern: Regular expression to search for.
        path: Root directory to search (default: current directory).
        glob: Shell glob to filter filenames (default: '*' matches all).
        max_results: Maximum number of matching lines to return (default 100).
    """
    try:
        root = Path(path).expanduser().resolve()
        if not root.exists():
            return f"error: path '{path}' does not exist"
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"error: invalid regex '{pattern}': {e}"

        results: list[str] = []
        truncated = False
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fname in sorted(filenames):
                if not fnmatch.fnmatch(fname, glob):
                    continue
                fpath = Path(dirpath) / fname
                try:
                    with open(fpath, "rb") as fb:
                        chunk = fb.read(8192)
                    if b"\x00" in chunk:
                        continue
                except OSError:
                    continue
                try:
                    relpath = fpath.relative_to(root)
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        for lineno, line in enumerate(f, 1):
                            if rx.search(line):
                                entry = f"{relpath}:{lineno}: {line.rstrip()}"
                                results.append(entry[:300])
                                if len(results) >= max_results:
                                    truncated = True
                                    break
                    if truncated:
                        break
                except OSError:
                    continue
            if truncated:
                break

        if not results:
            return f"no matches for '{pattern}' in {path}"
        out = "\n".join(results)
        return out + "\n… (truncated)" if truncated else out
    except Exception as e:
        return f"error during grep: {type(e).__name__}: {e}"


@tool
def find_files(pattern: str, path: str = ".", max_results: int = 200) -> str:
    """Recursively find files whose name matches a shell glob pattern.

    Args:
        pattern: Shell glob pattern to match against filenames (e.g. '*.py').
        path: Root directory to search (default: current directory).
        max_results: Maximum number of results to return (default 200).
    """
    try:
        root = Path(path).expanduser().resolve()
        if not root.exists():
            return f"error: path '{path}' does not exist"

        matches: list[str] = []
        truncated = False
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for fname in sorted(filenames):
                if fnmatch.fnmatch(fname, pattern):
                    fpath = Path(dirpath) / fname
                    try:
                        matches.append(str(fpath.relative_to(root)))
                    except ValueError:
                        matches.append(str(fpath))
                    if len(matches) >= max_results:
                        truncated = True
                        break
            if truncated:
                break

        if not matches:
            return f"no files matching '{pattern}' found in {path}"
        matches.sort()
        out = "\n".join(matches)
        return out + f"\n… (truncated at {max_results})" if truncated else out
    except Exception as e:
        return f"error during find_files: {type(e).__name__}: {e}"


@tool
def fetch_url(url: str, max_bytes: int = 8000) -> str:
    """Fetch a URL via HTTP/HTTPS and return stripped text content.

    Args:
        url: The URL to fetch (http:// or https:// only).
        max_bytes: Maximum response body bytes to return (default 8000).
    """
    try:
        if not (url.startswith("http://") or url.startswith("https://")):
            return f"error: unsupported scheme in '{url}' — only http:// and https:// are allowed"
        req = urllib.request.Request(url, headers={"User-Agent": "DeepSeek-CLI/0.3"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        body = raw[:max_bytes].decode("utf-8", errors="replace")
        if truncated:
            body += "… (truncated)"
        body = re.sub(r"<script[^>]*>.*?</script>", " ", body, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<style[^>]*>.*?</style>", " ", body, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<[^>]+>", " ", body)
        return re.sub(r"\s+", " ", body).strip()
    except urllib.error.HTTPError as e:
        return f"error: HTTP {e.code} {e.reason} fetching '{url}'"
    except urllib.error.URLError as e:
        return f"error: URL error fetching '{url}': {e.reason}"
    except TimeoutError:
        return f"error: timed out fetching '{url}'"
    except Exception as e:
        return f"error fetching '{url}': {type(e).__name__}: {e}"


class _DDGResultParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self._in_title = False
        self._in_snippet = False
        self._href = ""
        self._title = ""
        self._snippet = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        cls = attrs.get("class", "")
        if tag == "a" and "result__a" in cls:
            self._in_title = True
            self._href = attrs.get("href", "")
            self._title = ""
        elif "result__snippet" in cls:
            self._in_snippet = True
            self._snippet = ""

    def handle_endtag(self, tag):
        if tag == "a" and self._in_title:
            self._in_title = False
        if self._in_snippet and tag in {"a", "div", "td"}:
            self._in_snippet = False
            if self._href and self._title:
                self.results.append((self._title.strip(), self._href, self._snippet.strip()))

    def handle_data(self, data):
        if self._in_title:
            self._title += data
        elif self._in_snippet:
            self._snippet += data


@tool(eager=True, read_only=True)
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web with DuckDuckGo HTML and return top results.

    Args:
        query: Search query.
        max_results: Maximum number of results.
    """
    try:
        from urllib.parse import quote_plus
        url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read(200000).decode("utf-8", errors="replace")
        parser = _DDGResultParser()
        parser.feed(body)
        results = parser.results[: max(1, int(max_results))]
        if not results:
            api_url = (
                "https://api.duckduckgo.com/?q="
                + quote_plus(query)
                + "&format=json&no_html=1&skip_disambig=1"
            )
            req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if data.get("AbstractURL"):
                results.append((
                    data.get("Heading") or query,
                    data["AbstractURL"],
                    data.get("AbstractText") or "",
                ))
            for topic in data.get("RelatedTopics") or []:
                if len(results) >= max(1, int(max_results)):
                    break
                if isinstance(topic, dict) and topic.get("FirstURL"):
                    results.append((
                        topic.get("Text") or query,
                        topic["FirstURL"],
                        topic.get("Text") or "",
                    ))
        if not results:
            return "Error: web_search returned no parseable results."
        return "\n\n".join(
            f"{title}\n{url}\n{snippet}" for title, url, snippet in results
        )
    except Exception as exc:
        return f"Error: web_search failed: {type(exc).__name__}: {exc}"


def legacy_tools() -> list[Tool]:
    """Return the legacy file-oriented tool tier."""
    tools = [
        read_file,
        list_dir,
        write_file,
        run_shell,
        edit_file,
        grep,
        find_files,
        fetch_url,
    ]
    for tool_obj in tools:
        tool_obj.eager = True
        tool_obj.deferred = False
    return tools


def codex_default_tools(plan_enabled: bool = False,
                        plan_mode: bool = False) -> list[Tool]:
    """Return the eager Codex-style tool set."""
    _ = (plan_enabled, plan_mode)
    return [
        exec_command,
        write_stdin,
        apply_patch,
        search_tools,
        update_plan,
        request_user_input,
        list_skills,
        use_skill,
        read_skill_file,
        fetch_url,
        web_search,
    ]


def build_tool_registry(
    *,
    legacy_enabled: bool,
    plan_enabled: bool,
    plan_mode: bool,
    verbose: bool = True,
) -> DeferredToolRegistry:
    """Build the eager/deferred tool registry used by the CLI."""
    eager = codex_default_tools(plan_enabled=plan_enabled, plan_mode=plan_mode)
    if legacy_enabled:
        eager.extend(legacy_tools())

    deferred: list[Tool] = []
    eager_names = {t.name for t in eager}
    added: list[str] = []
    for tool_obj in load_extensions():
        if tool_obj.name in eager_names:
            continue
        if tool_obj.eager:
            tool_obj.deferred = False
            eager.append(tool_obj)
            eager_names.add(tool_obj.name)
        else:
            tool_obj.deferred = True
            deferred.append(tool_obj)
        added.append(tool_obj.name)

    if added and verbose:
        sys.stderr.write(
            f"[cli] loaded {len(added)} extension tool(s): {', '.join(added)}\n"
        )
    return DeferredToolRegistry(eager, deferred)


def default_tools(verbose: bool = True) -> list[Tool]:
    """Backward-compatible visible-tool helper."""
    return build_tool_registry(
        legacy_enabled=False,
        plan_enabled=False,
        plan_mode=False,
        verbose=verbose,
    ).visible_tools()


BUILTIN_SLASH_COMMANDS = {
    "/help": "show commands and usage",
    "/new": "start a fresh thread",
    "/thread": "print the current conversation_id",
    "/clear": "clear the terminal display",
    "/model": "show or set model: chat | expert",
    "/thinking": "toggle DeepThink reasoning",
    "/search": "toggle DeepSeek web search (enabled by default)",
    "/mode": "set approval mode: manual | auto",
    "/tools": "set tools: off | manual | auto | list",
    "/compact": "summarize and restart the context window",
    "/plan": "show, clear, or resume the persistent plan",
    "/agents": "show the loaded AGENTS.md path and line count",
    "/agents-reload": "reload AGENTS.md from disk",
    "/agents-init": "generate AGENTS.md for the current repository",
    "/extensions": "list loaded extensions and their tools",
    "/reload": "reload extensions from disk",
    "/exit": "quit",
}

SLASH_COMMAND_ARGUMENTS = {
    "/model": [("chat", "Fast default"), ("expert", "Stronger, slower")],
    "/mode": [("manual", "Prompt before tools"), ("auto", "Run tools unattended")],
    "/tools": [
        ("off", "Disable tool calling"),
        ("manual", "Prompt before each tool call"),
        ("auto", "Run without prompting"),
        ("list", "Show all registered tools"),
    ],
    "/plan": [("clear", "Delete plan"), ("resume", "Resume in-progress step")],
    "/thinking": [("on", "Enable DeepThink"), ("off", "Disable DeepThink")],
    "/search": [("on", "Enable web search"), ("off", "Disable web search")],
}

_TIPS = [
    "Use --json for one JSON object per line on stdout.",
    "Use --tools manual to approve every tool call.",
    "Use --resume <conversation_id> to continue a prior thread.",
    "Use --show-thinking to inspect reasoning.",
    "Type /help to list slash commands.",
]


def build_parser() -> UsageErrorParser:
    p = UsageErrorParser(
        prog="deepseek_cli.py",
        description="Rich terminal client for chat.deepseek.com.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes:\n"
            "  0 success   1 runtime error   2 auth required   3 usage error\n\n"
            "Examples:\n"
            '  python deepseek_cli.py "say hello"\n'
            '  echo "hi" | python deepseek_cli.py\n'
            '  python deepseek_cli.py --tools auto "what is the weather in Tokyo?"\n'
        ),
    )
    p.add_argument("prompt", nargs="?", help="prompt for one-shot mode; omit for the REPL")
    p.add_argument("--model", choices=sorted(MODEL_CHOICES), help="model alias: chat or expert")
    p.add_argument("--no-thinking", action="store_true",
                   help="disable DeepThink reasoning (enabled by default)")
    p.add_argument("--no-search", action="store_true",
                   help="disable DeepSeek model-side web search (enabled by default)")
    p.add_argument(
        "--tools",
        choices=("off", "manual", "auto"),
        default="auto",
        help="tool mode: off (strip tools), manual (approve), auto (unattended)",
    )
    p.add_argument("--show-thinking", action="store_true", help="display DeepThink reasoning")
    p.add_argument("--json", action="store_true", help="emit JSONL events on stdout")
    p.add_argument("--resume", metavar="CONVERSATION_ID", help="resume an existing thread")
    p.add_argument("--no-stream", action="store_true", help="buffer the answer; do not stream tokens")
    p.add_argument("--no-markdown", action="store_true",
                   help="disable Markdown rendering during terminal streaming")
    p.add_argument("--no-diff", action="store_true",
                   help="suppress apply_patch diff previews")
    p.add_argument("--legacy-tools", action="store_true",
                   help="also expose the legacy file-oriented tool tier")
    p.add_argument("--compact-at", type=int,
                   help="compact when estimated context tokens exceed this value (default: 500000)")
    p.add_argument("--plan-mode", action="store_true",
                   help="enable request_user_input for structured plan questions")
    p.add_argument("--mode", choices=("normal", "agent"), default="normal",
                   help="agent mode enables persistent plan tracking")
    p.add_argument("--no-memory", action="store_true",
                   help="skip memory auto-injection")
    p.add_argument("--no-agents", action="store_true",
                   help="skip AGENTS.md discovery")
    p.add_argument("--no-skills", action="store_true",
                   help="skip skills discovery and preamble listing")
    p.add_argument("--generate-agents", action="store_true",
                   help="generate AGENTS.md for the current repository and exit")
    p.add_argument("--show-preamble", action="store_true",
                   help="print the assembled system preamble and exit")
    p.add_argument("--version", action="version", version=f"deepseek-cli {__version__}")
    return p


class DeepSeekCLI:
    """Stateful Rich CLI shared by one-shot, piped, and interactive modes."""

    def __init__(self, args: argparse.Namespace, parser: UsageErrorParser,
                 interactive: bool, prompt: str | None):
        self.args = args
        self.parser = parser
        self.interactive = interactive
        self.json_mode = bool(args.json)
        self.stream = not args.no_stream
        self.no_markdown = bool(args.no_markdown)
        self.no_diff = bool(args.no_diff)
        self.no_memory = bool(args.no_memory)
        self.no_agents = bool(args.no_agents)
        self.no_skills = bool(args.no_skills)
        self.generate_agents = bool(args.generate_agents)
        self.show_preamble = bool(args.show_preamble)
        self.show_thinking = bool(args.show_thinking)
        if args.model:
            self.model_alias = args.model
        elif args.tools == "auto" or args.mode == "agent":
            self.model_alias = "expert"
        else:
            self.model_alias = "chat"
        self.model = MODEL_CHOICES[self.model_alias][0]
        self.wire_model_first = MODEL_CHOICES[self.model_alias][1]
        self.thinking = not bool(getattr(args, "no_thinking", False))
        self.search = not bool(getattr(args, "no_search", False))
        self.tools_mode = args.tools
        self.mode = args.mode
        self.legacy_tools_enabled = bool(args.legacy_tools)
        self.autonomous = bool(args.tools == "auto" or args.mode == "agent")
        self.plan_mode = bool(args.plan_mode or args.mode == "agent")
        self.plan_enabled = bool(self.autonomous or self.plan_mode)
        self.conversation_id = args.resume
        self.context_chars = 0
        self.client: DeepSeekClient | None = None
        self.tools: list[Tool] = []
        self.registry = DeferredToolRegistry()
        self.plan_store = PlanStore()
        self.compaction = CompactionManager(args.compact_at, plan_store=self.plan_store)
        self.runtime = AgentRuntime(
            conversation_id=self.conversation_id,
            plan_mode=self.plan_mode,
            autonomous=self.autonomous,
            json_mode=bool(args.json),
            interactive=interactive,
            approval="auto" if self.tools_mode == "auto" else "manual",
            ask=self._runtime_ask,
            emit_plan=self._render_plan,
            emit_file_change=self._render_file_change,
            plan_store=self.plan_store,
        )
        self.extension_commands: dict[str, Callable] = {}
        self._extensions_reported = False
        self.repl_session = None
        self.approved_tools: set[str] = set()
        self._pending_tool_names: deque[str] = deque()
        self._answer_parts: list[str] = []
        self._raw_answer_parts: list[str] = []
        self._answer_tail = ""
        self._patch_filter = PatchStreamFilter()
        self._response_processor = ResponseProcessor()
        self._original_prompt: str | None = None
        self._thinking_parts: list[str] = []
        self._answer_line_open = False
        self._thinking_line_open = False
        self._answer_line_buffer = ""
        self._last_render_ended_with_newline = True
        self._in_code_fence = False
        self._fence_lines: list[str] = []
        self._fence_language = "text"
        self._just_closed_code_fence = False
        self._code_gap_emitted = False
        self._last_output_blank = False
        self._pending_compaction_prefix: str | None = None
        self._summary_prefix_for_next_request: str | None = None
        self._plan_injected_cid: str | None = None
        self._turn_tool_result_chars = 0
        self._json_tool_event_seen = False
        self._json_answer_buffer = ""
        self.agents_path: Path | None = None
        self.agents_text = ""
        self.memory_text = ""
        self.skills_text = ""
        self._preamble_loaded = False

        stderr_ui = (not self.interactive) or self.json_mode
        self.ui = Console(stderr=stderr_ui, highlight=False, soft_wrap=True)
        self.out = Console(highlight=False, soft_wrap=True)

    # ----- output ----------------------------------------------------------

    def emit(self, message: str = "", kind: str = "system", **fields: Any) -> None:
        """Public extension-friendly helper: emit a system or JSON event."""
        if kind != "system":
            self.emit_event(kind, message, **fields)
        else:
            self._render_system(str(message))

    def emit_event(self, kind: str, text: str = "", **fields: Any) -> None:
        if self.json_mode:
            if kind == "answer" and not self._json_tool_event_seen:
                self._json_answer_buffer += text
                return
            event = {"kind": kind, **fields}
            if kind in {"answer", "thinking"}:
                event["text"] = text
            elif kind == "tool_result":
                event["result"] = text
            elif kind == "error":
                event["message"] = fields.get("message", text)
            elif kind == "system":
                event["text"] = text
            if kind in {"tool_call", "tool_result"}:
                self._json_tool_event_seen = True
            try:
                sys.stdout.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
                sys.stdout.flush()
            except BrokenPipeError:
                raise
            return

        if kind == "answer":
            if not self._answer_line_open:
                self._close_thinking_line()
                self.out.print()
                self._answer_line_open = True
            self._emit_answer_text(text)
            self._last_render_ended_with_newline = text.endswith("\n")
        elif kind == "thinking":
            if self.show_thinking:
                if not self._thinking_line_open:
                    self.ui.print("\n[dim]Thinking[/]")
                    self._thinking_line_open = True
                self.ui.print(text, end="", markup=False, highlight=False)
                self._last_render_ended_with_newline = text.endswith("\n")
        elif kind == "tool_call":
            self._close_answer_line()
            args = json.dumps(fields.get("arguments", {}), ensure_ascii=False)
            self.ui.print(
                f"\n[bold yellow]tool[/] [cyan]{fields.get('name', 'unknown')}[/] "
                f"[dim]{args}[/]"
            )
            self._last_render_ended_with_newline = True
        elif kind == "tool_result":
            result = self._short_result(text)
            self.ui.print(
                f"[dim]  result[/] [cyan]{fields.get('name', 'unknown')}[/] "
                f"[dim]{result}[/]"
            )
            self._last_render_ended_with_newline = True
        elif kind == "system":
            self._render_system(text)
        elif kind == "error":
            self._render_error(fields.get("message", text))

    def _render_system(self, message: str) -> None:
        if self.json_mode:
            return
        if message:
            self.ui.print(message)

    def _render_error(self, message: str) -> None:
        if self.json_mode:
            return
        self.ui.print(f"[bold red]error:[/] {message}")

    def _close_answer_line(self) -> None:
        if self._in_code_fence:
            self._flush_code_fence()
        if self._answer_line_buffer:
            self.out.print(Markdown(self._answer_line_buffer))
            self._answer_line_buffer = ""
        if self._answer_line_open:
            self.out.print()
            self._answer_line_open = False

    def _ensure_fresh_line(self) -> None:
        """Make the next rendered output start on its own line.

        Continuations and tool results reuse the same output stream as the
        previous response; without this, a response that did not end in a
        newline glues onto the one before it.
        """
        if self.json_mode or self._last_render_ended_with_newline:
            return
        if self._answer_line_buffer or self._answer_line_open:
            # Flushes the pending answer line (and closes the block) so the
            # next text starts fresh instead of continuing that line.
            self._close_answer_line()
        else:
            sys.stdout.write("\n")
            sys.stdout.flush()
        self._last_render_ended_with_newline = True

    def _markdown_streaming_enabled(self) -> bool:
        return (
            not self.json_mode
            and not self.no_markdown
            and sys.stdout.isatty()
        )

    def _emit_answer_text(self, text: str) -> None:
        if not self._markdown_streaming_enabled():
            self.out.print(text, end="", markup=False, highlight=False)
            return
        self._answer_line_buffer += text
        while "\n" in self._answer_line_buffer:
            line, self._answer_line_buffer = self._answer_line_buffer.split("\n", 1)
            self._emit_markdown_line(line.rstrip("\r"))

    def _flush_code_fence(self) -> None:
        if not self._fence_lines:
            self._in_code_fence = False
            return
        while self._fence_lines and not self._fence_lines[-1].strip():
            self._fence_lines.pop()
        code = "\n".join(self._fence_lines)
        self.out.print(Padding(
            Syntax(
                code,
                self._fence_language or "text",
                theme="monokai",
                background_color="default",
                line_numbers=False,
            ),
            (0, 0, 0, 2),
        ))
        self.out.print()
        self._fence_lines = []
        self._fence_language = "text"
        self._in_code_fence = False
        self._just_closed_code_fence = True
        self._code_gap_emitted = True
        self._last_output_blank = True

    def _emit_markdown_line(self, line: str) -> None:
        stripped = line.strip()
        if not self._in_code_fence and stripped.startswith("```"):
            if not self._last_output_blank:
                self.out.print()
            self._in_code_fence = True
            self._fence_language = stripped[3:].strip() or "text"
            return
        if self._in_code_fence and stripped.startswith("```"):
            self._flush_code_fence()
            return
        if self._in_code_fence:
            self._fence_lines.append(line)
            return
        if self._just_closed_code_fence:
            if not line.strip():
                if not self._code_gap_emitted:
                    self.out.print()
                    self._code_gap_emitted = True
                    self._last_output_blank = True
                return
            self._just_closed_code_fence = False
        if not line.strip():
            self.out.print()
            self._last_output_blank = True
            return
        self.out.print(Markdown(line))
        self._last_output_blank = not line.strip()

    def _close_thinking_line(self) -> None:
        if self._thinking_line_open:
            self.ui.print()
            self._thinking_line_open = False

    @staticmethod
    def _short_result(result: str, limit: int = 220) -> str:
        first = (result or "").strip().splitlines()[0] if (result or "").strip() else ""
        return first if len(first) <= limit else first[: limit - 1] + "…"

    # ----- startup UI ------------------------------------------------------

    def render_startup(self) -> None:
        if self.json_mode:
            return
        try:
            width = min(self.ui.width, 100)
        except Exception:
            width = 80
        version_line = f"deepseek-cli v{__version__}  ·  model {self.model}"
        update = os.getenv("DEEPSEEK_CLI_UPDATE_NOTICE")
        body = version_line
        body += "\n[dim]By Michael Owusu Ntim[/]"
        if update:
            body += f"\n[bold yellow]update:[/] {update}"
        self.ui.print(Panel(body, title="DeepSeek CLI", box=box.ROUNDED, width=width))

        self.print_session_panel()
        self.ui.print(f"[dim]Tip: {random.choice(_TIPS)}[/]")

    def print_session_panel(self) -> None:
        try:
            width = min(self.ui.width, 100)
        except Exception:
            width = 100
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        cwd = str(Path.cwd())
        home = str(Path.home())
        if cwd.startswith(home):
            cwd = "~" + cwd[len(home):]
        table.add_row("dir", cwd)
        table.add_row("permissions", f"tools={self.tools_mode}")
        table.add_row("deferred", str(len(self.registry.deferred_tools)))
        table.add_row("compact", f"{self.compaction.threshold:,} tokens")
        self.ui.print(Panel(table, title="Session", box=box.SQUARE, width=width))

    def toolbar_text(self):
        return (
            f" {self.model}  ·  think {'on' if self.thinking else 'off'}"
            f"  ·  model-search {'on' if self.search else 'off'}"
            f"  ·  tools {self.tools_mode}"
            f"  ·  agent {self.mode}"
            f"  ·  {self.short_thread()}  ·  ctx ~{max(0, self.context_chars // 4):,} tok "
        )

    def short_thread(self) -> str:
        if not self.conversation_id:
            return "(new)"
        sid = self.conversation_id.split(":", 1)[0]
        return sid[:12] + "…" if len(sid) > 12 else sid

    def context_estimate(self) -> str:
        tokens = max(0, self.context_chars // 4)
        return f"~{tokens:,} tokens (estimate)"

    def render_status(self) -> None:
        if self.json_mode:
            return
        text = Text()
        text.append(self.model, style="bold")
        text.append(f" · {self.mode}/{self.tools_mode} · {self.short_thread()} · ")
        text.append(f"ctx ~{max(0, self.context_chars // 4):,} tok · ", style="dim")
        text.append("/help · /new · /exit", style="dim")
        self.ui.print(text)

    # ----- auth/client -----------------------------------------------------

    def session_refreshed(self, message: str) -> None:
        self._render_system(f"[yellow]{message}[/]")

    def init_client(self) -> None:
        try:
            if self.json_mode:
                with contextlib.redirect_stdout(sys.stderr):
                    self.client = DeepSeekClient(
                        allow_interactive=True,
                        on_session_refresh=self.session_refreshed,
                    )
            else:
                self.client = DeepSeekClient(
                    allow_interactive=True,
                    on_session_refresh=self.session_refreshed,
                )
        except LoginRequired as e:
            self.emit_event("error", message=str(e))
            raise SystemExit(EXIT_AUTH)
        except Exception as e:
            self.emit_event("error", message=f"failed to initialise session: {e}")
            raise SystemExit(EXIT_RUNTIME)

        self.reload_tools()
        self.extension_commands = load_extension_commands()
        self.refresh_repl_session()
        self._load_context_layers()
        self.runtime.conversation_id = self.conversation_id
        self.runtime.tool_registry = self.registry
        set_agent_runtime(self.runtime)
        self._load_and_show_resumed_plan()
        self.configure_readline()

    def reload_tools(self) -> None:
        self.registry = build_tool_registry(
            legacy_enabled=self.legacy_tools_enabled,
            plan_enabled=self.plan_enabled,
            plan_mode=self.plan_mode,
            verbose=not self.json_mode and not self._extensions_reported,
        )
        self._extensions_reported = True
        self.tools = self.registry.visible_tools()
        self.runtime.tool_registry = self.registry
        set_agent_runtime(self.runtime)

    def _load_context_layers(self) -> None:
        if self.no_skills:
            self.skills_text = ""
        else:
            self.skills_text, warning = skills_preamble()
            if warning:
                sys.stderr.write(warning + "\n")
        if not self.no_agents:
            found = discover_agents()
            if found:
                self.agents_path, self.agents_text = found
                line_count = len(self.agents_text.splitlines())
                sys.stderr.write(f"[agents] loaded {self.agents_path} ({line_count} lines)\n")
        self.memory_text = ""
        if not self.no_memory:
            summary = ""
            if self.agents_text:
                summary = self.agents_text.splitlines()[0] if self.agents_text.splitlines() else ""
            memories = []
            for provider in load_context_providers():
                try:
                    text = provider(cwd=str(Path.cwd()), agents_summary=summary)
                except TypeError:
                    try:
                        text = provider(str(Path.cwd()), summary)
                    except Exception:
                        continue
                except Exception:
                    continue
                if text:
                    memories.append(text)
            if memories:
                self.memory_text = "\n".join(memories)[:2000]
        self._preamble_loaded = True

    def _reload_memory(self) -> None:
        self._load_context_layers()

    def _context_sections(self) -> list[str]:
        sections = []
        if self.skills_text:
            sections.append(self.skills_text)
        if self.memory_text:
            sections.append("## Long-term memory (auto-retrieved)\n" + self.memory_text)
        if self.agents_text:
            sections.append("## Project instructions (AGENTS.md)\n" + self.agents_text)
        return sections

    def _runtime_ask(self, prompt: str) -> str:
        if self.json_mode:
            sys.stderr.write(prompt)
            sys.stderr.flush()
            return sys.stdin.readline().strip()
        try:
            return input(prompt).strip()
        except EOFError:
            return ""

    def _render_plan(self, plan: list[dict[str, Any]]) -> None:
        self._plan_injected_cid = self.conversation_id
        checklist = render_checklist(plan)
        if self.json_mode:
            self.emit_event("plan", plan=plan)
        else:
            self._render_system(f"[bold]Plan[/]\n{checklist}")

    def _render_file_change(self, path: str, additions: int,
                            deletions: int, diff_text: str) -> None:
        if self.no_diff:
            return
        if self.json_mode:
            self.emit_event(
                "file_change",
                path=path,
                additions=additions,
                deletions=deletions,
                diff=diff_text,
            )
            return
        if sys.stdout.isatty():
            rendered = []
            for line in (diff_text or "").splitlines():
                if line.startswith("+++") or line.startswith("---"):
                    rendered.append(Text(line, style="bold"))
                elif line.startswith("@@"):
                    rendered.append(Text(line, style="bold cyan"))
                elif line.startswith("+"):
                    rendered.append(Text(line, style="bright_green on #0d2818"))
                elif line.startswith("-"):
                    rendered.append(Text(line, style="bright_red on #2d0d0d"))
                else:
                    rendered.append(Text(line))
            self.ui.print(
                Panel(
                    Group(*rendered) if rendered else Text("(no textual diff)"),
                    title=path if len(path) <= 60 else path[:57] + "...",
                    border_style="cyan",
                    expand=False,
                )
            )
            self.ui.print(f"• Edited {path} (+{additions} -{deletions})")
            return
        if diff_text:
            self.out.print(diff_text, markup=False, highlight=False)
        self.ui.print(f"• Edited {path} (+{additions} -{deletions})")

    def _load_and_show_resumed_plan(self) -> None:
        if not self.conversation_id:
            return
        data = self.plan_store.load(self.conversation_id)
        if data and data.get("plan"):
            self._render_plan(data["plan"])
            self._plan_injected_cid = None

    def configure_readline(self) -> None:
        if readline is None:
            return
        commands = sorted(set(BUILTIN_SLASH_COMMANDS) | set(self.extension_commands))

        def completer(text: str, state: int) -> str | None:
            if not text.startswith("/"):
                return None
            options = [cmd for cmd in commands if cmd.startswith(text)]
            return options[state] if state < len(options) else None

        readline.set_completer(completer)
        readline.set_completer_delims(" \t\n")
        try:
            readline.parse_and_bind("tab: complete")
        except Exception:
            pass

    def refresh_repl_session(self) -> None:
        commands = dict(BUILTIN_SLASH_COMMANDS)
        for name in sorted(self.extension_commands):
            commands.setdefault(name, "extension command")
        self.repl_session = create_session(
            sorted(commands.items()),
            SLASH_COMMAND_ARGUMENTS,
            bottom_toolbar=self.toolbar_text,
        )

    # ----- turn execution --------------------------------------------------

    def run_prompt(self, prompt: str) -> int:
        if self.client is None:
            self.init_client()
        assert self.client is not None

        self._pending_tool_names.clear()
        self._answer_parts = []
        self._raw_answer_parts = []
        self._answer_tail = ""
        self._patch_filter = PatchStreamFilter()
        self._response_processor = ResponseProcessor()
        if self._original_prompt is None:
            self._original_prompt = prompt
        self._thinking_parts = []
        self._answer_line_open = False
        self._thinking_line_open = False
        self._last_render_ended_with_newline = True
        self._json_tool_event_seen = False
        self._json_answer_buffer = ""
        self._turn_tool_result_chars = 0

        if not self.json_mode and not self.interactive:
            self.render_startup()
            self.render_status()

        self._maybe_compact_before_turn()
        prompt = self._prepare_prompt(prompt)

        try:
            if self.tools_mode == "off" or not self.tools:
                return self._run_plain_turn(prompt)
            return self._run_agent_turn(prompt)
        except KeyboardInterrupt:
            self._close_answer_line()
            self.emit_event("error", message="interrupted")
            return EXIT_INTERRUPTED
        except BrokenPipeError:
            raise
        except LoginRequired as e:
            self.emit_event("error", message=str(e))
            return EXIT_AUTH
        except Exception as e:
            self._close_answer_line()
            self.emit_event("error", message=f"{type(e).__name__}: {e}")
            return EXIT_RUNTIME

    def _run_plain_turn(self, prompt: str) -> int:
        assert self.client is not None
        if self._summary_prefix_for_next_request:
            prompt = self._summary_prefix_for_next_request + "\n\n" + prompt
            self._summary_prefix_for_next_request = None
        wire_model = self.wire_model_first if self.conversation_id is None else None
        if self.stream:
            stream = self.client.stream(
                prompt,
                conversation_id=self.conversation_id,
                model=wire_model,
                thinking=self.thinking,
                search=self.search,
            )
            indicator = WorkingIndicator()
            indicator.start()
            for kind, text in stream.iter_parts():
                indicator.stop()
                self._handle_part(kind, text)
            new_cid = stream.conversation_id
        else:
            reply = self.client.chat(
                prompt,
                conversation_id=self.conversation_id,
                model=wire_model,
                thinking=self.thinking,
                search=self.search,
            )
            if reply.thinking:
                self._handle_part("thinking", reply.thinking)
            self._handle_part("answer", reply.text)
            new_cid = reply.conversation_id
        self._finish_turn(prompt, new_cid)
        return EXIT_OK

    def _run_agent_turn(self, prompt: str) -> int:
        assert self.client is not None
        approval: str | Callable[[ToolCall], bool | tuple[bool, bool]]
        approval = "auto" if self.tools_mode == "auto" else self._approval_callback
        executor = ToolExecutor(approval=approval)
        current_prompt = prompt
        current_cid = self.conversation_id
        wire_model = self.wire_model_first if current_cid is None else None
        turn_prompt_chars = 0
        summary_prefix = self._summary_prefix_for_next_request
        self._summary_prefix_for_next_request = None
        nudger = TurnNudger(MAX_CONTINUATIONS_PER_TURN)
        had_tool_call = False
        tool_count = 0
        last_response_text = ""
        parse_fail_count = 0
        self._indicator = WorkingIndicator()

        for iteration in range(8):
            visible = self.registry.visible_tools()
            schema = json.dumps([tool_obj.schema() for tool_obj in visible], indent=2)
            tool_prompt = (
                self.client._tool_prompt(
                    current_prompt, schema, current_cid,
                    extra_sections=self._context_sections(),
                )
                if visible
                else current_prompt
            )
            request_prompt = (
                summary_prefix + "\n\n" + tool_prompt
                if iteration == 0 and summary_prefix
                else tool_prompt
            )
            turn_prompt_chars += len(request_prompt)

            self._response_processor = ResponseProcessor()
            self._answer_tail = ""
            self._patch_filter = PatchStreamFilter()
            self._indicator.start()
            stream = self.client.stream(
                request_prompt,
                conversation_id=current_cid,
                model=wire_model if iteration == 0 else None,
                thinking=self.thinking,
                search=self.search,
            )
            for kind, text in stream.iter_parts():
                # Stop (and join) the spinner before the first rendered chunk so
                # it can never write over the response line.
                self._indicator.stop()
                self._handle_part(kind, text)

            current_cid = stream.conversation_id
            self.conversation_id = current_cid
            self.runtime.conversation_id = current_cid
            if visible:
                self.client._remember_tool_prompt(schema, current_cid)

            last_response_text = (
                self._response_processor.raw_answer or "".join(self._answer_parts)
            )
            # One unified parse of the whole response: JSON, freeform patches
            # and DSML all land here, in the order the model emitted them.
            calls = self._response_processor.parsed_tool_calls

            if calls:
                had_tool_call = True
                tool_count += len(calls)
                nudger.reset()
                decisions = (
                    self._approve_tool_batch(calls)
                    if self.tools_mode == "manual"
                    else None
                )
                results = execute_tool_calls(executor, calls, visible, decisions=decisions)
                for call, result in results:
                    self._turn_tool_result_chars += len(result)
                    self._emit_tool_result(call, result)
                self.registry.finish_iteration()
                body = "\n\n".join(
                    f"TOOL RESULT for {call.name}:\n{result}"
                    for call, result in results
                )
                current_prompt = (
                    body + "\n\n---\n[TASK STATUS]\n"
                    "Original user request (verbatim, truncated to 300 chars):\n"
                    f"{(self._original_prompt or prompt)[:300]}\n\n"
                    f"Tool calls completed so far: {tool_count}\n\n"
                    "Continue the task: call another tool if you need one, otherwise "
                    "give your final answer."
                )
                wire_model = None
                continue

            if self._response_processor.complete:
                self.registry.finish_iteration()
                self._indicator.stop()
                break

            # Incomplete: a soft ping, never a correction.
            decision = nudger.note(None)
            self.registry.finish_iteration()
            self._indicator.stop()
            if decision == "give_up":
                path = nudger.write_stop_file(last_response_text)
                sent = max(1, nudger.total_nudges - 1)
                sys.stderr.write(
                    "\n"
                    f"The model made no progress after {sent} "
                    "continuations, so the turn was stopped.\n"
                    f"The last response was saved to {path}\n"
                    "\n"
                )
                sys.stderr.flush()
                break
            self._ensure_fresh_line()
            current_prompt = CONTINUE_PROMPT

        self._finish_turn(prompt, current_cid, prefix_chars=turn_prompt_chars)
        self._indicator.stop()
        return EXIT_OK

    def _finish_turn(self, prompt: str, new_cid: str | None,
                     prefix_chars: int | None = None) -> None:
        tail = self._flush_answer_tail()
        if tail:
            self._answer_parts.append(tail)
            if self.stream:
                self.emit_event("answer", tail)
        if not self.stream:
            thinking = "".join(self._thinking_parts)
            answer = strip_done("".join(self._answer_parts))
            if thinking:
                self.emit_event("thinking", thinking)
            if answer:
                self.emit_event("answer", answer)

        self._close_answer_line()
        self._close_thinking_line()
        self._flush_json_answer()
        clear_active_skills()
        if new_cid:
            self.conversation_id = new_cid
            self.runtime.conversation_id = new_cid
            if not self.interactive and not self.json_mode:
                self.ui.print(f"[dim]conversation_id = {new_cid}[/]")
        prompt_chars = prefix_chars if prefix_chars is not None else len(prompt)
        self.context_chars += (
            prompt_chars
            + len("".join(self._answer_parts))
            + len("".join(self._thinking_parts))
            + self._turn_tool_result_chars
        )

    def _flush_json_answer(self) -> None:
        if not self.json_mode or not self._json_answer_buffer:
            return
        text = self._json_answer_buffer
        self._json_answer_buffer = ""
        event = {"kind": "answer", "text": text}
        try:
            sys.stdout.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            sys.stdout.flush()
        except BrokenPipeError:
            raise

    def _emit_tool_result(self, call: ToolCall, result: str) -> None:
        self._ensure_fresh_line()
        self.emit_event("tool_result", self._short_result(result, limit=500), name=call.name)

    def _audit_unparsed_tool_call(self, raw: str | None = None) -> bool:
        if raw is None:
            raw = "".join(self._raw_answer_parts)
        lowered = raw.lower()
        candidate = (
            "<tool_call" in lowered
            or "｜｜dsml" in lowered
            or "invoke name=" in lowered
            or ('"name":' in raw and '"arguments":' in raw)
        )
        if not candidate:
            return False
        sys.stderr.write(
            "[parser] unparsed tool-call candidate: " + raw[:500].replace("\n", "\\n") + "\n"
        )
        return True

    def _maybe_compact_before_turn(self) -> None:
        if not self.conversation_id:
            return
        if not self.compaction.should_compact(self.context_chars):
            return
        self._run_compaction()

    def _run_compaction(self) -> bool:
        assert self.client is not None
        try:
            result = self.compaction.compact(
                self.client,
                self.conversation_id,
                plan_enabled=self.plan_enabled,
            )
        except Exception as exc:
            self.emit_event("error", message=f"Compaction failed: {type(exc).__name__}: {exc}")
            return False
        if result is None:
            return False
        self.conversation_id = result.new_conversation_id
        self.runtime.conversation_id = result.new_conversation_id
        self._pending_compaction_prefix = result.prefix
        self.context_chars = len(result.prefix)
        self._plan_injected_cid = None
        self._render_system(
            "Context compacted. New thread: "
            f"[bold]{result.new_conversation_id}[/]. Previous summary saved to "
            f"[bold]{result.summary_path}[/]."
        )
        self.emit_event(
            "compaction",
            old_cid=result.old_conversation_id,
            new_cid=result.new_conversation_id,
            summary_path=str(result.summary_path),
        )
        if result.warning:
            self._render_system(f"[yellow]{result.warning}[/]")
        return True

    def _prepare_prompt(self, prompt: str) -> str:
        if self._pending_compaction_prefix:
            self._summary_prefix_for_next_request = self._pending_compaction_prefix
            self._pending_compaction_prefix = None
            if not self.json_mode:
                self._render_system(
                    "[dim]Prepended previous-conversation summary to the new session.[/]"
                )
            else:
                sys.stderr.write("[compaction] summary prepended to new session\n")
        if self.conversation_id and self._plan_injected_cid != self.conversation_id:
            plan_prompt = self.plan_store.make_resume_prompt(self.conversation_id)
            if plan_prompt:
                prompt = plan_prompt + "\n\nUser: " + prompt
                self._plan_injected_cid = self.conversation_id
                if not self.json_mode:
                    self._render_system("[dim]Injected active plan from previous session.[/]")
        return prompt

    def _handle_part(self, kind: str, text: str) -> None:
        if kind == "answer":
            self._raw_answer_parts.append(text)
            self._response_processor.feed("answer", text)
            visible = self._answer_chunk_visible(text)
            if not visible:
                return
            self._answer_parts.append(visible)
            if self.stream:
                self.emit_event("answer", visible)
        elif kind == "thinking":
            self._thinking_parts.append(text)
            self._response_processor.feed("thinking", text)
            if self.stream:
                self.emit_event("thinking", text)
        elif kind == "tool_call":
            self._response_processor.feed("tool_call", text)
            name, arguments = self._parse_tool_call(text)
            self._pending_tool_names.append(name)
            self.emit_event("tool_call", name=name, arguments=arguments)
        elif kind == "tool_result":
            name = self._pending_tool_names.popleft() if self._pending_tool_names else "unknown"
            result = self._short_result(text, limit=500)
            self.emit_event("tool_result", result, name=name)
        else:
            self._render_system(f"[dim]{text}[/]")

    def _answer_chunk_visible(self, text: str) -> str:
        """Strip tool calls and <<DONE>> from a streamed answer chunk.

        A trailing fragment that could be the start of ``<<DONE>>`` is held
        back until more text arrives so a split marker never leaks out.
        Freeform patch blocks are suppressed the same way.
        """
        text = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL)
        text = self._patch_filter.feed(text)
        combined = self._answer_tail + text
        hold = 0
        for n in range(min(len(DONE_TOKEN) - 1, len(combined)), 0, -1):
            if combined.endswith(DONE_TOKEN[:n]):
                hold = n
                break
        if hold:
            self._answer_tail = combined[-hold:]
            combined = combined[:-hold]
        else:
            self._answer_tail = ""
        return combined.replace(DONE_TOKEN, "")

    def _flush_answer_tail(self) -> str:
        pending = self._patch_filter.flush()
        self._answer_tail, tail = "", pending + self._answer_tail
        return strip_done(tail) if tail else ""

    @staticmethod
    def _parse_tool_call(text: str) -> tuple[str, dict]:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return str(data.get("name", "unknown")), dict(data.get("arguments", {}))
        except Exception:
            pass
        return "unknown", {"raw": text}

    def _approval_callback(self, call: ToolCall) -> tuple[bool, bool]:
        if call.name in self.approved_tools:
            return True, False
        arg_text = json.dumps(call.arguments, ensure_ascii=False)
        prompt = (
            f"Approve {call.name}({arg_text})? "
            "[y]es / [n]o / [a]lways: "
        )
        if self.json_mode:
            sys.stderr.write(prompt)
            sys.stderr.flush()
            response = sys.stdin.readline().strip().lower()
        else:
            try:
                response = input(prompt).strip().lower()
            except EOFError:
                response = ""
        if response in ("y", "yes"):
            return True, False
        if response in ("a", "always"):
            self.approved_tools.add(call.name)
            return True, True
        return False, False

    def _prompt_approval(self, prompt: str) -> str:
        if self.json_mode:
            sys.stderr.write(prompt)
            sys.stderr.flush()
            return sys.stdin.readline().strip()
        try:
            return input(prompt).strip()
        except EOFError:
            return ""

    def _approve_tool_batch(self, calls: list[ToolCall]) -> list[bool]:
        if not calls:
            return []
        headers = []
        for idx, call in enumerate(calls, 1):
            args = json.dumps(call.arguments, ensure_ascii=False)
            headers.append(f"{idx}. {call.name} {args}")
        batch_text = "Pending tool calls:\n" + "\n".join(headers)
        if self.json_mode:
            sys.stderr.write(batch_text + "\n")
            sys.stderr.flush()
        else:
            self.ui.print(batch_text)

        metadata = {"update_plan", "request_user_input"}
        always_names: set[str] = set()
        approve_all = False
        reject_all = False
        decisions: list[bool] = []
        for call in calls:
            if call.name in metadata:
                decisions.append(True)
                continue
            if approve_all:
                decisions.append(True)
                continue
            if reject_all:
                decisions.append(False)
                continue
            if call.name in always_names:
                decisions.append(True)
                continue
            response = self._prompt_approval(
                f"Approve {call.name}? [y]es/[n]o/[a]lways/[A]ll/[q]uit: "
            )
            if response == "y":
                decisions.append(True)
            elif response == "n":
                decisions.append(False)
            elif response == "a":
                always_names.add(call.name)
                decisions.append(True)
            elif response == "A":
                approve_all = True
                decisions.append(True)
            elif response == "q":
                reject_all = True
                decisions.append(False)
            else:
                decisions.append(False)
        return decisions

    # ----- REPL and slash commands ----------------------------------------

    def run_repl(self) -> int:
        self.init_client()
        self.render_startup()
        try:
            while True:
                self.render_status()
                try:
                    if self.repl_session is None:
                        self.refresh_repl_session()
                    raw = self.repl_session.prompt("> ")
                except EOFError:
                    self.ui.print()
                    break
                except KeyboardInterrupt:
                    self.ui.print("\n[dim]Use /exit to quit.[/]")
                    continue
                line = raw.strip()
                if not line:
                    continue
                if line.startswith("/"):
                    code = self.handle_command(line)
                    if code is not None:
                        return code
                else:
                    code = self.run_prompt(line)
                    if code == EXIT_AUTH:
                        return code
                    if code not in (EXIT_OK, EXIT_INTERRUPTED):
                        self.emit_event("error", message=f"turn failed with exit code {code}")
        finally:
            _stop_active_indicators()
            if self.client is not None:
                self.client.close()
        return EXIT_OK

    def handle_command(self, line: str) -> int | None:
        cmd, _, arg = line.partition(" ")
        cmd = cmd.lower()
        arg = arg.strip()

        if cmd in ("/exit", "/quit"):
            _stop_active_indicators()
            return EXIT_OK
        if cmd in ("/help", "/?"):
            self.show_help()
        elif cmd == "/new":
            self.conversation_id = None
            self.approved_tools.clear()
            self.context_chars = 0
            self._pending_compaction_prefix = None
            self._plan_injected_cid = None
            self.runtime.conversation_id = None
            self._reload_memory()
            self._render_system("[green]Started a new thread.[/]")
        elif cmd == "/thread":
            self._render_system(
                f"conversation_id = [bold]{self.conversation_id or '(none yet)'}[/]"
            )
        elif cmd == "/clear":
            self.ui.clear()
            self.out.clear()
            self._render_system("[green]Display cleared.[/]")
        elif cmd == "/compact":
            if not self.conversation_id:
                self._render_system("[yellow]No active conversation to compact.[/]")
            else:
                self._run_compaction()
                self._render_system(
                    "[green]Compaction complete.[/] The summary will prefix the next user message."
                )
        elif cmd == "/plan":
            self.command_plan(arg)
        elif cmd == "/agents":
            if self.agents_path:
                self._render_system(
                    f"AGENTS.md: [bold]{self.agents_path}[/] "
                    f"({len(self.agents_text.splitlines())} lines)"
                )
            else:
                self._render_system("[yellow]No AGENTS.md loaded.[/]")
        elif cmd == "/agents-reload":
            self.agents_path = None
            self.agents_text = ""
            self._load_context_layers()
            self._render_system("[green]AGENTS.md reloaded.[/]")
        elif cmd == "/agents-init":
            self.command_agents_init()
        elif cmd == "/model":
            self.command_model(arg)
        elif cmd == "/thinking":
            self.command_toggle("thinking", arg)
        elif cmd == "/search":
            self.command_toggle("search", arg)
        elif cmd == "/mode":
            self.command_mode(arg)
        elif cmd == "/tools":
            self.command_tools(arg)
        elif cmd == "/extensions":
            self.ui.print(extension_report())
        elif cmd == "/reload":
            reload_extensions()
            self.reload_tools()
            self.extension_commands = load_extension_commands()
            self.refresh_repl_session()
            self.configure_readline()
            self._render_system("[green]Extensions reloaded.[/]")
        elif cmd in self.extension_commands:
            self.run_extension_command(cmd, arg)
        else:
            self._render_system(f"[red]Unknown command {cmd}[/] — try /help")
        return None

    def show_help(self) -> None:
        table = Table(title="Slash commands", box=box.SIMPLE)
        table.add_column("Command", style="bold cyan")
        table.add_column("Effect")
        for cmd, desc in BUILTIN_SLASH_COMMANDS.items():
            table.add_row(cmd, desc)
        for cmd in sorted(self.extension_commands):
            table.add_row(cmd, "extension command")
        self.ui.print(table)
        self.ui.print(
            "[dim]Flags are available on startup: --model, --no-thinking, --no-search, "
            "--tools, --show-thinking, --json, --resume, --no-stream, "
            "--legacy-tools, --compact-at, --plan-mode, --mode.[/]"
        )

    def command_model(self, arg: str) -> None:
        if not arg:
            self._render_system(
                f"model = [bold]{self.model}[/] (aliases: chat, expert)"
            )
            return
        if arg not in MODEL_CHOICES:
            self._render_system("[red]Usage: /model chat | expert[/]")
            return
        self.model_alias = arg
        self.model = MODEL_CHOICES[arg][0]
        self.wire_model_first = MODEL_CHOICES[arg][1]
        suffix = (
            " [dim](current thread keeps its original model; use /new to switch)[/]"
            if self.conversation_id
            else ""
        )
        self._render_system(f"Model → [bold]{self.model}[/]{suffix}")

    def command_toggle(self, attr: str, arg: str) -> None:
        if arg in ("on", "off"):
            value = arg == "on"
        elif arg:
            self._render_system(f"[red]Usage: /{attr} on | off[/]")
            return
        else:
            value = not getattr(self, attr)
        setattr(self, attr, value)
        label = "DeepThink" if attr == "thinking" else "Web search"
        self._render_system(f"{label} → [bold]{'on' if value else 'off'}[/]")

    def command_mode(self, arg: str) -> None:
        if arg in ("manual", "auto"):
            self.tools_mode = arg
            self.autonomous = bool(arg == "auto" or self.mode == "agent")
            self.plan_enabled = bool(self.autonomous or self.plan_mode)
            self.reload_tools()
            self._render_system(f"Tool mode → [bold]{arg}[/]")
        elif not arg:
            self._render_system(f"tool mode = [bold]{self.tools_mode}[/]")
        else:
            self._render_system("[red]Usage: /mode manual | auto[/]")

    def command_plan(self, arg: str) -> None:
        cid = self.conversation_id
        if arg == "clear":
            removed = self.plan_store.clear(cid)
            self._render_system(
                "[green]Plan cleared.[/]" if removed else "[yellow]No plan to clear.[/]"
            )
            return
        if arg == "resume":
            data = self.plan_store.load(cid)
            if not data or not data.get("plan"):
                self._render_system("[yellow]No plan found.[/]")
                return
            plan = list(data["plan"])
            if not any(item.get("status") == "in_progress" for item in plan):
                for item in plan:
                    if item.get("status") == "pending":
                        item["status"] = "in_progress"
                        break
                self.plan_store.save(cid, plan, explanation="/plan resume")
            self._plan_injected_cid = None
            self._render_plan(plan)
            self._plan_injected_cid = None
            return
        if arg:
            self._render_system("[red]Usage: /plan [clear|resume][/]")
            return
        data = self.plan_store.load(cid)
        if not data or not data.get("plan"):
            self._render_system("[yellow]No active plan.[/]")
            return
        self._render_plan(data["plan"])

    def command_agents_init(self) -> None:
        target = Path.cwd() / "AGENTS.md"
        draft = generate_draft(Path.cwd())
        self.ui.print(Panel(draft, title="AGENTS.md draft", box=box.SQUARE))
        response = self._prompt_approval(f"Write {target}? [y/N]: ").lower()
        if response not in {"y", "yes"}:
            self._render_system("[yellow]AGENTS.md generation cancelled.[/]")
            return
        target.write_text(draft, encoding="utf-8")
        self.agents_path = target
        self.agents_text = draft
        self._render_system(f"[green]Wrote {target}[/]")

    def command_tools(self, arg: str) -> None:
        if arg in ("off", "manual", "auto"):
            self.tools_mode = arg
            self.autonomous = bool(arg == "auto" or self.mode == "agent")
            self.plan_enabled = bool(self.autonomous or self.plan_mode)
            self.reload_tools()
            self._render_system(f"Tools → [bold]{arg}[/]")
        elif arg == "list" or not arg:
            table = Table(title="Registered tools", box=box.SIMPLE)
            table.add_column("Tool", style="bold cyan")
            table.add_column("Tier")
            table.add_column("Description")
            for tool_obj in self.registry.eager_tools:
                tier = "eager"
                table.add_row(tool_obj.name, tier, tool_obj.description or "")
            for tool_obj in self.registry.deferred_tools:
                table.add_row(tool_obj.name, "deferred", tool_obj.description or "")
            self.ui.print(table)
        else:
            self._render_system("[red]Usage: /tools off | manual | auto | list[/]")

    def run_extension_command(self, cmd: str, arg: str) -> None:
        fn = self.extension_commands[cmd]
        stdout = io.StringIO()
        result: Any = None
        try:
            with contextlib.redirect_stdout(stdout):
                sig = inspect.signature(fn)
                params = list(sig.parameters.values())
                if any(p.kind is p.VAR_POSITIONAL for p in params):
                    result = fn(self)
                else:
                    positional = [
                        p for p in params
                        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                    ]
                    required = [p for p in positional if p.default is p.empty]
                    if positional and (required or len(positional) == 1):
                        first_name = positional[0].name
                        context = (
                            self.emit
                            if first_name in {"emit", "output", "write", "console"}
                            else self
                        )
                        result = fn(context)
                    else:
                        result = fn()
        except Exception as e:
            self._render_system(f"[red]{cmd} failed:[/] {type(e).__name__}: {e}")
            return
        captured = stdout.getvalue().strip()
        if captured:
            self._render_system(captured)
        if result is not None:
            self._render_system(str(result))
        if not captured and result is None:
            self._render_system(f"[dim]{cmd} completed.[/]")


def read_prompt(args: argparse.Namespace, parser: UsageErrorParser) -> tuple[bool, str | None]:
    """Return (interactive, prompt). Raise parser.error on invalid combinations."""
    if args.resume and args.model:
        parser.error("--model cannot be combined with --resume; a thread's model is fixed")
    if args.json and args.prompt is None and sys.stdin.isatty():
        parser.error("--json requires a prompt or piped stdin")
    if args.show_thinking and not args.thinking:
        # This is legal, but almost always a mistake. Keep it non-fatal and
        # explicit instead of silently enabling DeepThink.
        sys.stderr.write("[cli] note: --show-thinking has no effect with --no-thinking\n")

    if args.prompt is not None:
        return False, args.prompt
    if not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
        if not prompt:
            parser.error("no prompt provided on stdin")
        return False, prompt
    return True, None


def _redirect_stdout_to_devnull() -> None:
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.close(devnull)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    argv_list = list(sys.argv[1:] if argv is None else argv)
    if "--json" in argv_list and ("--help" in argv_list or "-h" in argv_list):
        parser = build_parser()
        text = parser.format_help()
        try:
            sys.stdout.write(json.dumps({"kind": "help", "text": text}) + "\n")
            sys.stdout.flush()
            return EXIT_OK
        except BrokenPipeError:
            _redirect_stdout_to_devnull()
            return EXIT_SIGPIPE
    parser = build_parser()
    parser.json_errors = "--json" in argv_list
    args = parser.parse_args(argv_list)
    if args.show_preamble:
        app = DeepSeekCLI(args, parser, interactive=False, prompt=None)
        app.reload_tools()
        app._load_context_layers()
        schema = json.dumps([t.schema() for t in app.registry.visible_tools()], indent=2)
        text = TOOL_SYSTEM_PREAMBLE.format(tools_schema=schema)
        sections = app._context_sections()
        if sections:
            text += "\n\n" + "\n\n".join(sections)
        sys.stderr.write(f"Compact at: {app.compaction.threshold:,} tokens\n")
        sys.stderr.write(text + "\n")
        return EXIT_OK
    if args.generate_agents:
        app = DeepSeekCLI(args, parser, interactive=False, prompt=None)
        app.command_agents_init()
        return EXIT_OK
    interactive, prompt = read_prompt(args, parser)
    app = DeepSeekCLI(args, parser, interactive=interactive, prompt=prompt)
    try:
        if interactive:
            return app.run_repl()
        assert prompt is not None
        return app.run_prompt(prompt)
    except KeyboardInterrupt:
        app.emit_event("error", message="interrupted")
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        _redirect_stdout_to_devnull()
        return EXIT_SIGPIPE
    finally:
        if app.client is not None:
            app.client.close()


if __name__ == "__main__":
    try:
        _exit_code = main()
    except BrokenPipeError:
        _redirect_stdout_to_devnull()
        _exit_code = EXIT_SIGPIPE
    raise SystemExit(_exit_code)
