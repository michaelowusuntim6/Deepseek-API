"""Client-side auto-compaction for DeepSeek web chat sessions."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import DeepSeekClient
from .plan_store import PlanStore, render_checklist, summaries_dir


DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_COMPACT_RATIO = 0.80
DEFAULT_COMPACT_AT = int(DEFAULT_CONTEXT_WINDOW * DEFAULT_COMPACT_RATIO)

SUMMARY_PROMPT = (
    "Summarize the conversation so far. Preserve: all file paths mentioned, "
    "all commands run, all decisions made, the current task state, all open "
    "TODOs, and any user constraints or preferences. Be concise but lossless "
    "on facts. Format as a structured brief."
)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def compact_threshold(value: int | None = None) -> int:
    if value is not None:
        return int(value)
    env_value = os.getenv("DEEPSEEK_COMPACT_AT")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass
    return DEFAULT_COMPACT_AT


def _safe_id(conversation_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", conversation_id)


@dataclass
class CompactionResult:
    old_conversation_id: str
    new_conversation_id: str
    summary: str
    summary_path: Path
    prefix: str
    count: int
    warning: str | None = None


class CompactionManager:
    def __init__(
        self,
        compact_at: int | None = None,
        plan_store: PlanStore | None = None,
        warn_after: int = 3,
    ):
        self.threshold = compact_threshold(compact_at)
        self.plan_store = plan_store or PlanStore()
        self.warn_after = warn_after
        self.count = 0

    def should_compact(self, context_chars: int) -> bool:
        return max(0, context_chars) // 4 >= self.threshold

    def compact(
        self,
        client: DeepSeekClient,
        conversation_id: str | None,
        *,
        plan_enabled: bool = False,
    ) -> CompactionResult | None:
        if not conversation_id:
            return None

        summary_prompt = SUMMARY_PROMPT
        plan_data: dict[str, Any] | None = None
        if plan_enabled:
            plan_data = self.plan_store.load(conversation_id)
            plan = (plan_data or {}).get("plan") or []
            if plan:
                summary_prompt += (
                    "\n\nActive plan that must survive compaction:\n"
                    + render_checklist(plan)
                )

        reply = client.chat(
            summary_prompt,
            conversation_id=conversation_id,
            model=None,
            thinking=False,
            search=False,
        )
        summary = (reply.text or "").strip()
        if not summary:
            raise RuntimeError("compaction summary was empty")

        target = summaries_dir() / f"{_safe_id(conversation_id)}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"# Conversation summary: {conversation_id}\n\n{summary}\n",
            encoding="utf-8",
        )

        new_session_id = client.create_chat_session()
        if plan_enabled and plan_data:
            self.plan_store.carry_over(conversation_id, new_session_id)

        self.count += 1
        warning = None
        if self.count > self.warn_after:
            warning = (
                f"This is the {self.count}th compaction. Consider starting a "
                "fresh /new thread or splitting the task."
            )
        prefix = (
            f"Previous conversation summary:\n{summary}\n\nContinue from here."
        )
        return CompactionResult(
            old_conversation_id=conversation_id,
            new_conversation_id=new_session_id,
            summary=summary,
            summary_path=target,
            prefix=prefix,
            count=self.count,
            warning=warning,
        )
