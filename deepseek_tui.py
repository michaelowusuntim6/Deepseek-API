#!/usr/bin/env python3
"""
DeepSeek TUI — Claude Code / Codex-style terminal UI for DeepSeek.

Talks to your local `deepseek` package directly. Streaming, multi-turn threads,
DeepThink reasoning (in a collapsible block), and web search.

Install once:  pip install textual
Run:           python deepseek_tui.py
"""

from __future__ import annotations

import sys
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

from deepseek import DeepSeekClient
from deepseek.auth import LoginRequired


SLASH_COMMANDS = [
    ("/help",     "show commands and key bindings"),
    ("/model",    "switch model — /model chat  or  /model expert"),
    ("/thinking", "toggle DeepThink reasoning"),
    ("/search",   "toggle web search"),
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
    """Reasoning trace in a collapsible block. Auto-collapses when the answer
    begins.

    IMPORTANT: We hold a direct reference to the body Static rather than using
    query_one(Static) — Collapsible's internal Title widget is itself a Static
    subclass, so query_one(Static) returns the *title*, not our body, and any
    update() call lands on the wrong widget.
    """

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


class StatusBar(Static):
    model = reactive("deepseek-chat")
    thinking = reactive(False)
    search = reactive(False)
    conversation_id = reactive(None)

    def render(self) -> str:
        cid = self.conversation_id
        short = (cid.split(":")[0][:8] + "…") if cid else "(new)"
        th = "on" if self.thinking else "off"
        sr = "on" if self.search else "off"
        return (
            f" [bold]{self.model}[/]  ·  "
            f"think [bold]{th}[/]  ·  "
            f"search [bold]{sr}[/]  ·  "
            f"thread [dim]{short}[/]     "
            f"[dim]ctrl+T think · ctrl+S search · ctrl+M model · /help[/]"
        )


class SlashMenu(OptionList):
    """Popup list of slash commands. Appears when the input starts with '/'."""
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
    ]

    def __init__(self):
        super().__init__()
        self.client: DeepSeekClient | None = None
        self.conversation_id: str | None = None
        self.model: str = "deepseek-chat"
        self.thinking: bool = False
        self.search: bool = False
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
        self.sub_title = "streaming · DeepThink · web search"
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
        menu = self.query_one(SlashMenu)
        if not menu.display:
            return
        if event.key == "down":
            menu.action_cursor_down()
            event.stop(); event.prevent_default()
        elif event.key == "up":
            menu.action_cursor_up()
            event.stop(); event.prevent_default()
        elif event.key in ("tab", "right"):
            cmd = menu.highlighted_command
            if cmd:
                inp = self.query_one("#input", Input)
                inp.value = cmd
                inp.cursor_position = len(cmd)
                self._hide_slash_menu()
            event.stop(); event.prevent_default()
        elif event.key == "enter":
            cmd = menu.highlighted_command
            if cmd:
                self.query_one("#input", Input).value = ""
                self._hide_slash_menu()
                self._handle_command(cmd)
            event.stop(); event.prevent_default()
        elif event.key == "escape":
            self._hide_slash_menu()
            event.stop(); event.prevent_default()

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
                "  [cyan]/new[/]                 start a fresh thread\n"
                "  [cyan]/clear[/]               clear the chat pane\n"
                "  [cyan]/thread[/]              print the current conversation_id\n"
                "  [cyan]/exit[/]                quit\n"
                "\n[bold]Keys[/]  ctrl+t think · ctrl+s search · ctrl+m model · "
                "ctrl+n new · ctrl+l clear · ctrl+c quit"
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
        elif cmd == "new":
            self.conversation_id = None
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


def main() -> None:
    DeepSeekTUI().run()


if __name__ == "__main__":
    main()
