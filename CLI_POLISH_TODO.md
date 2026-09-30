# DeepSeek CLI Polish — Living To-Do List

Last updated: 2026-09-30

## Preflight

- [x] Read current tree and git status.
- [x] Read `deepseek_cli.py`, `deepseek/client.py`, `deepseek/tools.py`, `deepseek/agent_tools.py`, `README.md`, and `.gitignore`.
- [x] Read `CODEX_TOOLS_REPORT.md`.
- [x] Create and maintain this checklist.

## Task 1 — BrokenPipeError

- [x] Reproduce BrokenPipeError with `--json --tools auto ... | head`.
- [x] Catch top-level BrokenPipeError, dup fd 1 to `/dev/null`, return 141.
- [x] Ensure `emit_event()` re-raises BrokenPipeError instead of swallowing it.
- [x] Add subprocess regression test for early pipe close.
- [x] Verify P1/P2/P10.

## Task 2 — Remove `view_image`

- [x] Delete `view_image` implementation from `deepseek/agent_tools.py`.
- [x] Remove it from registry/default tools and docs.
- [x] Amend old reports to note removal.
- [x] Add/adjust tests so no removed functionality is referenced.

## Task 3 — Image viewer extension

- [x] Add `rich-pixels` to `requirements.txt`.
- [x] Add `examples/extensions/image_view.py` with deferred `show_image`.
- [x] Add fixture generation for `tests/fixtures/test_image.png`.
- [x] Add `tests/test_image_view.py` offline regression.
- [x] Verify P9.
- [x] Document future API vision path in report.

## Task 4 — Streaming markdown

- [x] Implement line-buffered Markdown rendering for TTY streaming.
- [x] Flush trailing line through Markdown at stream end.
- [x] Skip Markdown for non-TTY and JSON.
- [x] Add `--no-markdown`.
- [x] Verify P3/P4/P5 and no ANSI in JSON.

## Task 5 — Diff rendering for `apply_patch`

- [x] Capture original file content before applying patches.
- [x] Build unified diffs for updated/added/deleted files.
- [x] Render Rich diff panel on TTY.
- [x] Emit JSON `file_change` events in JSON mode.
- [x] Print plain diff text in piped mode.
- [x] Add `--no-diff`.
- [x] Verify P6/P7.

## Task 6 — Batched approvals

- [x] Render all pending tool calls once, numbered.
- [x] Prompt per call with `y`, `n`, `a`, `A`, `q` semantics.
- [x] Execute only after approvals are resolved.
- [x] Preserve read-only parallel and mutating sequential ordering.
- [x] Verify P8.

## Task 7 — AX hardening

- [x] Add JSON `help` event for `--json --help`.
- [x] Confirm all error paths emit JSON `error`.
- [x] Add JSON `compaction` event.
- [x] Add JSON `plan` event.
- [x] Add JSON `file_change` event.
- [x] Audit exit codes: 0/1/2/3/141.
- [x] Audit stdout/stderr discipline.

## Task 8 — Repo cleanup

- [x] Delete superseded report files.
- [x] Delete only tests referencing removed functionality.
- [x] Remove temporary test extensions from `~/.deepseek-tui/extensions`.
- [x] Rewrite `.gitignore` with required entries.
- [x] Audit `pip freeze` against `requirements.txt`.

## Task 9 — Documentation

- [x] Add README "What's new".
- [x] Document `--no-markdown` and `--no-diff`.
- [x] Add "Diff rendering" section.
- [x] Add "Streaming markdown" section.
- [x] Add "Image viewing" extension section.
- [x] Update extension docs for `image_view.py`.
- [x] Add BrokenPipeError troubleshooting.
- [x] Update exit codes table with 141.

## Final Tests

- [x] P1: `--json "hi" | head -1` returns clean JSON and exit 0.
- [x] P2: `--json --tools auto "search_tools for get_weather" | head -5` exits 141 with no traceback.
- [x] P3: one-shot plain text works.
- [x] P4: interactive markdown renders progressively.
- [x] P5: JSON output has no ANSI escapes.
- [x] P6: apply_patch diff panel renders.
- [x] P7: JSON `file_change` event appears.
- [x] P8: manual approvals batch.
- [x] P9: image extension renders block characters.
- [x] P10: `--json "hi" > /dev/null` exits 0.
- [x] P11: `tests/test_cli.py` passes.
- [x] P12: `tests/test_image_view.py` passes.
- [x] P13: `tests/test_cli_tools.py` passes.
- [x] P14: `tests/test_extensions.py` passes.
- [x] P15: `git status --short` has no fixture images, session, `.deepseek-cli`, or `__pycache__`.

## Report and Commit

- [x] Write `CLI_POLISH_REPORT.md` with P1–P15 evidence, bugs, limitations, quick start.
- [x] Update README and `.gitignore`.
- [x] Stage all intended changes.
- [x] Verify staged diff/stat.
- [x] Commit locally with the requested message.
