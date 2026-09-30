"""
deepseek/extensions.py — plugin/extension loader for the DeepSeek CLI.

Drop .py files into:
  ~/.deepseek-tui/extensions/
  <cwd>/.deepseek-tui/extensions/

Each file may define:
  def register():
      return [tool_a, tool_b]   # one Tool or a list of Tools

  COMMANDS = {"/name": callable, ...}   # optional slash commands

Command callables may be zero-argument (stdout is captured by the CLI) or may
accept the CLI app instance as their first argument. A first argument named
`emit`/`output`/`write`/`console` receives an emit callback instead.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Callable

from .tools import Tool

# Built-in CLI slash commands that extensions must NOT override.
_BUILTIN_COMMANDS: frozenset[str] = frozenset({
    "/mode", "/tools", "/help", "/new", "/clear",
    "/thread", "/exit", "/extensions", "/reload",
    "/model", "/thinking", "/search",
})

# Module-name prefix to avoid collisions in sys.modules.
_MODULE_PREFIX = "dsx_ext_"

# Cached results — cleared by reload_extensions().
_cached_tools: list[Tool] | None = None
_cached_commands: dict[str, Callable] | None = None
_cached_context_providers: list[Callable] | None = None
_load_report: list[str] = []  # human-readable lines for extension_report()


def _search_paths() -> list[Path]:
    """Return extension search paths in precedence order (later overrides earlier)."""
    return [
        Path.home() / ".deepseek-tui" / "extensions",
        Path.cwd() / ".deepseek-tui" / "extensions",
    ]


def _collect_py_files() -> list[tuple[str, Path]]:
    """Return (label, path) pairs for all candidate extension files."""
    results: list[tuple[str, Path]] = []

    for search_dir in _search_paths():
        if not search_dir.is_dir():
            continue
        for py_file in sorted(search_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            name = py_file.stem
            results.append((name, py_file))

    return results


def _import_module(stem: str, path: Path):
    """Import a .py file as a module.  Returns the module object."""
    mod_name = _MODULE_PREFIX + stem
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create module spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod        # Register so re-import on /reload is clean
    spec.loader.exec_module(mod)       # type: ignore[union-attr]
    return mod


def _load_all() -> tuple[list[Tool], dict[str, Callable], list[Callable]]:
    """
    Internal: scan all extension paths, import, call register(), collect COMMANDS.
    Returns (tools, commands).  Errors are caught and printed to stderr.
    """
    global _load_report
    _load_report = []

    tools_by_name: dict[str, Tool] = {}
    commands: dict[str, Callable] = {}
    context_providers: list[Callable] = []

    for stem, path in _collect_py_files():
        label = path.name
        try:
            mod = _import_module(stem, path)
        except Exception as exc:
            msg = f"[ext] {label}: {type(exc).__name__}: {exc}"
            sys.stderr.write(msg + "\n")
            _load_report.append(f"[red]✗[/] {label}: import failed — {type(exc).__name__}: {exc}")
            continue

        # ----- register() -----
        register_fn = getattr(mod, "register", None)
        if register_fn is None:
            msg = f"[ext] {label}: no register() function — skipped"
            sys.stderr.write(msg + "\n")
            _load_report.append(f"[yellow]⚠[/] {label}: no register() — skipped")
            continue

        try:
            result = register_fn()
        except Exception as exc:
            msg = f"[ext] {label}: {type(exc).__name__}: {exc}"
            sys.stderr.write(msg + "\n")
            _load_report.append(f"[red]✗[/] {label}: register() failed — {type(exc).__name__}: {exc}")
            continue

        if result is None:
            _load_report.append(f"[yellow]⚠[/] {label}: register() returned None — skipped")
            continue

        # Normalise to list
        if isinstance(result, Tool):
            result = [result]
        elif not isinstance(result, (list, tuple)):
            result = list(result)

        loaded_names: list[str] = []
        for t in result:
            if not isinstance(t, Tool):
                sys.stderr.write(f"[ext] {label}: register() returned non-Tool object {t!r} — skipped\n")
                continue
            if t.name in tools_by_name:
                sys.stderr.write(
                    f"[ext] {label}: tool '{t.name}' already registered — later path wins\n"
                )
            tools_by_name[t.name] = t
            loaded_names.append(t.name)

        # ----- COMMANDS -----
        ext_commands = getattr(mod, "COMMANDS", None)
        if ext_commands and isinstance(ext_commands, dict):
            for cmd_key, cmd_fn in ext_commands.items():
                # Normalise: ensure leading slash
                if not cmd_key.startswith("/"):
                    cmd_key = "/" + cmd_key
                if cmd_key in _BUILTIN_COMMANDS:
                    sys.stderr.write(
                        f"[ext] {label}: slash command '{cmd_key}' clashes with a built-in — skipped\n"
                    )
                    continue
                commands[cmd_key] = cmd_fn

        provider = getattr(mod, "context_provider", None)
        if callable(provider):
            context_providers.append(provider)

        _load_report.append(
            f"[green]✓[/] [bold]{label}[/]: tools=[cyan]{', '.join(loaded_names) or '(none)'}[/]"
        )

    return list(tools_by_name.values()), commands, context_providers


# ── Public API ──────────────────────────────────────────────────────────────

def load_extensions() -> list[Tool]:
    """Discover, import, and return tools from every extension (cached)."""
    global _cached_tools, _cached_commands, _cached_context_providers
    if _cached_tools is None:
        _cached_tools, _cached_commands, _cached_context_providers = _load_all()
    return list(_cached_tools)


def load_extension_commands() -> dict[str, Callable]:
    """Return merged slash-command dict from all extensions (cached)."""
    global _cached_tools, _cached_commands, _cached_context_providers
    if _cached_commands is None:
        _cached_tools, _cached_commands, _cached_context_providers = _load_all()
    return dict(_cached_commands)


def load_context_providers() -> list[Callable]:
    """Return extension context providers for startup preamble injection."""
    global _cached_tools, _cached_commands, _cached_context_providers
    if _cached_context_providers is None:
        _cached_tools, _cached_commands, _cached_context_providers = _load_all()
    return list(_cached_context_providers)


def extension_report() -> str:
    """Return a Rich-markup string summarising loaded extensions."""
    # Ensure we've loaded at least once so _load_report is populated.
    load_extensions()

    paths = [str(p) for p in _search_paths()]
    lines = [
        "[bold]Extension Search Paths:[/]",
        *[f"  {p}" for p in paths],
        "",
        "[bold]Loaded Extensions:[/]",
    ]
    if _load_report:
        lines.extend(f"  {r}" for r in _load_report)
    else:
        lines.append("  (none found)")

    tools = load_extensions()
    if tools:
        lines.append("")
        lines.append("[bold]Extension Tools:[/]")
        for t in tools:
            lines.append(f"  [bold cyan]{t.name}[/]: {t.description}")

    cmds = load_extension_commands()
    if cmds:
        lines.append("")
        lines.append("[bold]Extension Commands:[/]")
        for k in sorted(cmds):
            lines.append(f"  [bold]{k}[/]")

    return "\n".join(lines)


def reload_extensions() -> None:
    """Clear cached state; the next load_* call re-scans the disk."""
    global _cached_tools, _cached_commands, _cached_context_providers, _load_report

    # Remove previously imported extension modules from sys.modules
    to_remove = [k for k in sys.modules if k.startswith(_MODULE_PREFIX)]
    for k in to_remove:
        del sys.modules[k]

    _cached_tools = None
    _cached_commands = None
    _cached_context_providers = None
    _load_report = []
