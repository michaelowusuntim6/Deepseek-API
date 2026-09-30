---
name: skill-creator
description: Author a new DeepSeek CLI SKILL.md with valid frontmatter.
---

# Skill Creator

## When to use
Use when the user asks you to create or update a skill.

## Steps
1. Choose a kebab-case skill name.
2. Write `~/.deepseek-cli/skills/<name>/SKILL.md`.
3. Keep the body under 500 lines.
4. Put long schemas in `references/` and link them from the body.
5. See `references/frontmatter.md` for the exact frontmatter schema.

## Notes
Descriptions must be one sentence and no longer than 80 characters.
