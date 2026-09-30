# Context Layer — Living To-Do List

Last updated: 2026-09-30

## Preflight

- [x] Read current tree, CLI, tool search, extensions, client, README, .gitignore.
- [x] Read CLI_POLISH_REPORT.md and CODEX_TOOLS_REPORT.md.
- [x] Create this checklist.

## Task 0 — Shared BM25

- [x] Create `deepseek/bm25.py` with tokenizer and BM25 class.
- [x] Add `search_with_scores(query, top_k)` returning `(doc_id, score, token_count)`.
- [x] Refactor `deepseek/tool_search.py` to import it.
- [x] Add `tests/test_bm25.py`.
- [x] Run BM25 and CLI regression tests.

## Task 1 — Memory Extension

- [x] Create `examples/extensions/memory.py`.
- [x] Implement memory files and directories.
- [x] Implement write/read/search/forget/status tools.
- [x] Implement BM25 paragraph retrieval.
- [x] Implement session-start auto-injection with 2000-char cap.
- [x] Implement 200-line rollover and archive.
- [x] Implement usage sidecar and promotion.
- [x] Add `--no-memory`.
- [x] Add `tests/test_memory.py`.
- [x] Run M1–M6 evidence.

## Task 2 — AGENTS.md

- [x] Add upward discovery with AGENTS.md and CLAUDE.md fallback.
- [x] Add 500-line cap and truncation note.
- [x] Inject "Project instructions" preamble section.
- [x] Add `--no-agents`, `/agents`, `/agents-reload`.
- [x] Add generator, `/agents-init`, and `--generate-agents`.
- [x] Run A1–A5 evidence.

## Task 3 — Skills

- [x] Implement skill discovery/frontmatter parsing.
- [x] Add 80-char description cap, 30-skill cap, warning.
- [x] Add `list_skills`, `use_skill`, `read_skill_file`.
- [x] Add one-turn skill body loading and path traversal rejection.
- [x] Run S1–S5 evidence.

## Task 4 — Default Skills

- [x] Add `examples/skills/skill-creator/`.
- [x] Add `examples/skills/review/`.
- [x] Add `examples/skills/agents-init/`.
- [x] Run D1–D4 evidence.

## Task 5 — Preamble Assembly

- [x] Assemble base + skills + memory + AGENTS sections with blank-line separators.
- [x] Add `--show-preamble`.
- [x] Run P1–P4 context-budget evidence.

## Task 6 — Docs and Cleanup

- [x] Update README.
- [x] Update `.gitignore`.
- [x] Delete temporary fixtures.
- [x] Audit dependencies.
- [x] Write `CONTEXT_LAYER_REPORT.md`.
- [x] Tick this checklist.
- [x] Commit locally.
