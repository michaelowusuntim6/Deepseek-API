"""
notes.py — DeepSeek CLI extension: simple markdown note storage.

Drop this file into ~/.deepseek-tui/extensions/ and restart (or /reload).

Notes are stored as Markdown files in $DEEPSEEK_NOTES_DIR (default: ~/notes).

Also demonstrates COMMANDS: adding /notes as a custom CLI slash command.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from deepseek import tool


def _notes_dir() -> Path:
    """Return the notes directory, creating it if necessary."""
    base = os.environ.get("DEEPSEEK_NOTES_DIR", str(Path.home() / "notes"))
    p = Path(base).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


@tool
def save_note(title: str, body: str) -> str:
    """Save a markdown note to the notes directory.

    Args:
        title: Short title for the note (used as filename).
        body: Markdown content of the note.
    """
    try:
        notes_dir = _notes_dir()
        # Sanitise the title to a safe filename
        safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in title).strip()
        safe_title = safe_title[:80] or "untitled"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"{timestamp}_{safe_title}.md"
        path = notes_dir / filename
        content = f"# {title}\n\n*Saved {timestamp}*\n\n{body}\n"
        path.write_text(content, encoding="utf-8")
        return f"Note saved to {path}"
    except Exception as e:
        return f"Error saving note: {type(e).__name__}: {e}"


@tool
def list_notes() -> str:
    """List all saved notes in the notes directory.

    Returns a newline-separated list of filenames and first-line titles.
    """
    try:
        notes_dir = _notes_dir()
        files = sorted(notes_dir.glob("*.md"))
        if not files:
            return f"No notes found in {notes_dir}"
        lines = [f"Notes directory: {notes_dir}", ""]
        for f in files:
            try:
                first_line = f.read_text(encoding="utf-8").splitlines()[0].lstrip("# ").strip()
            except Exception:
                first_line = "(unreadable)"
            lines.append(f"  {f.name}  —  {first_line}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing notes: {type(e).__name__}: {e}"


# ── COMMANDS dict ────────────────────────────────────────────────────────────
# Each key is a slash command name. Commands may take the CLI app instance (or
# an emit callback); zero-argument commands still work and their stdout is shown.
# Built-in commands take priority — if /notes were built-in it would be skipped.

def _show_notes(app):
    app.emit("(notes command — see the list_notes tool to list saved notes)")


COMMANDS = {
    "/notes": _show_notes,
}


def register():
    """Return the list of tools provided by this extension."""
    return [save_note, list_notes]
