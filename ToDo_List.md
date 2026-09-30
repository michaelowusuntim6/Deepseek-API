# DeepSeek CLI Conversion — Living To-Do List

Last updated: 2026-09-30

## Preflight

- [x] Read the repository tree and identify all front-end, core, server, example, and report files.
- [x] Read `deepseek/` core modules (`auth.py`, `client.py`, `extensions.py`, `tools.py`, `pow.py`).
- [x] Read `deepseek_tui.py`, `server/`, `examples/`, `tests/`, `README.md`, and all existing reports.
- [x] Confirm runtime/dependency state (`venv/bin/python`, Rich, Textual, Playwright, session age).
- [x] Create and maintain this living checklist.

## Core Bug Fixes

- [x] B1: `deepseek/tools.py` — unknown tool names return an error string instead of raising `KeyError`.
- [x] B2: `deepseek/extensions.py` / CLI dispatch — extension `COMMANDS` receive an app/emit callback and can print.
- [x] B3: `deepseek/auth.py` / `deepseek/client.py` — pre-turn session freshness and transparent refresh.
- [x] B4: `deepseek/client.py` — parse all `<tool_call>` blocks and represent multi-call turns with `Reply.tool_calls`.
- [x] B5: `deepseek/client.py` — stop re-sending the unchanged tool preamble on later turns of the same session.
- [x] B6: `deepseek/extensions.py` — remove or implement the dead `seen_names` branch; document the choice.
- [x] Add regression coverage for every bug fixed above.

## CLI Implementation

- [x] Design `deepseek_cli.py` architecture: Rich UI, event model, streaming, tool loop, REPL, exit codes.
- [x] Implement Codex-style startup banner, info grid, rotating tip, conversation stream, footer, and prompt.
- [x] Implement one-shot invocation: `python deepseek_cli.py "prompt"`.
- [x] Implement piped-stdin invocation: `echo "prompt" | python deepseek_cli.py`.
- [x] Implement interactive REPL invocation: `python deepseek_cli.py`.
- [x] Implement flags: `--model`, `--thinking`, `--search`, `--tools`, `--show-thinking`, `--json`, `--resume`, `--no-stream`.
- [x] Implement exit codes: `0` success, `1` runtime error, `2` auth required, `3` usage error.
- [x] Implement streaming answers by default with hidden thinking unless `--show-thinking`.
- [x] Implement JSONL mode with one object per event on stdout.
- [x] Implement tool-call visibility and manual/auto/off semantics.
- [x] Implement slash commands: `/help`, `/new`, `/thread`, `/clear`, `/model`, `/thinking`, `/search`, `/mode`, `/tools`, `/extensions`, `/reload`, `/exit`.
- [x] Implement slash command Tab completion with `readline`, without prompt_toolkit/Textual.
- [x] Implement extension command dispatch with emitted output capture.
- [x] Implement live context/token estimate and short conversation id display.
- [x] Implement clean Ctrl+C handling during streaming and at the prompt.
- [x] Implement network/auth error handling without tracebacks.
- [x] Move built-in tools out of the deleted TUI and wire them into the CLI.

## Repository Cleanup and Documentation

- [x] Delete `deepseek_tui.py`.
- [x] Delete or rewrite Textual-specific tests (`tests/e2e_test.py`, `tests/test_tui_tools.py`).
- [x] Add CLI-focused regression/e2e tests that do not depend on Textual.
- [x] Update `requirements.txt` to remove Textual, if present, and make Rich explicit.
- [x] Update `README.md`: replace TUI usage with CLI usage, slash commands, command-line reference, project layout.
- [x] Update extension examples/docs for callable commands that can emit output.
- [x] Write `CLI_CONVERSION_REPORT.md` with architecture, bug fixes, tests, limitations, and quick start.
- [x] Finalize this checklist, marking every item complete or explicitly abandoned.

## End-to-End Tests

- [x] T1: `python deepseek_cli.py --help` prints usage and exits 0.
- [x] T2: `echo "hi" | python deepseek_cli.py` returns a reply and exits 0.
- [x] T3: `python deepseek_cli.py "say hello in one word"` returns a one-shot reply and exits 0.
- [x] T4: `python deepseek_cli.py --json "hi"` emits valid JSONL on stdout.
- [x] T5: `python deepseek_cli.py --thinking "2+2"` runs DeepThink.
- [x] T6: `python deepseek_cli.py --show-thinking --thinking "2+2"` shows the trace.
- [x] T7: `python deepseek_cli.py --search "latest news about X"` runs the search path.
- [x] T8: `python deepseek_cli.py --model expert "hi"` wires the expert model.
- [x] T9: `python deepseek_cli.py --tools off "list files"` attempts no tool.
- [x] T10: `python deepseek_cli.py --tools manual "what is the weather in Tokyo"` prompts for approval.
- [x] T11: `python deepseek_cli.py --tools auto "what is the weather in Tokyo"` runs the tool unattended.
- [x] T12: REPL launch, send prompt, `/thread`, `/new`, `/exit`.
- [x] T13: REPL `/help`, `/model`, `/thinking`, `/search`, `/mode`, `/tools`.
- [x] T14: REPL `/extensions`, `/reload`, and an extension slash command.
- [x] T15: `--resume <conversation_id>` retains context.
- [x] T16: extension tool invocation from the model (`get_weather` via `--tools auto`).
- [x] T17: session-expiry simulation recovers or reports clearly.
- [x] T18: Ctrl+C during streaming exits cleanly without traceback.
- [x] T19: network failure mid-request is caught without a stack trace.
- [x] T20: invalid flags/flag combinations produce argparse errors and exit 3.
- [x] Record exact command and observed outcome for T1–T20 in the report.

## Discovered During Work

- [x] N1: `--search` produced an empty answer because nested `BATCH` SSE frames were ignored; fixed and verified live.
- [x] N2: JSON `tool_result` events omitted the `result` field; fixed and covered by regression test.
