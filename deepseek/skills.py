"""Progressive-disclosure skills registry and tools."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .tools import tool


MAX_SKILLS = 30
MAX_DESCRIPTION = 80


@dataclass
class Skill:
    name: str
    description: str
    path: Path


def skill_search_paths() -> list[Path]:
    return [
        Path.home() / ".deepseek-cli" / "skills",
        Path.cwd() / ".deepseek-cli" / "skills",
    ]


def _parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    data: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def load_skills(cwd: str | Path | None = None) -> tuple[list[Skill], str | None]:
    cwd_name = Path(cwd or os.getcwd()).name.lower()
    found: dict[str, Skill] = {}
    for base in skill_search_paths():
        if not base.is_dir():
            continue
        for skill_md in sorted(base.glob("*/SKILL.md")):
            try:
                head = skill_md.read_text(encoding="utf-8", errors="replace")[:2048]
            except OSError:
                continue
            meta = _parse_frontmatter(head)
            name = meta.get("name") or skill_md.parent.name
            description = (meta.get("description") or "").strip()
            if len(description) > MAX_DESCRIPTION:
                description = description[: MAX_DESCRIPTION - 1] + "…"
            found[name] = Skill(name=name, description=description, path=skill_md)
    skills = list(found.values())
    warning = None
    if len(skills) > MAX_SKILLS:
        skills.sort(key=lambda skill: (cwd_name not in skill.name.lower(), skill.name))
        skills = skills[:MAX_SKILLS]
        warning = f"[skills] more than {MAX_SKILLS} skills found; truncated to {MAX_SKILLS}"
    return skills, warning


def skills_preamble(cwd: str | Path | None = None) -> tuple[str, str | None]:
    skills, warning = load_skills(cwd)
    if not skills:
        return "", warning
    lines = [
        "## Available skills",
        "Use `use_skill(name)` to load a skill's full instructions.",
    ]
    lines.extend(f"- {skill.name}: {skill.description}" for skill in skills)
    return "\n".join(lines), warning


_ACTIVE_SKILLS: set[str] = set()


def clear_active_skills() -> None:
    _ACTIVE_SKILLS.clear()


@tool(eager=True, read_only=True)
def list_skills() -> str:
    """Return every discovered skill name, description, and path."""
    skills, warning = load_skills()
    lines = [f"{s.name}: {s.description} ({s.path})" for s in skills]
    if warning:
        lines.append(warning)
    return "\n".join(lines) or "No skills discovered."


@tool(eager=True, read_only=True)
def use_skill(name: str) -> str:
    """Load the full SKILL.md body for one skill.

    Args:
        name: Skill name.
    """
    skills, _ = load_skills()
    match = next((skill for skill in skills if skill.name == name), None)
    if match is None:
        return f"Error: skill '{name}' not found."
    _ACTIVE_SKILLS.add(match.name)
    return match.path.read_text(encoding="utf-8", errors="replace")


@tool(eager=True, read_only=True)
def read_skill_file(name: str, path: str) -> str:
    """Read a reference file inside a skill directory.

    Args:
        name: Skill name previously loaded with use_skill.
        path: Relative path inside the skill directory.
    """
    if name not in _ACTIVE_SKILLS:
        return f"Error: call use_skill('{name}') first."
    if path.startswith("/") or ".." in Path(path).parts:
        return "Error: unsafe skill file path."
    skills, _ = load_skills()
    match = next((skill for skill in skills if skill.name == name), None)
    if match is None:
        return f"Error: skill '{name}' not found."
    target = (match.path.parent / path).resolve()
    try:
        target.relative_to(match.path.parent.resolve())
    except ValueError:
        return "Error: unsafe skill file path."
    if not target.is_file():
        return f"Error: skill file '{path}' not found."
    return target.read_text(encoding="utf-8", errors="replace")
