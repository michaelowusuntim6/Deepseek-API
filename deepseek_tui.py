#!/usr/bin/env python3
"""
DeepSeek TUI — Claude Code / Codex-style terminal UI for DeepSeek.

Talks to your local `deepseek` package directly. Streaming, multi-turn threads,
DeepThink reasoning (in a collapsible block), web search, and tool calling.

Install once:  pip install textual
Run:           python deepseek_tui.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import (
    Collapsible, Header, Input, Markdown, OptionList, Static,
)
from textual.widgets.option_list import Option

from deepseek import DeepSeekClient, tool, Tool, ToolCall
from deepseek.auth import LoginRequired


# ----- Built-in Tools -----

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
            output = output_bytes[:max_bytes].decode("utf-8", errors="replace") + "\n[Output truncated at 8KB]"
        return output
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 30 seconds."
    except Exception as e:
        return f"Error running shell command: {type(e).__name__}: {e}"


def default_tools() -> list[Tool]:
    return [read_file, list_dir, write_file, run_shell]


SLASH_COMMANDS = [
    ("/help",     "show commands and key bindings"),
    ("/model",    "switch model — /model chat  or  /model expert"),
    ("/thinking", "toggle DeepThink reasoning"),
    ("/search",   "toggle web search"),
    ("/mode",     "set tool mode — /mode manual  or  /mode auto"),
    ("/tools",    "toggle/list tools — /tools on | off | list"),
    ("/new",      "start a fresh thread"),
    ("/clear",    "clear the chat pane"),
    ("/thread",   "print the current conversation_id"),
    ("/exit",     "quit"),
]


# ---------------- Widgets ----------------

class UserMessage(Static):
    def __init__(self, text: str):
        super().__init__(f"[bold cyan]You[/]\n{text}")
        self.add_class("user-msg")


class AssistantMessage(Markdown):
    def __init__(self):
        super().__init__("")
        self.add_class("assistant-msg")
        self._buffer = ""

    def append(self, chunk: str) -> None:
        self._buffer += chunk
        self.update(self._buffer)


class ThinkingBlock(Collapsible):
    def __init__(self):
        body = Static("", classes="thinking-body", markup=False)
        super().__init__(body, title="💭 Thinking…", collapsed=False)
        self.add_class("thinking-block")
        self._body = body
        self._buffer = ""
        self.finished = False

    def append(self, text: str) -> None:
        self._buffer += text
        self._body.update(self._buffer)

    def finish(self) -> None:
        self.finished = True
        n = len(self._buffer)
        self.title = f"💭 Thinking  ·  {n:,} chars  ·  click to expand"
        self.collapsed = True


class ToolCallBlock(Static):
    """Widget rendering a requested tool call and approval prompt."""
    def __init__(self, call: ToolCall, mode: str):
        short_args = str(call.arguments)
        if len(short_args) > 100:
            short_args = short_args[:97] + "..."
        super().__init__(f"⚙ Tool call: [bold]{call.name}[/]({short_args})")
        self.add_class("tool-call-block")


class ToolResultBlock(Static):
    """Widget rendering the result of a tool call."""
    def __init__(self, result: str, is_error: bool = False):
        short_res = result.strip().replace("\n", " ")
        if len(short_res) > 200:
            short_res = short_res[:197] + "…"
        prefix = "✗" if is_error else "✓"
        style = "red" if is_error else "dim"
        super().__init__(f"[{style}]{prefix} Result: {short_res}[/]")
        self.add_class("tool-result-block")


class StatusBar(Static):
    model = reactive("deepseek-chat")
    thinking = reactive(False)
    search = reactive(False)
    tool_mode = reactive("manual")
    tools_enabled = reactive(True)
    conversation_id = reactive(None)

    def render(self) -> str:
        cid = self.conversation_id
        short = (cid.split(":")[0][:8] + "…") if cid else "(new)"
        th = "on" if self.thinking else "off"
        sr = "on" if self.search else "off"
        tl = "on" if self.tools_enabled else "off"
        mode_str = f"[bold red]{self.tool_mode}[/]" if self.tool_mode == "auto" else f"[bold]{self.tool_mode}[/]"
        return (
            f" [bold]{self.model}[/] · "
            f"think [bold]{th}[/] · "
            f"search [bold]{sr}[/] · "
            f"tools [bold]{tl}[/] ({mode_str}) · "
            f"thread [dim]{short}[/]   "
            f"[dim]ctrl+Y mode · ctrl+T think · ctrl+S search · /help[/]"
        )


class SlashMenu(OptionList):
    can_focus = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_class("slash-menu")
        self.display = False
        for cmd, desc in SLASH_COMMANDS:
            self.add_option(Option(f"[bold]{cmd}[/]   [dim]{desc}[/]", id=cmd))

    def filter_to(self, prefix: str) -> None:
        self.clear_options()
        needle = prefix.lower()
        for cmd, desc in SLASH_COMMANDS:
            if cmd.startswith(needle):
                self.add_option(Option(f"[bold]{cmd}[/]   [dim]{desc}[/]", id=cmd))
        self.highlighted = 0 if self.option_count else None

    @property
    def highlighted_command(self) -> str | None:
        idx = self.highlighted
        if idx is None:
            return None
        try:
            return self.get_option_at_index(idx).id
        except Exception:
            return None


# ---------------- App ----------------

class DeepSeekTUI(App):
    CSS = """
    Screen { background: $surface; }

    #chat {
        height: 1fr;
        padding: 1 2;
    }

    .user-msg {
        background: $boost;
        border-left: thick $accent;
        padding: 0 1;
        margin: 1 0 0 0;
    }

    .assistant-msg {
        padding: 0 1;
        margin: 0 0 1 0;
    }

    .system-msg {
        color: $text-muted;
        padding: 0 1;
        margin: 0 0 1 0;
    }

    .thinking-block {
        margin: 0 0 1 0;
        border-left: thick $warning;
        background: $boost;
    }

    .thinking-body {
        color: $text-muted;
        padding: 0 1;
    }

    .tool-call-block {
        margin: 0 0 1 0;
        border-left: thick $accent;
        background: $panel;
        padding: 0 1;
    }

    .tool-result-block {
        margin: 0 0 1 0;
        padding: 0 1;
    }

    .slash-menu {
        display: none;
        height: auto;
        max-height: 10;
        margin: 0 2;
        border: tall $accent;
        background: $panel;
    }

    #input {
        margin: 0 2;
        border: tall $accent;
    }

    StatusBar {
        height: 1;
        background: $panel;
        color: $text;
        padding: 0 2;
    }
    """

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+l", "clear_chat", "Clear"),
        ("ctrl+n", "new_thread", "New thread"),
        ("ctrl+t", "toggle_thinking", "DeepThink"),
        ("ctrl+s", "toggle_search", "Search"),
        ("ctrl+m", "cycle_model", "Model"),
        ("ctrl+y", "toggle_tool_mode", "Tool Mode"),
    ]

    def __init__(self):
        super().__init__()
        self.client: DeepSeekClient | None = None
        self.conversation_id: str | None = None
        self.model: str = "deepseek-chat"
        self.thinking: bool = False
        self.search: bool = False
        self.tool_mode: str = "manual"
        self.tools_enabled: bool = True
        self.tools: list[Tool] = default_tools()

        self._pending_approval: dict | None = None
        self._approved_tools: set[str] = set()

        self._streaming = False
        self._thinking_block: ThinkingBlock | None = None
        self._answer_md: AssistantMessage | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield VerticalScroll(id="chat")
        yield SlashMenu(id="slash-menu")
        yield Input(placeholder="Ask DeepSeek…   (type / for commands)", id="input")
        yield StatusBar(id="status")

    def on_mount(self) -> None:
        self.title = "DeepSeek TUI"
        self.sub_title = "streaming · DeepThink · web search · tool calling"
        self._update_status()
        self.query_one("#input", Input).focus()
        self._init_client()

    # ----- session bootstrap -----

    @work(thread=True, exclusive=True)
    def _init_client(self) -> None:
        self.call_from_thread(self._add_system, "[dim]Loading DeepSeek session…[/]")
        try:
            self.client = DeepSeekClient(allow_interactive=True)
            self.call_from_thread(
                self._add_system,
                "[green]Session ready.[/] Type a message, or / for commands.",
            )
        except LoginRequired as e:
            self.call_from_thread(self._add_system, f"[red]Login required:[/] {e}")
        except Exception as e:
            self.call_from_thread(self._add_system,
                                  f"[red]Failed to start session:[/] {e}")

    # ----- helpers -----

    def _chat(self) -> VerticalScroll:
        return self.query_one("#chat", VerticalScroll)

    def _update_status(self) -> None:
        bar = self.query_one(StatusBar)
        bar.model = self.model
        bar.thinking = self.thinking
        bar.search = self.search
        bar.tool_mode = self.tool_mode
        bar.tools_enabled = self.tools_enabled
        bar.conversation_id = self.conversation_id

    def _add_system(self, text: str) -> None:
        self._chat().mount(Static(text, classes="system-msg"))
        self._chat().scroll_end(animate=False)

    def _add_user(self, text: str) -> None:
        self._chat().mount(UserMessage(text))
        self._chat().scroll_end(animate=False)

    # ----- slash menu -----

    def _show_slash_menu(self, prefix: str) -> None:
        menu = self.query_one(SlashMenu)
        menu.filter_to(prefix)
        if menu.option_count == 0:
            self._hide_slash_menu()
            return
        menu.display = True

    def _hide_slash_menu(self) -> None:
        self.query_one(SlashMenu).display = False

    def on_input_changed(self, event: Input.Changed) -> None:
        v = event.value
        if v.startswith("/") and " " not in v:
            self._show_slash_menu(v)
        else:
            self._hide_slash_menu()

    def on_key(self, event: events.Key) -> None:
        if self._pending_approval is not None:
            key = event.key
            if key == "y":
                self._resolve_approval("yes")
            elif key == "n":
                self._resolve_approval("no")
            elif key == "a":
                self._resolve_approval("always")
            elif key == "escape":
                self._resolve_approval("no")
            event.stop()
            event.prevent_default()
            return

        menu = self.query_one(SlashMenu)
        if menu.display:
            if event.key == "down":
                menu.action_cursor_down()
                event.stop(); event.prevent_default()
                return
            elif event.key == "up":
                menu.action_cursor_up()
                event.stop(); event.prevent_default()
                return
            elif event.key in ("tab", "right"):
                cmd = menu.highlighted_command
                if cmd:
                    inp = self.query_one("#input", Input)
                    inp.value = cmd
                    inp.cursor_position = len(cmd)
                    self._hide_slash_menu()
                event.stop(); event.prevent_default()
                return
            elif event.key == "enter":
                cmd = menu.highlighted_command
                if cmd:
                    self.query_one("#input", Input).value = ""
                    self._hide_slash_menu()
                    self._handle_command(cmd)
                event.stop(); event.prevent_default()
                return
            elif event.key == "escape":
                self._hide_slash_menu()
                event.stop(); event.prevent_default()
                return

    def on_unmount(self) -> None:
        pa = self._pending_approval
        if pa is not None:
            pa["slot"]["result"] = "no"
            pa["event"].set()

    # ----- approval plumbing -----

    def _request_approval(self, call: ToolCall) -> str:
        """Called from the WORKER thread. Blocks until the UI resolves it."""
        if call.name in self._approved_tools:
            return "always"

        ev = threading.Event()
        slot = {"result": None}
        self._pending_approval = {"event": ev, "slot": slot, "call": call}

        self.call_from_thread(self._enter_approval_ui, call)

        got = ev.wait(timeout=300)

        result = slot["result"] or "no"
        self.call_from_thread(self._exit_approval_ui, result, got)
        self._pending_approval = None
        return result

    def _resolve_approval(self, result: str) -> None:
        """Called from the UI thread (on_key). Signals the worker."""
        pa = self._pending_approval
        if pa is None:
            return
        pa["slot"]["result"] = result
        if result == "always":
            self._approved_tools.add(pa["call"].name)
        pa["event"].set()

    def _enter_approval_ui(self, call: ToolCall) -> None:
        """Runs on the UI thread. Shows the prompt and routes keys."""
        args_short = str(call.arguments)
        if len(args_short) > 120:
            args_short = args_short[:117] + "..."
        self._add_system(
            f"[bold yellow]⚙ Tool call:[/] [cyan]{call.name}[/]  "
            f"[dim]{args_short}[/]\n"
            f"[bold yellow]Approve?[/] "
            f"[bold]y[/]=yes  [bold]n[/]=no  [bold]a[/]=always run this tool  "
            f"[bold]esc[/]=no"
        )
        inp = self.query_one("#input", Input)
        inp.disabled = True
        self.set_focus(None)

    def _exit_approval_ui(self, result: str, got: bool) -> None:
        """Runs on the UI thread after the decision is resolved."""
        inp = self.query_one("#input", Input)
        inp.disabled = False
        inp.focus()
        if not got:
            self._add_system("[red]✗ approval timed out — treating as 'no'[/]")
            return
        label = {
            "yes":    "[green]✓ approved[/]",
            "no":     "[red]✗ rejected[/]",
            "always": "[green]✓ approved (always for this tool)[/]",
        }.get(result, result)
        self._add_system(f"  {label}")

    def _approval_callback(self, call: ToolCall) -> bool:
        """Called from the worker thread by chat_with_tools / stream_with_tools."""
        decision = self._request_approval(call)
        return decision in ("yes", "always")

    # ----- submit -----

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        self._hide_slash_menu()
        if not text:
            return
        if self._streaming:
            self._add_system("[yellow]Still streaming — please wait.[/]")
            return
        if text.startswith("/"):
            self._handle_command(text)
            return
        self._send(text)

    # ----- slash commands -----

    def _handle_command(self, text: str) -> None:
        parts = text[1:].split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("help", "?"):
            self._add_system(
                "[bold]Commands[/]\n"
                "  [cyan]/model chat|expert[/]   pick the answering model\n"
                "  [cyan]/thinking[/]            toggle DeepThink reasoning\n"
                "  [cyan]/search[/]              toggle web search\n"
                "  [cyan]/mode manual|auto[/]    set tool call approval mode\n"
                "  [cyan]/tools on|off|list[/]   toggle or list registered tools\n"
                "  [cyan]/new[/]                 start a fresh thread\n"
                "  [cyan]/clear[/]               clear the chat pane\n"
                "  [cyan]/thread[/]              print current conversation_id\n"
                "  [cyan]/exit[/]                quit\n"
                "\n[bold]Keys[/]  ctrl+y mode · ctrl+t think · ctrl+s search · "
                "ctrl+m model · ctrl+n new · ctrl+l clear · ctrl+c quit"
            )
        elif cmd == "model":
            if arg in ("chat", "deepseek-chat", "default"):
                self.model = "deepseek-chat"
            elif arg in ("expert", "deepseek-expert"):
                self.model = "deepseek-expert"
            elif arg == "":
                self._cycle_model()
                return
            else:
                self._add_system("[red]Usage: /model chat  or  /model expert[/]")
                return
            self._update_status()
            self._add_system(f"Model → [bold]{self.model}[/]")
        elif cmd == "thinking":
            self.thinking = not self.thinking
            self._update_status()
            self._add_system(f"DeepThink → [bold]{'on' if self.thinking else 'off'}[/]")
        elif cmd == "search":
            self.search = not self.search
            self._update_status()
            self._add_system(f"Web search → [bold]{'on' if self.search else 'off'}[/]")
        elif cmd == "mode":
            if arg == "auto":
                self.tool_mode = "auto"
            elif arg == "manual":
                self.tool_mode = "manual"
            elif arg == "":
                self._add_system(f"Tool mode is currently: [bold]{self.tool_mode}[/]")
                return
            else:
                self._add_system("[red]Usage: /mode manual  or  /mode auto[/]")
                return
            self._update_status()
            self._add_system(f"Tool mode → [bold]{self.tool_mode}[/]")
        elif cmd == "tools":
            if arg == "on":
                self.tools_enabled = True
                self._add_system("Tools → [bold]on[/]")
            elif arg == "off":
                self.tools_enabled = False
                self._add_system("Tools → [bold]off[/]")
            elif arg == "list":
                tool_lines = [f"  [bold cyan]{t.name}[/]: {t.description}" for t in self.tools]
                self._add_system("[bold]Registered Tools:[/]\n" + "\n".join(tool_lines))
            else:
                self._add_system("[red]Usage: /tools on | off | list[/]")
                return
            self._update_status()
        elif cmd == "new":
            self.conversation_id = None
            self._approved_tools.clear()
            self._update_status()
            self._add_system("[green]Started a new thread.[/]")
        elif cmd == "clear":
            self.action_clear_chat()
        elif cmd == "thread":
            self._add_system(
                f"conversation_id = [bold]{self.conversation_id or '(none yet)'}[/]"
            )
        elif cmd in ("exit", "quit"):
            self.exit()
        else:
            self._add_system(f"[red]Unknown command /{cmd}[/]  — try /help")

    def _cycle_model(self) -> None:
        self.model = (
            "deepseek-expert" if self.model == "deepseek-chat" else "deepseek-chat"
        )
        self._update_status()
        self._add_system(f"Model → [bold]{self.model}[/]")

    # ----- send + stream -----

    def _send(self, prompt: str) -> None:
        if self.client is None:
            self._add_system("[yellow]Session not ready yet — hang on a second.[/]")
            return
        self._add_user(prompt)
        self._thinking_block = None
        self._answer_md = None
        self._streaming = True
        self._run_stream(prompt)

    @work(thread=True, exclusive=True)
    def _run_stream(self, prompt: str) -> None:
        assert self.client is not None
        try:
            wire_model = None
            if self.conversation_id is None:
                wire_model = "expert" if self.model == "deepseek-expert" else "default"

            if self.tools_enabled:
                appr_arg = "auto" if self.tool_mode == "auto" else self._approval_callback
                stream_gen = self.client.stream_with_tools(
                    prompt,
                    tools=self.tools,
                    approval=appr_arg,
                    conversation_id=self.conversation_id,
                    model=wire_model,
                    thinking=self.thinking,
                    search=self.search,
                )
                for kind, text in stream_gen:
                    if text:
                        self.call_from_thread(self._on_part, kind, text)
                cid = getattr(self.client, "_last_stream_cid", self.conversation_id)
                self.call_from_thread(self._on_stream_done, cid, None)
            else:
                stream = self.client.stream(
                    prompt,
                    conversation_id=self.conversation_id,
                    model=wire_model,
                    thinking=self.thinking,
                    search=self.search,
                )
                for kind, chunk in stream.iter_parts():
                    if chunk:
                        self.call_from_thread(self._on_part, kind, chunk)
                self.call_from_thread(self._on_stream_done, stream.conversation_id, None)
        except Exception as e:
            self.call_from_thread(self._on_stream_done, None, str(e))

    def _on_part(self, kind: str, chunk: str) -> None:
        if kind == "thinking":
            if self._thinking_block is None:
                self._thinking_block = ThinkingBlock()
                self._chat().mount(self._thinking_block)
            self._thinking_block.append(chunk)
        elif kind == "tool_call":
            if self._thinking_block is not None and not self._thinking_block.finished:
                self._thinking_block.finish()
            # If parsed in stream_with_tools, note it's handled via callback
        elif kind == "tool_result":
            if self._thinking_block is not None and not self._thinking_block.finished:
                self._thinking_block.finish()
            is_err = chunk.startswith("Error") or "rejected" in chunk
            self._chat().mount(ToolResultBlock(chunk, is_error=is_err))
            # Reset answer block so subsequent narration gets a fresh Markdown bubble
            self._answer_md = None
        else:  # answer
            if self._thinking_block is not None and not self._thinking_block.finished:
                self._thinking_block.finish()
            if self._answer_md is None:
                self._answer_md = AssistantMessage()
                self._chat().mount(self._answer_md)
            self._answer_md.append(chunk)
        self._chat().scroll_end(animate=False)

    def _on_stream_done(self, conversation_id: str | None, error: str | None) -> None:
        self._streaming = False
        if self._thinking_block is not None and not self._thinking_block.finished:
            self._thinking_block.finish()
        if error:
            self._add_system(f"[red]Error:[/] {error}")
        elif conversation_id:
            self.conversation_id = conversation_id
            self._update_status()
        self._thinking_block = None
        self._answer_md = None

    # ----- key actions -----

    def action_clear_chat(self) -> None:
        self._chat().remove_children()
        self._add_system("[green]Chat cleared.[/]")

    def action_new_thread(self) -> None:
        self.conversation_id = None
        self.always_approved_tools.clear()
        self._update_status()
        self._add_system("[green]Started a new thread.[/]")

    def action_toggle_thinking(self) -> None:
        self.thinking = not self.thinking
        self._update_status()
        self._add_system(f"DeepThink → [bold]{'on' if self.thinking else 'off'}[/]")

    def action_toggle_search(self) -> None:
        self.search = not self.search
        self._update_status()
        self._add_system(f"Web search → [bold]{'on' if self.search else 'off'}[/]")

    def action_cycle_model(self) -> None:
        self._cycle_model()

    def action_toggle_tool_mode(self) -> None:
        self.tool_mode = "auto" if self.tool_mode == "manual" else "manual"
        self._update_status()
        self._add_system(f"Tool mode → [bold]{self.tool_mode}[/]")


def main() -> None:
    DeepSeekTUI().run()


if __name__ == "__main__":
    main()
