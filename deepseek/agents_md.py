"""AGENTS.md discovery and generator helpers."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


MAX_AGENTS_LINES = 500


def _candidate_paths(start: Path):
    current = start.resolve()
    roots = [current, *current.parents]
    for directory in roots:
        for name in ("AGENTS.md", ".agents/AGENTS.md"):
            yield directory / name
        for name in ("CLAUDE.md", ".agents/CLAUDE.md"):
            yield directory / name


def discover_agents(start: str | Path | None = None) -> tuple[Path, str] | None:
    base = Path(start or os.getcwd()).resolve()
    for path in _candidate_paths(base):
        if path.is_file():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > MAX_AGENTS_LINES:
                text = "\n".join(lines[:MAX_AGENTS_LINES])
                text += "\n[AGENTS.md truncated at 500 lines]"
            else:
                text = "\n".join(lines)
            return path, text
    return None


def _read(path: Path, limit: int = 20000) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:limit]


def generate_draft(cwd: str | Path | None = None) -> str:
    base = Path(cwd or os.getcwd()).resolve()
    readme = _read(base / "README.md", 4000)
    pyproject = _read(base / "pyproject.toml")
    package = _read(base / "package.json")
    makefile = _read(base / "Makefile")
    workflows = ""
    wf_dir = base / ".github" / "workflows"
    if wf_dir.is_dir():
        workflows = "\n".join(_read(p, 4000) for p in sorted(wf_dir.glob("*.yml")))

    commands: list[str] = []
    if "pytest" in pyproject or "pytest" in readme:
        commands.append("- Test: `pytest`")
    if "ruff" in pyproject:
        commands.append("- Lint: `ruff check .`")
    if package:
        try:
            data = json.loads(package)
            scripts = data.get("scripts") or {}
            for name in ("build", "test", "lint", "start"):
                if name in scripts:
                    commands.append(f"- {name.title()}: `npm run {name}`")
        except Exception:
            pass
    if makefile:
        for target in re.findall(r"^([A-Za-z0-9_.-]+):", makefile, flags=re.MULTILINE):
            if target in {"test", "lint", "build", "run"}:
                commands.append(f"- {target.title()}: `make {target}`")
    if workflows and not commands:
        commands.append("- CI: inspect `.github/workflows/` for the canonical test command")
    if not commands:
        commands.append("- Test: `<add project test command here>`")

    overview = readme.strip().splitlines()[0] if readme.strip() else "Project overview."
    return "\n".join([
        "# Project",
        "",
        overview,
        "",
        "# Commands",
        "",
        *commands,
        "",
        "# Style",
        "",
        "- Follow the formatter and naming conventions already present in the repository.",
        "",
        "# Testing",
        "",
        "- Add or update tests with each behavior change.",
        "- Run the project test command before committing.",
        "",
        "# Layout",
        "",
        "- Document the top-level source, test, and tooling directories here.",
        "",
        "# Notes",
        "",
        "- Keep changes minimal and scoped.",
        "",
    ])
