"""Persistent plan storage for the DeepSeek CLI.

Plans are stored one JSON file per conversation id under
``$DEEPSEEK_CLI_HOME/plans`` (default ``~/.deepseek-cli/plans``). Completed
plans are moved to ``plans/completed``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VALID_STATUSES = {"pending", "in_progress", "completed"}
STATUS_SYMBOLS = {
    "pending": "☐",
    "in_progress": "◐",
    "completed": "☑",
}


def cli_home() -> Path:
    """Return the DeepSeek CLI state directory, honoring the env override."""
    configured = os.getenv("DEEPSEEK_CLI_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".deepseek-cli"


def plans_dir() -> Path:
    return cli_home() / "plans"


def completed_plans_dir() -> Path:
    return plans_dir() / "completed"


def summaries_dir() -> Path:
    return cli_home() / "summaries"


def _safe_id(conversation_id: str | None) -> str:
    if not conversation_id:
        return "pending"
    # DeepSeek conversation ids are "<session_id>:<message_id>". The message
    # suffix changes every turn, so plans must be keyed by the stable session id.
    session_id = conversation_id.split(":", 1)[0]
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def render_checklist(plan: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in plan:
        status = str(item.get("status", "pending"))
        symbol = STATUS_SYMBOLS.get(status, "☐")
        lines.append(f"{symbol} {item.get('step', '')}")
    return "\n".join(lines)


class PlanStore:
    """Read/write helper for persistent update_plan state."""

    def path(self, conversation_id: str | None) -> Path:
        return plans_dir() / f"{_safe_id(conversation_id)}.json"

    def completed_path(self, conversation_id: str | None) -> Path:
        return completed_plans_dir() / f"{_safe_id(conversation_id)}.json"

    def load(self, conversation_id: str | None) -> dict[str, Any] | None:
        for path in (self.path(conversation_id), self.completed_path(conversation_id)):
            if path.exists():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    return None
        return None

    def save(
        self,
        conversation_id: str | None,
        plan: list[dict[str, Any]],
        explanation: str = "",
        history_entry: str | None = None,
    ) -> dict[str, Any]:
        existing = self.load(conversation_id) or {}
        now = _now()
        history = list(existing.get("history") or [])
        entry = {
            "at": now,
            "explanation": explanation,
            "plan": plan,
        }
        if history_entry:
            entry["note"] = history_entry
        history.append(entry)
        data = {
            "conversation_id": conversation_id or "pending",
            "created_at": existing.get("created_at") or now,
            "updated_at": now,
            "plan": plan,
            "history": history,
        }
        path = self.path(conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        if plan and all(item.get("status") == "completed" for item in plan):
            self.archive_completed(conversation_id)
        return data

    def archive_completed(self, conversation_id: str | None) -> Path | None:
        src = self.path(conversation_id)
        if not src.exists():
            return None
        dst = self.completed_path(conversation_id)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return dst

    def clear(self, conversation_id: str | None) -> bool:
        removed = False
        for path in (self.path(conversation_id), self.completed_path(conversation_id)):
            if path.exists():
                path.unlink()
                removed = True
        return removed

    def carry_over(self, old_conversation_id: str | None,
                   new_conversation_id: str | None) -> dict[str, Any] | None:
        data = self.load(old_conversation_id)
        if not data:
            return None
        plan = data.get("plan") or []
        return self.save(
            new_conversation_id,
            plan,
            explanation="Plan carried across context compaction.",
            history_entry=f"carried over from {old_conversation_id}",
        )

    def next_in_progress(self, conversation_id: str | None) -> str | None:
        data = self.load(conversation_id)
        if not data:
            return None
        for item in data.get("plan") or []:
            if item.get("status") == "in_progress":
                return str(item.get("step", ""))
        return None

    def make_resume_prompt(self, conversation_id: str | None) -> str | None:
        data = self.load(conversation_id)
        if not data:
            return None
        plan = data.get("plan") or []
        if not plan:
            return None
        current = self.next_in_progress(conversation_id)
        suffix = (
            f"\nContinue from the in_progress step: {current}."
            if current
            else "\nContinue from the active plan."
        )
        return "Active plan from previous session:\n" + render_checklist(plan) + suffix
