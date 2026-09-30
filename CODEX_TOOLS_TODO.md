# Codex-Style Tool Layer — Living To-Do List

Last updated: 2026-09-30

## Preflight

- [x] Read the current repository tree and status.
- [x] Read `deepseek_cli.py`, `deepseek/tools.py`, `deepseek/extensions.py`, and `deepseek/client.py`.
- [x] Read `CLI_CONVERSION_REPORT.md` and `ToDo_List.md`.
- [x] Check official OpenAI Codex CLI documentation and installed `codex-cli` behavior.
- [x] Create and maintain this checklist.

## Task 1 — Codex Default Tool Set

- [x] Extend `Tool` with `deferred`, `eager`, and `read_only` metadata; update `@tool`.
- [x] Create `deepseek/agent_tools.py`.
- [x] Implement `exec_command` with PTY and plain-pipe execution, workdir, yield time, output caps, and session ids.
- [x] Implement `write_stdin` with a thread-safe session registry.
- [x] Implement `apply_patch` for Update File, Add File, Delete File, exact context matching, and malformed-input rejection.
- [x] Implement `update_plan` with validation, checklist rendering, persistence, and completion archival.
- [x] Implement `request_user_input` with 1–3 structured questions, numbered options, and free-text fallback.
- [x] Implemented, then removed as dead weight in the later polish pass: `view_image` metadata stub.
- [x] Move legacy file tools to an off-by-default `--legacy-tools` tier.

## Task 2 — Deferred Tool Loading

- [x] Create `deepseek/tool_search.py`.
- [x] Implement pure-stdlib BM25 (`k1=1.5`, `b=0.75`).
- [x] Build and cache an index over name, description, and parameter names.
- [x] Mark extension tools deferred by default; honor `eager=True`.
- [x] Add always-visible `search_tools`.
- [x] Add exact-name bias before BM25 results.
- [x] Scope matched tools to the next sampling turn only.
- [x] Update the preamble to show only eager tools plus `search_tools`.

## Task 3 — Auto-Compaction

- [x] Create `deepseek/compaction.py`.
- [x] Improve prompt/reply token estimation and per-session accumulation.
- [x] Add `--compact-at <tokens>` and `DEEPSEEK_COMPACT_AT`.
- [x] Trigger compaction before a new user turn above threshold.
- [x] Summarize with the required lossless-brief prompt.
- [x] Save summaries under `~/.deepseek-cli/summaries/`.
- [x] Create a new DeepSeek session and prepend the summary to its first message.
- [x] Preserve the active plan across compaction.
- [x] Warn after more than three compactions.
- [x] Add `/compact`.

## Task 4 — Persistent Plan Tracking

- [x] Create `deepseek/plan_store.py`.
- [x] Store plans under `~/.deepseek-cli/plans/<conversation_id>.json`.
- [x] Archive completed plans under `plans/completed/`.
- [x] Reload and inject active plans on resume.
- [x] Add `/plan`, `/plan clear`, and `/plan resume`.
- [x] Enable `update_plan` in autonomous mode (`--tools auto` or `--mode agent`).
- [x] Add the plan contract to the system preamble.

## Task 5 — Codex-Style Preamble

- [x] Replace the current tool preamble with the Codex-style structure.
- [x] Include role, personality, preambles, visible-tool list, discovery note, plan guidance, shell rules, apply_patch rule, and editing discipline.
- [x] Keep the `<tool_call>` wrapper and `TOOL RESULT` convention.

## Task 6 — Multi-Tool-Call Execution

- [x] Classify read-only, mutating, and metadata calls.
- [x] Batch read-only calls with `ThreadPoolExecutor(max_workers=4)`.
- [x] Execute mutating calls sequentially.
- [x] Preserve result order matching request order.
- [x] Batch manual approval prompts before execution.

## CLI Wiring and Documentation

- [x] Add `--legacy-tools`, `--compact-at`, `--plan-mode`, and `--mode {normal,agent}`.
- [x] Wire dynamic visible tools into the model loop.
- [x] Render plans as `☐ / ◐ / ☑` checklists.
- [x] Add `/compact`, `/plan`, `/plan clear`, and `/plan resume`.
- [x] Wire plan reload/display on `--resume`.
- [x] Update `README.md`.
- [x] Write `CODEX_TOOLS_REPORT.md`.
- [x] Add regression tests for new modules.
- [x] Finalize this checklist.

## End-to-End Tests

- [x] E1: `exec_command` runs `ls` under `--tools auto`.
- [x] E2: `exec_command` respects `workdir=/tmp`.
- [x] E3: interactive `python3` via `exec_command` + `write_stdin` returns `4`.
- [x] E4: `apply_patch` Update changes a test file.
- [x] E5: `apply_patch` Add creates a file.
- [x] E6: `apply_patch` Delete removes a file.
- [x] E7: malformed `apply_patch` returns an error and touches nothing.
- [x] E8: `update_plan` renders a checklist in autonomous mode.
- [x] E9: plan persists across CLI restart and `--resume`.
- [x] E10: `request_user_input` prompt/answer works in plan mode.
- [x] E11: default preamble contains no deferred extension tool names.
- [x] E12: weather request calls `search_tools`, then `get_weather`, then answers.
- [x] E13: exact-name search finds `get_weather`.
- [x] E14: matched deferred tools revert to hidden after one turn.
- [x] E15: `DEEPSEEK_COMPACT_AT=5000` triggers compaction.
- [x] E16: compaction summary is written under `~/.deepseek-cli/summaries/`.
- [x] E17: conversation id changes after compaction.
- [x] E18: summary is prepended to the new session's first message.
- [x] E19: `/compact` manual trigger behaves like auto compaction.
- [x] E20: active plan survives compaction.
- [x] E21: autonomous multi-step task writes a plan/ToDo.
- [x] E22: Ctrl+C mid-task and resume reloads the plan.
- [x] E23: `/plan`, `/plan clear`, and `/plan resume` behave correctly.
- [x] E24: prior T1–T20 still pass.
- [x] E25: existing regression tests still pass.
- [x] E26: `--json` still emits valid tool_call/tool_result/answer JSONL.

## Discovered During Work

- [x] Add new bugs or follow-up work here as they are found.
