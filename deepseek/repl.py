"""prompt_toolkit REPL session with slash command completions."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory
from prompt_toolkit.document import Document
from prompt_toolkit.styles import Style
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import ThreadedCompleter
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import CompleteStyle


def claude_style() -> Style:
    return Style.from_dict({
        "completion-menu.completion": "bg:default fg:default",
        "completion-menu.completion.current": "bg:#3a3a3a fg:#ffffff",
        "completion-menu.meta.completion": "bg:default fg:#888888",
        "completion-menu.meta.completion.current": "bg:#3a3a3a fg:#bbbbbb",
    })


def _tier(query: str, name: str) -> int | None:
    q = query.lower()
    n = name.lower()
    if not q:
        return 0
    if q == n:
        return 0
    if n.startswith(q):
        return 1
    pos = 0
    for ch in q:
        pos = n.find(ch, pos)
        if pos < 0:
            return None
        pos += 1
    return 2


class SlashCommandCompleter(Completer):
    def __init__(self, commands: list[tuple[str, str]],
                 arguments: dict[str, list[tuple[str, str]]] | None = None):
        self.commands = list(commands)
        self.arguments = arguments or {}

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        self.commands = list(commands)

    def get_completions(self, document, complete_event) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        if " " not in text:
            query = text[1:]
            items = self.commands
            start_position = -(len(query) + 1)
            command_mode = True
        else:
            cmd, _, query = text.partition(" ")
            items = self.arguments.get(cmd, [])
            start_position = -len(query)
            command_mode = False
            if not items:
                return
        ranked = []
        for name, desc in items:
            tier = _tier(query, name)
            if tier is None:
                continue
            ranked.append((tier, len(name), name, desc))
        for _, _, name, desc in sorted(ranked):
            if command_mode:
                insert = name
                if name in {"/thinking", "/search"}:
                    insert += " "
            else:
                insert = name
            yield Completion(
                insert,
                start_position=start_position,
                display=name,
                display_meta=desc,
            )


def create_session(commands: list[tuple[str, str]],
                   arguments: dict[str, list[tuple[str, str]]]) -> PromptSession:
    completer = SlashCommandCompleter(commands, arguments)
    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        buf = event.current_buffer
        state = buf.complete_state
        if state and state.completions:
            index = state.complete_index if state.complete_index is not None else 0
            state.complete_index = (index - 1) % len(state.completions)
            event.app.invalidate()

    @kb.add("down")
    def _(event):
        buf = event.current_buffer
        state = buf.complete_state
        if state and state.completions:
            index = state.complete_index if state.complete_index is not None else -1
            state.complete_index = (index + 1) % len(state.completions)
            event.app.invalidate()

    @kb.add("escape")
    def _(event):
        buf = event.current_buffer
        if buf.complete_state:
            buf.cancel_completion()

    @kb.add("enter")
    def _(event):
        buf = event.current_buffer
        text = buf.text
        if text.startswith("/"):
            completions = list(completer.get_completions(Document(text, len(text)), None))
            if completions:
                index = 0
                if buf.complete_state and buf.complete_state.complete_index is not None:
                    index = min(buf.complete_state.complete_index, len(completions) - 1)
                completion = completions[index]
                replace_at = len(text) + completion.start_position
                new_text = text[:replace_at] + completion.text
                buf.text = new_text
                buf.cursor_position = len(new_text)
        buf.validate_and_handle()

    hist_path = Path.home() / ".deepseek-cli" / "repl_history"
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    session = PromptSession(
        completer=ThreadedCompleter(completer),
        key_bindings=kb,
        complete_while_typing=True,
        complete_style=CompleteStyle.COLUMN,
        history=FileHistory(str(hist_path)),
        style=claude_style(),
        reserve_space_for_menu=8,
    )
    session._deepseek_completer = completer  # type: ignore[attr-defined]
    return session
