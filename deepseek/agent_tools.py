"""Codex-style agent tools for the DeepSeek CLI.

This module provides process execution with persistent PTY/pipe sessions,
structured patch application, plan tracking, user questions, and image
metadata inspection. It intentionally uses only the Python standard library.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import difflib
import json
import os
import pty
import select
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .plan_store import PlanStore, render_checklist
from .tools import Tool, ToolCall, execute_tool, tool


MAX_OUTPUT_BYTES = 50 * 1024
MAX_OUTPUT_LINES = 256
METADATA_TOOLS = {"update_plan", "request_user_input"}


@dataclass
class AgentRuntime:
    """Context needed by tools that interact with CLI state."""

    conversation_id: str | None = None
    plan_mode: bool = False
    autonomous: bool = False
    json_mode: bool = False
    interactive: bool = True
    approval: str | Callable[[ToolCall], bool | tuple[bool, bool]] = "auto"
    ask: Callable[[str], str] | None = None
    emit_plan: Callable[[list[dict[str, Any]]], None] | None = None
    emit_file_change: Callable[[str, int, int, str], None] | None = None
    plan_store: PlanStore = field(default_factory=PlanStore)
    tool_registry: Any = None


_CURRENT_RUNTIME: contextvars.ContextVar[AgentRuntime | None] = contextvars.ContextVar(
    "deepseek_agent_runtime", default=None
)


def set_agent_runtime(runtime: AgentRuntime | None) -> None:
    _CURRENT_RUNTIME.set(runtime)


def get_agent_runtime() -> AgentRuntime | None:
    return _CURRENT_RUNTIME.get()


def _runtime_ask(prompt: str) -> str:
    runtime = get_agent_runtime()
    if runtime is not None and runtime.ask is not None:
        return runtime.ask(prompt)
    return input(prompt)


def command_is_read_only(command: str) -> bool:
    """Best-effort read-only classification for exec_command."""
    if not command or not command.strip():
        return False
    if any(token in command for token in (">", ">>", "<<")):
        return False

    read_only_prefixes = (
        "cat", "ls", "rg", "grep", "find", "head", "tail", "wc", "pwd",
        "git status", "git log", "git diff", "git show", "git branch",
    )

    # Split on separators that can sequence multiple commands. A pipeline is
    # read-only only when every stage is read-only.
    normalized = command.replace("&&", ";").replace("||", ";")
    segments = []
    for chunk in normalized.split(";"):
        segments.extend(part.strip() for part in chunk.split("|"))

    for segment in [s for s in segments if s]:
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return False
        if not tokens:
            continue
        lowered = " ".join(tokens[:2]).lower()
        first = tokens[0].lower()
        if first not in {p.split()[0] for p in read_only_prefixes}:
            return False
        if lowered.startswith("git ") and not lowered.startswith(
            ("git status", "git log", "git diff", "git show", "git branch")
        ):
            return False
    return True


def _truncate_output(data: bytes, max_output_tokens: int) -> str:
    max_bytes = min(max(1024, int(max_output_tokens) * 4), MAX_OUTPUT_BYTES)
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    truncated_lines = 0
    if len(lines) > MAX_OUTPUT_LINES:
        truncated_lines = len(lines) - MAX_OUTPUT_LINES
        text = "".join(lines[:MAX_OUTPUT_LINES])

    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        text = encoded[:max_bytes].decode("utf-8", errors="replace")

    if truncated_lines:
        text += f"\n[… truncated {truncated_lines} lines]"
    elif len(encoded) > max_bytes:
        text += f"\n[… truncated {len(encoded) - max_bytes} bytes]"
    return text


@dataclass
class ExecSession:
    session_id: str
    proc: subprocess.Popen
    fd: int | None
    tty: bool
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)


_SESSIONS: dict[str, ExecSession] = {}
_SESSIONS_LOCK = threading.RLock()


def _set_nonblocking(fd: int) -> None:
    try:
        os.set_blocking(fd, False)
    except Exception:
        pass


def _collect_output(session: ExecSession, timeout_ms: int,
                    max_output_tokens: int) -> bytes:
    if session.fd is None:
        return b""
    deadline = time.monotonic() + max(0, min(int(timeout_ms), 30000)) / 1000.0
    max_bytes = min(max(1024, int(max_output_tokens) * 4), MAX_OUTPUT_BYTES)
    chunks: list[bytes] = []
    total = 0

    while total < max_bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            ready, _, _ = select.select([session.fd], [], [], min(0.1, remaining))
        except (OSError, ValueError):
            break
        if not ready:
            if session.proc.poll() is not None:
                # Drain a final nonblocking read before returning.
                continue
            continue
        try:
            data = os.read(session.fd, 4096)
        except BlockingIOError:
            continue
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
        total += len(data)
        session.last_activity = time.time()
        if session.proc.poll() is not None:
            # Let the loop perform one more select/read cycle to collect output
            # that arrived in the same scheduling quantum.
            continue

    return b"".join(chunks)


def _session_status(session: ExecSession, output: bytes, max_output_tokens: int) -> str:
    text = _truncate_output(output, max_output_tokens)
    code = session.proc.poll()
    if code is None and session.fd is not None and not session.tty:
        try:
            code = session.proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            code = None
    if code is None:
        if text and not text.endswith("\n"):
            text += "\n"
        return f"{text}[process still running; session_id={session.session_id}]"
    with _SESSIONS_LOCK:
        _SESSIONS.pop(session.session_id, None)
    if session.tty and session.fd is not None:
        try:
            os.close(session.fd)
        except OSError:
            pass
    if not session.tty:
        for stream in (session.proc.stdin, session.proc.stdout, session.proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
    if text and not text.endswith("\n"):
        text += "\n"
    return f"{text}[process exited with code {code}]"


def _build_popen(args: list[str] | str, *, workdir: str | None, shell: str | None,
                 login: bool, tty_mode: bool):
    popen_kwargs: dict[str, Any] = {
        "cwd": str(Path(workdir).expanduser()) if workdir else None,
        "close_fds": True,
        "start_new_session": True,
        "bufsize": 0,
    }
    if isinstance(args, str):
        popen_kwargs.update({"args": args, "shell": True})
        if shell:
            popen_kwargs["executable"] = shell
    else:
        popen_kwargs["args"] = args

    if tty_mode:
        master_fd, slave_fd = pty.openpty()
        popen_kwargs.update({"stdin": slave_fd, "stdout": slave_fd, "stderr": slave_fd})
        proc = subprocess.Popen(**popen_kwargs)
        os.close(slave_fd)
        _set_nonblocking(master_fd)
        return proc, master_fd

    popen_kwargs.update({
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
    })
    proc = subprocess.Popen(**popen_kwargs)
    assert proc.stdout is not None
    _set_nonblocking(proc.stdout.fileno())
    return proc, proc.stdout.fileno()


@tool(deferred=False, read_only=None)
def exec_command(
    cmd: str,
    workdir: str | None = None,
    yield_time_ms: int = 10000,
    max_output_tokens: int = 12000,
    sandbox_permissions: str | None = None,
    shell: str | None = None,
    tty: bool = False,
    login: bool = True,
    justification: str | None = None,
    prefix_rule: list[str] | None = None,
) -> str:
    """Run a command in a PTY, returning output or a session ID for ongoing interaction.

    Args:
        cmd: Command string to execute.
        workdir: Working directory for the command.
        yield_time_ms: Milliseconds to wait for output before returning a session id.
        max_output_tokens: Maximum estimated tokens to return (about 4 chars/token).
        sandbox_permissions: Accepted for Codex compatibility; unused by this CLI.
        shell: Optional shell executable to use.
        tty: If true, run under a pseudo-terminal for interactive programs.
        login: If true, use a login shell when a shell executable is provided.
        justification: Accepted for Codex compatibility; unused by this CLI.
        prefix_rule: Accepted for Codex compatibility; unused by this CLI.
    """
    _ = (sandbox_permissions, justification, prefix_rule)
    if not cmd.strip():
        return "Error: cmd must not be empty."
    try:
        if shell:
            shell_name = Path(shell).name
            if login and shell_name in {"bash", "zsh", "sh"}:
                args: list[str] | str = [shell, "-lc", cmd]
            else:
                args = [shell, "-c", cmd]
        else:
            args = cmd
        proc, fd = _build_popen(
            args, workdir=workdir, shell=shell, login=login, tty_mode=bool(tty)
        )
    except Exception as exc:
        return f"Error launching command: {type(exc).__name__}: {exc}"

    session_id = "sess_" + uuid.uuid4().hex[:12]
    session = ExecSession(session_id=session_id, proc=proc, fd=fd, tty=bool(tty))
    with _SESSIONS_LOCK:
        _SESSIONS[session_id] = session
    output = _collect_output(session, yield_time_ms, max_output_tokens)
    return _session_status(session, output, max_output_tokens)


def _get_session(session_id: str) -> ExecSession | None:
    with _SESSIONS_LOCK:
        return _SESSIONS.get(session_id)


def latest_running_session_id() -> str | None:
    """Most recently created session whose process is still running."""
    with _SESSIONS_LOCK:
        sessions = sorted(_SESSIONS.values(), key=lambda s: s.created_at, reverse=True)
    for session in sessions:
        if session.proc.poll() is None:
            return session.session_id
    return None


@tool(deferred=False, read_only=False)
def write_stdin(session_id: str, chars: str = "",
                yield_time_ms: int = 250, max_output_tokens: int = 12000) -> str:
    """Write characters to an existing exec_command session and return recent output.

    Args:
        session_id: Session id returned by exec_command.
        chars: Characters to write to stdin.
        yield_time_ms: Milliseconds to wait for output after writing.
        max_output_tokens: Maximum estimated tokens to return.
    """
    session = _get_session(session_id)
    if session is None:
        with _SESSIONS_LOCK:
            known = ", ".join(sorted(_SESSIONS)) or "(none)"
        return f"Error: unknown session_id '{session_id}'. Known sessions: {known}"

    try:
        if chars:
            data = chars.encode("utf-8")
            if session.tty and session.fd is not None:
                os.write(session.fd, data)
            else:
                if session.proc.stdin is None:
                    return "Error: session stdin is closed."
                session.proc.stdin.write(data)
                session.proc.stdin.flush()
            session.last_activity = time.time()
    except Exception as exc:
        return f"Error writing to session '{session_id}': {type(exc).__name__}: {exc}"

    output = _collect_output(session, yield_time_ms, max_output_tokens)
    return _session_status(session, output, max_output_tokens)


def _parse_patch(patch: str) -> list[dict[str, Any]]:
    lines = patch.splitlines()
    if not lines or lines[0].strip() != "*** Begin Patch":
        raise ValueError("patch must start with *** Begin Patch")
    if lines[-1].strip() != "*** End Patch":
        raise ValueError("patch must end with *** End Patch")

    operations: list[dict[str, Any]] = []
    i = 1
    while i < len(lines) - 1:
        line = lines[i]
        if line.startswith("*** Update File: "):
            path = line[len("*** Update File: "):].strip()
            i += 1
            hunks: list[dict[str, Any]] = []
            current: dict[str, Any] | None = None
            move_to: str | None = None
            while i < len(lines) - 1:
                item = lines[i]
                if item.startswith("*** Move to: "):
                    move_to = item[len("*** Move to: "):].strip()
                    i += 1
                    continue
                if item.startswith("*** "):
                    break
                if item.startswith("@@"):
                    if current is not None:
                        hunks.append(current)
                    current = {"header": item[2:].strip(), "lines": []}
                elif current is not None:
                    if item.startswith("+"):
                        current["lines"].append(("add", item[1:]))
                    elif item.startswith("-"):
                        current["lines"].append(("remove", item[1:]))
                    elif item.startswith(" "):
                        current["lines"].append(("context", item[1:]))
                    elif item.startswith("\\"):
                        pass
                    else:
                        current["lines"].append(("context", item))
                else:
                    raise ValueError(f"unexpected line before first hunk: {item!r}")
                i += 1
            if current is not None:
                hunks.append(current)
            if not hunks and move_to is None:
                raise ValueError(f"no hunks found for {path}")
            operations.append(
                {"op": "update", "path": path, "hunks": hunks, "move_to": move_to}
            )
            continue
        if line.startswith("*** Add File: "):
            path = line[len("*** Add File: "):].strip()
            i += 1
            content_lines: list[str] = []
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                item = lines[i]
                if item.startswith("+"):
                    content_lines.append(item[1:])
                elif item.startswith("\\"):
                    pass
                else:
                    raise ValueError(f"Add File lines must start with +: {item!r}")
                i += 1
            operations.append({"op": "add", "path": path, "lines": content_lines})
            continue
        if line.startswith("*** Delete File: "):
            path = line[len("*** Delete File: "):].strip()
            operations.append({"op": "delete", "path": path})
            i += 1
            continue
        if line.strip():
            raise ValueError(f"unsupported patch directive: {line!r}")
        i += 1
    return operations


def _split_text(text: str) -> tuple[list[str], bool]:
    return text.splitlines(), text.endswith("\n")


def _join_text(lines: list[str], trailing_newline: bool) -> str:
    text = "\n".join(lines)
    if trailing_newline:
        text += "\n"
    return text


@tool(deferred=False, read_only=False)
def apply_patch(patch: str) -> str:
    """Apply a Codex-style patch to files.

    Args:
        patch: Patch text using *** Begin Patch, *** Update File, *** Add File,
            *** Delete File, @@ hunks, and -/+ edit lines.
    """
    try:
        operations = _parse_patch(patch)
    except Exception as exc:
        return f"Error applying patch: {exc}"

    pending_writes: list[tuple[Path, str]] = []
    pending_deletes: list[Path] = []
    change_records: list[tuple[Path, str, str]] = []
    hunk_count = 0
    touched: set[Path] = set()

    try:
        for operation in operations:
            path = Path(operation["path"]).expanduser()
            touched.add(path)
            if operation["op"] == "add":
                if path.exists():
                    raise ValueError(f"Add File target already exists: {path}")
                content = "\n".join(operation["lines"])
                if operation["lines"]:
                    content += "\n"
                pending_writes.append((path, content))
                change_records.append((path, "", content))
                hunk_count += 1
                continue

            if operation["op"] == "delete":
                if not path.exists():
                    raise ValueError(f"Delete File target does not exist: {path}")
                pending_deletes.append(path)
                change_records.append((path, path.read_text(encoding="utf-8"), ""))
                hunk_count += 1
                continue

            if not path.exists():
                raise ValueError(f"Update File target does not exist: {path}")
            original = path.read_text(encoding="utf-8")
            file_lines, trailing = _split_text(original)
            for hunk in operation["hunks"]:
                old_lines = [line for kind, line in hunk["lines"] if kind in {"context", "remove"}]
                new_lines = [line for kind, line in hunk["lines"] if kind in {"context", "add"}]
                if not old_lines:
                    file_lines.extend(new_lines)
                    hunk_count += 1
                    continue
                match_index = None
                for idx in range(0, len(file_lines) - len(old_lines) + 1):
                    if file_lines[idx:idx + len(old_lines)] == old_lines:
                        match_index = idx
                        break
                if match_index is None:
                    header = hunk.get("header") or ""
                    detail = f" ({header})" if header else ""
                    raise ValueError(f"context not found in {path}{detail}")
                file_lines[match_index:match_index + len(old_lines)] = new_lines
                hunk_count += 1
            updated = _join_text(file_lines, trailing)
            move_to = operation.get("move_to")
            if move_to:
                target = Path(move_to).expanduser()
                if target.exists():
                    raise ValueError(f"Move target already exists: {target}")
                touched.add(target)
                pending_writes.append((target, updated))
                pending_deletes.append(path)
                change_records.append((path, original, ""))
                change_records.append((target, "", updated))
            else:
                pending_writes.append((path, updated))
                change_records.append((path, original, updated))

        for path, content in pending_writes:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        for path in pending_deletes:
            path.unlink()
    except Exception as exc:
        return f"Error applying patch: {exc}"

    runtime = get_agent_runtime()
    if runtime and runtime.emit_file_change is not None:
        for path, before, after in change_records:
            diff_text = "".join(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=str(path),
                    tofile=str(path),
                )
            )
            additions = sum(
                1 for line in diff_text.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            )
            deletions = sum(
                1 for line in diff_text.splitlines()
                if line.startswith("-") and not line.startswith("---")
            )
            try:
                runtime.emit_file_change(str(path), additions, deletions, diff_text)
            except Exception:
                pass

    return f"applied {hunk_count} hunks to {len(touched)} files"


@tool(deferred=False, read_only=True)
def update_plan(explanation: str = "",
                plan: list[dict[str, Any]] | None = None) -> str:
    """Keep an up-to-date step-by-step plan.

    Args:
        explanation: Optional short explanation of the plan update.
        plan: Array of objects with step and status.
    """
    if not plan:
        return "Error: plan must contain 1-7 steps."
    if len(plan) > 7:
        return "Error: plan must contain no more than 7 steps."
    normalized: list[dict[str, str]] = []
    in_progress = 0
    for item in plan:
        if not isinstance(item, dict):
            return "Error: each plan item must be an object."
        step = str(item.get("step", "")).strip()
        status = str(item.get("status", "pending")).strip()
        if not step:
            return "Error: each plan item needs a step."
        if status not in {"pending", "in_progress", "completed"}:
            return f"Error: invalid plan status '{status}'."
        if status == "in_progress":
            in_progress += 1
        normalized.append({"step": step, "status": status})
    if in_progress > 1:
        return "Error: at most one plan step may be in_progress."

    runtime = get_agent_runtime()
    store = runtime.plan_store if runtime else PlanStore()
    conversation_id = runtime.conversation_id if runtime else None
    data = store.save(conversation_id, normalized, explanation=explanation)
    checklist = render_checklist(normalized)
    if runtime and runtime.emit_plan is not None:
        try:
            runtime.emit_plan(normalized)
        except Exception:
            pass
    archived = (
        " Plan archived as completed."
        if all(item["status"] == "completed" for item in normalized)
        else ""
    )
    return f"Plan updated:\n{checklist}{archived}\nSaved: {store.path(conversation_id)}"


@tool(deferred=False, read_only=True)
def request_user_input(questions: list[dict[str, Any]]) -> str:
    """Ask the user 1-3 structured questions with mutually exclusive options.

    Args:
        questions: Array of question objects with id, header, question, and options.
    """
    runtime = get_agent_runtime()
    if runtime is None or not runtime.plan_mode:
        return "Error: request_user_input is available in plan mode only."
    if not isinstance(questions, list) or not 1 <= len(questions) <= 3:
        return "Error: questions must contain 1-3 items."

    answers: list[dict[str, str]] = []
    for idx, question in enumerate(questions, 1):
        if not isinstance(question, dict):
            return "Error: each question must be an object."
        qid = str(question.get("id", f"q{idx}"))
        header = str(question.get("header", qid))[:12]
        text = str(question.get("question", "")).strip()
        options = question.get("options") or []
        if not text or not isinstance(options, list) or not 2 <= len(options) <= 3:
            return "Error: each question needs text and 2-3 options."

        lines = [f"{header}: {text}"]
        labels: list[str] = []
        for opt_idx, option in enumerate(options, 1):
            if not isinstance(option, dict):
                return "Error: each option must be an object."
            label = str(option.get("label", "")).strip()
            description = str(option.get("description", "")).strip()
            if not label:
                return "Error: each option needs a label."
            labels.append(label)
            suffix = f" - {description}" if description else ""
            lines.append(f"  {opt_idx}. {label}{suffix}")
        prompt = "\n".join(lines) + "\nAnswer number or free text: "
        raw = _runtime_ask(prompt).strip()
        if raw.isdigit():
            choice = int(raw)
            answer = labels[choice - 1] if 1 <= choice <= len(labels) else raw
        else:
            answer = raw
        answers.append({"id": qid, "answer": answer})

    return json.dumps({"answers": answers}, ensure_ascii=False)


def tool_is_read_only(tool_obj: Tool, call: ToolCall) -> bool:
    if tool_obj.name == "exec_command":
        return command_is_read_only(str(call.arguments.get("cmd", "")))
    if tool_obj.name == "search_tools":
        return False
    if tool_obj.read_only is not None:
        return bool(tool_obj.read_only)
    return False


def tool_is_metadata(tool_obj: Tool) -> bool:
    return tool_obj.name in METADATA_TOOLS


class ToolExecutor:
    """Execute tool calls with Codex-style ordering and approval batching."""

    def __init__(
        self,
        approval: str | Callable[[ToolCall], bool | tuple[bool, bool]] = "auto",
        emit_call: Callable[[ToolCall], None] | None = None,
    ):
        self.approval = approval
        self.emit_call = emit_call

    def _approved(self, call: ToolCall, tool_obj: Tool | None,
                  always_approved: set[str]) -> bool:
        if tool_obj is not None and tool_is_metadata(tool_obj):
            return True
        if self.approval == "auto":
            return True
        if call.name in always_approved:
            return True
        if callable(self.approval):
            result = self.approval(call)
            if isinstance(result, tuple):
                approved, remember = result
                if remember and approved:
                    always_approved.add(call.name)
                return bool(approved)
            return bool(result)
        if self.approval == "manual":
            return False
        raise ValueError(f"Unknown approval mode: {self.approval!r}")

    def execute(
        self,
        calls: list[ToolCall],
        tools: list[Tool],
        decisions: list[bool] | None = None,
    ) -> list[tuple[ToolCall, str]]:
        tool_map = {t.name: t for t in tools}
        always_approved: set[str] = set()
        approved: list[bool] = []

        if self.emit_call is not None:
            for call in calls:
                self.emit_call(call)
        if decisions is not None:
            if len(decisions) != len(calls):
                raise ValueError("decisions length must match calls length")
            approved = list(decisions)
        else:
            for call in calls:
                approved.append(self._approved(call, tool_map.get(call.name), always_approved))

        results: list[str | None] = [None] * len(calls)
        read_only_jobs: list[tuple[int, ToolCall]] = []
        serial_jobs: list[tuple[int, ToolCall]] = []

        for idx, (call, ok) in enumerate(zip(calls, approved)):
            if not ok:
                results[idx] = "User rejected this tool call."
                continue
            tool_obj = tool_map.get(call.name)
            if tool_obj is not None and tool_is_metadata(tool_obj):
                serial_jobs.append((idx, call))
            elif tool_obj is not None and tool_is_read_only(tool_obj, call):
                read_only_jobs.append((idx, call))
            else:
                serial_jobs.append((idx, call))

        if read_only_jobs:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                future_map = {
                    pool.submit(execute_tool, call, tools): idx
                    for idx, call in read_only_jobs
                }
                for future, idx in future_map.items():
                    try:
                        results[idx] = future.result()
                    except Exception as exc:
                        results[idx] = (
                            f"Error executing tool '{calls[idx].name}': "
                            f"{type(exc).__name__}: {exc}"
                        )

        for idx, call in serial_jobs:
            results[idx] = execute_tool(call, tools)

        return [
            (call, result if result is not None else "")
            for call, result in zip(calls, results)
        ]
