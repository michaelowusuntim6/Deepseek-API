---
name: agents-init
description: Generate AGENTS.md with the CLI's repo scanner.
---

# Agents Init

## When to use
Use when the user asks to create or refresh AGENTS.md.

## Steps
1. Run the CLI command `/agents-init` or use `--generate-agents`.
2. Review the draft.
3. Refine commands, layout, and testing notes.
4. Keep the file under 500 lines.

## Notes
Do not reimplement the scan; the CLI generator inspects README, pyproject,
package.json, Makefile, CI workflows, and tests.
