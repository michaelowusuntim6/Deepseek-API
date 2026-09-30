"""Pi-style Markdown memory extension with BM25 retrieval."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import date as _date, datetime
from pathlib import Path

from deepseek import tool
from deepseek.bm25 import BM25, tokenize


MEMORY_LINE_CAP = 200
ROLLOVER_LINES = 50
INJECTION_CHAR_CAP = 2000


def _home() -> Path:
    return Path(os.getenv("DEEPSEEK_CLI_HOME", Path.home() / ".deepseek-cli")).expanduser()


def _memory_dir() -> Path:
    path = _home() / "memory"
    path.mkdir(parents=True, exist_ok=True)
    (path / "daily").mkdir(exist_ok=True)
    (path / "archive").mkdir(exist_ok=True)
    return path


def _path_for(target: str) -> Path | None:
    base = _memory_dir()
    if target == "long_term":
        return base / "MEMORY.md"
    if target == "scratchpad":
        return base / "SCRATCHPAD.md"
    if target == "daily":
        return base / "daily" / f"{_date.today().isoformat()}.md"
    return None


def _rollover_long_term() -> str | None:
    path = _path_for("long_term")
    assert path is not None
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) <= MEMORY_LINE_CAP:
        return None
    moved = lines[:ROLLOVER_LINES]
    kept = lines[ROLLOVER_LINES:]
    archive = _memory_dir() / "archive" / f"{_date.today().strftime('%Y-%m')}.md"
    with archive.open("a", encoding="utf-8") as f:
        f.write(f"\n## Rolled over {_date.today().isoformat()}\n")
        f.write("\n".join(moved))
        f.write("\n")
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return str(archive)


@tool
def memory_write(target: str, content: str, mode: str = "append") -> str:
    """Write to long-term, daily, or scratchpad memory.

    Args:
        target: long_term, daily, or scratchpad.
        content: Markdown memory content.
        mode: append or replace.
    """
    path = _path_for(target)
    if path is None:
        return "Error: target must be long_term, daily, or scratchpad."
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "replace":
        path.write_text(content.rstrip() + "\n", encoding="utf-8")
    else:
        with path.open("a", encoding="utf-8") as f:
            f.write(content.rstrip() + "\n")
    rolled = _rollover_long_term() if target == "long_term" else None
    suffix = f" Rolled over to {rolled}." if rolled else ""
    return f"wrote {target} memory to {path}.{suffix}"


@tool
def memory_read(target: str, date: str = "", limit: int = 200) -> str:
    """Read memory files or list daily files.

    Args:
        target: long_term, scratchpad, daily, or list.
        date: Optional YYYY-MM-DD for daily reads.
        limit: Maximum lines to return.
    """
    base = _memory_dir()
    if target == "list":
        files = sorted((base / "daily").glob("*.md"))
        return "\n".join(str(p) for p in files) or "No daily memory files."
    if target == "daily":
        day = date or _date.today().isoformat()
        path = base / "daily" / f"{day}.md"
    elif target == "long_term":
        path = base / "MEMORY.md"
    elif target == "scratchpad":
        path = base / "SCRATCHPAD.md"
    else:
        return "Error: target must be long_term, scratchpad, daily, or list."
    if not path.exists():
        return f"No memory file at {path}."
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[: max(1, int(limit))])


def _paragraphs() -> list[tuple[str, int, str]]:
    base = _memory_dir()
    files = [base / "MEMORY.md", base / "SCRATCHPAD.md"]
    files.extend(sorted((base / "daily").glob("*.md"))[-3:])
    paragraphs: list[tuple[str, int, str]] = []
    for path in files:
        if not path.exists():
            continue
        current: list[str] = []
        start_line = 1
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                if not current:
                    start_line = lineno
                current.append(line)
            elif current:
                paragraphs.append((str(path), start_line, "\n".join(current)))
                current = []
        if current:
            paragraphs.append((str(path), start_line, "\n".join(current)))
    return paragraphs


def _usage_path() -> Path:
    return _memory_dir() / ".usage.json"


def _load_usage() -> dict[str, int]:
    path = _usage_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_usage(data: dict[str, int]) -> None:
    _usage_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def _entry_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _search_paragraphs(query: str, limit: int = 5) -> list[tuple[str, int, str, float]]:
    paragraphs = _paragraphs()
    if not paragraphs:
        return []
    docs = [tokenize(text) for _, _, text in paragraphs]
    index = BM25(docs)
    scored = index.search_with_scores(query)
    results: list[tuple[str, int, str, float]] = []
    for idx, score, tokens in scored:
        if score <= 0:
            continue
        source, line, text = paragraphs[idx]
        ratio = score / max(1, tokens)
        results.append((source, line, text, ratio))
    results.sort(key=lambda item: item[3], reverse=True)
    return results[: max(1, int(limit))]


@tool
def memory_search(query: str, limit: int = 5) -> str:
    """Search memory paragraphs with BM25.

    Args:
        query: Search query.
        limit: Maximum matching paragraphs.
    """
    results = _search_paragraphs(query, limit=limit)
    usage = _load_usage()
    for _, _, text, _ in results:
        key = _entry_key(text)
        usage[key] = usage.get(key, 0) + 1
    _save_usage(usage)
    if not results:
        return "No memory matches."
    return "\n\n".join(
        f"{source}:{line}\n{text}" for source, line, text, _ in results
    )


@tool
def memory_forget(match: str) -> str:
    """Remove case-insensitive matching lines from long-term memory.

    Args:
        match: Substring to remove from MEMORY.md.
    """
    path = _path_for("long_term")
    assert path is not None
    if not path.exists():
        return "No MEMORY.md file."
    lines = path.read_text(encoding="utf-8").splitlines()
    needle = match.lower()
    removed = [line for line in lines if needle in line.lower()]
    kept = [line for line in lines if needle not in line.lower()]
    if not removed:
        return f"No memory entries matched {match!r}."
    log = _memory_dir() / "archive" / "forget-log.md"
    with log.open("a", encoding="utf-8") as f:
        f.write(f"\n## Forgot {datetime.now().isoformat(timespec='seconds')}\n")
        f.write("\n".join(removed))
        f.write("\n")
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return f"Removed {len(removed)} line(s); recovery log: {log}"


@tool
def memory_status() -> str:
    """Report memory file paths, sizes, and line counts."""
    base = _memory_dir()
    lines = []
    for label, path in [
        ("long_term", base / "MEMORY.md"),
        ("scratchpad", base / "SCRATCHPAD.md"),
    ]:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            line_count = len(text.splitlines())
            lines.append(f"{label}: {path} ({len(text)} bytes, {line_count} lines)")
        else:
            lines.append(f"{label}: {path} (missing)")
    daily = sorted((base / "daily").glob("*.md"))
    lines.append(f"daily: {len(daily)} file(s)")
    return "\n".join(lines)


def context_provider(cwd: str = "", agents_summary: str = "") -> str:
    """Return capped auto-retrieved memory for preamble injection."""
    query = f"{Path(cwd or os.getcwd()).name} {agents_summary}".strip()
    results = _search_paragraphs(query, limit=3)
    if not results:
        results = [(source, line, text, 1.0) for source, line, text in _paragraphs()[-3:]]
    selected: list[str] = []
    total = 0
    usage = _load_usage()
    for _, _, text, _ in results:
        if total + len(text) + 5 > INJECTION_CHAR_CAP:
            continue
        selected.append(text)
        total += len(text) + 5
        key = _entry_key(text)
        usage[key] = usage.get(key, 0) + 1
    _save_usage(usage)
    for key, count in usage.items():
        if count < 3:
            continue
        match = next((text for _, _, text, _ in results if _entry_key(text) == key), None)
        if match and not (_path_for("long_term") and _path_for("long_term").exists() and
                          match in _path_for("long_term").read_text(encoding="utf-8")):
            memory_write.fn("long_term", f"{match} #promoted #from-{_date.today().isoformat()}")
    if selected:
        sys.stderr.write(f"[memory] injected {len(selected)} memories ({total} chars)\n")
    return "\n---\n".join(selected)


def register():
    return [memory_write, memory_read, memory_search, memory_forget, memory_status]
