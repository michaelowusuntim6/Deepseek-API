# DeepSeek CLI Conversion Report

Date: 2026-09-30

## 1. Summary

The Textual TUI in `deepseek_tui.py` has been replaced by `deepseek_cli.py`, a
Rich-based CLI modeled on Codex CLI. The new CLI supports one-shot prompts,
piped stdin, an interactive REPL, streaming output, JSONL events, tool
approval modes, extension commands, session refresh, and clean exit codes.

The old TUI and its Textual-specific tests were deleted. Textual was not listed
in `requirements.txt`; it has not been retained, and Rich is now explicit as the
direct terminal-UI dependency.

Verified live against the saved DeepSeek session while it was still fresh
(under 6 hours). The session refresh path was additionally forced with a
zero-age threshold and recovered successfully.

## 2. What Was Built

### Architecture

`deepseek_cli.py` is the only new front-end entry point. It contains:

- `UsageErrorParser` and `build_parser()` for argparse with exit code `3`.
- `DeepSeekCLI`, a state holder for model, DeepThink, search, tool mode,
  conversation id, context estimate, Rich consoles, and extension commands.
- Built-in tools (`read_file`, `list_dir`, `write_file`, `run_shell`,
  `edit_file`, `grep`, `find_files`, `fetch_url`) plus extension tools.
- A central `emit_event()` renderer shared by one-shot and REPL modes.
- JSONL output mode where stdout contains only event objects.
- A `readline`-backed REPL loop with slash-command completion.

### Rich UI

Interactive startup renders:

- A bordered banner with `deepseek-cli v0.3.0` and the active model.
- A compact session panel: Model, Directory, Permissions, DeepThink, Web search,
  Tool mode, Thread, and Context estimate.
- A dim tip line.
- A dim status footer before each prompt.

Conversation output is visually separated:

- User input appears at the `>` prompt.
- Assistant output streams under a bold `Assistant` label.
- Thinking streams under a dim `Thinking` label only with `--show-thinking`.
- Tool calls render as `tool <name> <arguments>`.
- Tool results render as `result <name> <first line>`.

### CLI Modes and Flags

Supported invocation modes:

```bash
python deepseek_cli.py "prompt"
echo "prompt" | python deepseek_cli.py
python deepseek_cli.py
```

Supported flags:

```text
--model {chat,expert}
--thinking
--search
--tools {off,manual,auto}
--show-thinking
--json
--resume <conversation_id>
--no-stream
```

Exit codes:

- `0` success
- `1` runtime error
- `2` auth required
- `3` usage error
- `130` interrupted with Ctrl+C (documented extension of the original set)

### JSONL Events

With `--json`, stdout contains one JSON object per line:

```json
{"kind": "answer", "text": "..."}
{"kind": "thinking", "text": "..."}
{"kind": "tool_call", "name": "get_weather", "arguments": {"city": "Tokyo"}}
{"kind": "tool_result", "name": "get_weather", "result": "18.7°C in Tokyo, Japan..."}
{"kind": "error", "message": "..."}
```

Human chrome and manual approval prompts go to stderr in JSON mode.

### Extension Commands

`COMMANDS` dispatch now supports:

- App instance:

```python
def show(app):
    app.emit("hello")

COMMANDS = {"/show": show}
```

- Emit callback:

```python
def show(emit):
    emit("hello")

COMMANDS = {"/show": show}
```

- Legacy zero-argument callables; their stdout is captured and displayed.

## 3. Bugs Fixed

### B1 — Unknown tool crashed the request

File: `deepseek/tools.py:179`

`execute_tool()` checked for the tool name before entering the `try` block, so a
hallucinated tool name raised `KeyError`. It now returns:

```text
Error: tool 'nope' is not registered. Available tools: read_file
```

Regression test: `tests/test_cli.py::test_unknown_tool_returns_error`.

### B2 — Extension commands could not emit UI output

Files: `deepseek_cli.py:962`, `examples/extensions/README.md`,
`examples/extensions/notes.py`

The CLI dispatch now accepts zero-argument commands, app-instance commands, and
emit-callback commands. Output printed by legacy commands is captured and shown.

Regression test: `tests/test_cli.py::test_extension_command_contexts`, plus live
REPL verification in T14.

### B3 — Expiring sessions were only checked once

File: `deepseek/client.py:182`

Added a pre-turn freshness check in `DeepSeekClient`. It runs before session
creation, before PoW challenge fetch, and before completion streaming:

- `deepseek/client.py:262`
- `deepseek/client.py:268`
- `deepseek/client.py:484`

When the session exceeds `deepseek.auth.SESSION_MAX_AGE`, the client asks
`get_session()` for a fresh session, rebuilds its HTTP client with the new
headers/cookies, and calls the optional `on_session_refresh` callback so the CLI
can tell the user. Login failures surface clearly instead of as raw HTTP errors.

Chosen approach: pre-turn check rather than a background thread. It is simpler,
deterministic, and does not race with Playwright’s sync API.

### B4 — Multi-call turns were dropped

Files: `deepseek/client.py:100`, `deepseek/client.py:112`,
`deepseek/client.py:464`, `deepseek/client.py:481`, `deepseek/client.py:532`

`Reply` and `_Stream` now expose `tool_calls: list[ToolCall]`. `answer`,
`chat_with_tools()`, and `stream_with_tools()` process every parsed call.
`tool_call` is retained as a compatibility alias for the first call, so the
OpenAI-compatible server and older examples continue to work.

Regression tests:

- `tests/test_cli.py::test_parse_sse_collects_multiple_tool_calls`
- `tests/test_cli.py::test_reply_tool_calls_list`
- `tests/test_cli.py::test_stream_with_tools_executes_all_calls`

### B5 — Tool preamble was re-sent on later turns

Files: `deepseek/client.py:225`, `deepseek/client.py:238`

The client now stores a SHA-256 fingerprint of the tool schema per DeepSeek
session id. On later turns in the same session, unchanged tool definitions are
not re-sent. If tools change, the preamble is sent again.

Regression test: `tests/test_cli.py::test_tool_preamble_cached_per_session`.

### B6 — Dead `seen_names` branch

File: `deepseek/extensions.py:52`

The dead `if name in seen_names: pass` branch was deleted. Tool-name collision
handling remains in `_load_all()`, where later paths intentionally win and emit
a warning.

### New Bug N1 — Search responses produced no answer

File: `deepseek/client.py:652`

Live `--search` testing exposed that DeepSeek registers search answers inside a
`BATCH` SSE envelope. The parser ignored nested `fragments` operations, so the
`RESPONSE` fragment was never registered and the answer stayed attached to the
`SEARCH` fragment, hidden as thinking.

Fixed by handling `BATCH` envelopes before normal frame dispatch. Subsequent
content deltas now map to the real answer fragment.

Recovered live behavior: `--search "latest news about X"` produced a 3,266-byte
answer and exited 0.

### New Bug N2 — JSON `tool_result` omitted the result string

File: `deepseek_cli.py:484`

JSON mode emitted `{"kind":"tool_result","name":"get_weather"}` without
`result`. Fixed to emit:

```json
{"kind": "tool_result", "name": "get_weather", "result": "18.7°C in Tokyo, Japan..."}
```

Regression test: `tests/test_cli.py::test_json_tool_result_event_shape`.

## 4. T1–T20 Verification

All commands below were run against the live saved DeepSeek session using
`venv/bin/python` (equivalent to `python` after activating the project venv).
Outputs are abbreviated where noted.

### T1 — Help

Command:

```bash
venv/bin/python deepseek_cli.py --help
```

Observed: usage printed with all flags; exit code `0`.

### T2 — Piped stdin

Command:

```bash
echo "hi" | venv/bin/python deepseek_cli.py
```

Observed stdout:

```text
Assistant
Hi! How can I help you today?
```

Exit code `0`.

### T3 — One-shot

Command:

```bash
venv/bin/python deepseek_cli.py "say hello in one word"
```

Observed stdout:

```text
Assistant
Hello
```

Conversation id: `f5c80658-4bf0-4a11-b3e1-7a97cacceafb:2`. Exit code `0`.

### T4 — JSON mode

Command:

```bash
venv/bin/python deepseek_cli.py --json "hi"
```

Observed stdout:

```json
{"kind": "answer", "text": "Hi! How can I help you today?"}
```

stderr was empty; stdout was valid JSONL; exit code `0`.

### T5 — DeepThink

Command:

```bash
venv/bin/python deepseek_cli.py --thinking "2+2"
```

Observed stdout:

```text
Assistant
4
```

Exit code `0`; conversation id was returned.

### T6 — Visible thinking trace

Command:

```bash
venv/bin/python deepseek_cli.py --show-thinking --thinking "2+2"
```

Observed stdout:

```text
Assistant
4
```

Observed stderr included:

```text
Thinking
We need answer normal. 2+2=4. No tool.
```

Exit code `0`.

### T7 — Web search

Command:

```bash
venv/bin/python deepseek_cli.py --search "latest news about X"
```

Observed stdout began:

```text
Assistant
Based on the latest information, here are the key news stories about X...
```

Stdout was 3,266 bytes. Exit code `0`.

### T8 — Expert model

Command:

```bash
venv/bin/python deepseek_cli.py --model expert "hi"
```

Observed banner and session panel showed `deepseek-expert`; answer was
`Hello! How can I help you today?`; exit code `0`.

### T9 — Tools off

Command:

```bash
venv/bin/python deepseek_cli.py --tools off "list files"
```

Observed answer explained that it had no filesystem access and recommended
manual commands. No tool-call events appeared. Exit code `0`.

### T10 — Manual tool approval

Command used, with `y` supplied on stdin for the approval prompt:

```bash
printf 'y\n' | venv/bin/python deepseek_cli.py --tools manual "what is the weather in Tokyo"
```

Observed approval prompt:

```text
Approve get_weather({"city": "Tokyo"})? [y]es / [n]o / [a]lways:
```

Observed tool events:

```text
tool get_weather {"city": "Tokyo"}
  result get_weather 18.7°C in Tokyo, Japan · wind 5.5 km/h · WMO code 3
```

Final answer reported `18.7°C`, overcast skies. Exit code `0`.

### T11 — Auto tool execution

Command:

```bash
venv/bin/python deepseek_cli.py --tools auto "what is the weather in Tokyo"
```

Observed no approval prompt, `get_weather` executed, result rendered, final
answer reported `18.7°C`. Exit code `0`.

### T12 — Interactive prompt, `/thread`, `/new`, `/exit`

Command:

```bash
venv/bin/python deepseek_cli.py
```

In a PTY, sent `say hello in one word`:

```text
Assistant
Hello
```

`/thread` printed:

```text
conversation_id = b1d09885-f877-4ee7-8073-de2c82250e4c:2
```

`/new` printed `Started a new thread.` and `/thread` then printed `(none yet)`.
`/exit` quit cleanly.

### T13 — Interactive slash commands

In the same REPL, verified:

```text
/help       rendered the command table
/model      showed available aliases
/mode       set manual/auto mode
/tools      listed and set off/manual/auto
/thinking   toggled on/off
/search     toggled on/off
```

All printed confirmation lines and updated the status footer.

### T14 — Extensions, reload, extension command

A temporary extension was placed in the real extension search path:

```text
/home/mike/.deepseek-tui/extensions/cli_emit_test.py
```

It registered `/emit_test`, `/app_test`, and `/old_test`.

Observed:

```text
/extensions  listed the extension, tools, and extension commands
/emit_test   extension emit callback works
/app_test    extension app context works
/old_test    extension print capture works
/reload      Extensions reloaded.
```

The temporary extension was removed after verification.

### T15 — Resume retains context

Commands:

```bash
venv/bin/python deepseek_cli.py --tools off "Remember this word: nebula. Reply with OK."
cid=$(sed -n 's/^conversation_id = //p' /tmp/t15a.err | tail -1)
venv/bin/python deepseek_cli.py --tools off --resume "$cid" "What word did I ask you to remember? Reply with only that word."
```

Observed first answer:

```text
OK
```

Observed resumed answer:

```text
nebula
```

Exit code `0`.

### T16 — Extension tool invocation from model

Covered by T10 and T11. The `get_weather` tool is provided by
`examples/extensions/weather.py` installed in `~/.deepseek-tui/extensions/`.
The model requested it, the CLI executed it in auto mode, and the result was fed
back into the final answer.

### T17 — Session expiry simulation

Scratch script:

```python
import deepseek.auth as auth
from deepseek.client import DeepSeekClient

auth.SESSION_MAX_AGE = 60.0
notes = []
client = DeepSeekClient(session_max_age=60.0, on_session_refresh=notes.append)
reply = client.chat("Reply with exactly OK.", model="default")
print("refreshed_notes=", notes)
print("age_now_seconds=", round(client.session.age, 2))
print("answer=", repr(reply.text.strip()))
client.close()
```

Observed:

```text
refreshed_notes= ['DeepSeek session refreshed.']
age_now_seconds= 2.92
answer= 'OK'
```

This demonstrates the realistic sequence: the old session was past the tiny
threshold, refreshed once, updated its captured age, and then completed the
request normally.

### T18 — Ctrl+C during streaming

Command:

```bash
venv/bin/python deepseek_cli.py --tools off --thinking --search "write a 1000-word essay about the history of computing"
```

Immediately sent Ctrl+C in the PTY.

Observed:

```text
^Cerror: interrupted
```

Exit code `130`, no traceback.

### T19 — Network failure mid-request

Simulated with an `httpx.ReadError` raised from the stream iterator, which is
the same exception class used when a connection drops mid-stream:

```python
import httpx
from deepseek_cli import DeepSeekCLI, build_parser

class BadStream:
    conversation_id = None
    def iter_parts(self):
        raise httpx.ReadError("network cable pulled mid-stream")

class BadClient:
    def stream(self, *a, **kw):
        return BadStream()
    def close(self):
        pass

parser = build_parser()
args = parser.parse_args(["--json", "--tools", "off", "hello"])
app = DeepSeekCLI(args, parser, interactive=False, prompt="hello")
app.client = BadClient()
print("exit_code", app.run_prompt("hello"))
```

Observed stdout:

```json
{"kind": "error", "message": "ReadError: network cable pulled mid-stream"}
exit_code 1
```

No traceback.

### T20 — Usage errors

Command:

```bash
venv/bin/python deepseek_cli.py --model nonsense "hi"
```

Observed:

```text
deepseek_cli.py: error: argument --model: invalid choice: 'nonsense' (choose from chat, expert)
```

Exit code `3`.

Also verified:

```bash
venv/bin/python deepseek_cli.py --resume abc:1 --model expert "hi"
```

Observed:

```text
deepseek_cli.py: error: --model cannot be combined with --resume; a thread's model is fixed
```

Exit code `3`.

## 5. Offline Test Suites

All offline suites were rerun after the conversion:

```bash
PYTHONPATH=. venv/bin/python tests/test_cli.py
PYTHONPATH=. venv/bin/python tests/e2e_test.py
PYTHONPATH=. venv/bin/python tests/test_cli_tools.py
PYTHONPATH=. venv/bin/python tests/test_extensions.py
PYTHONPATH=. venv/bin/python tests/test_server_tools.py
```

Observed:

- CLI regression tests: 7 passes.
- CLI e2e smoke tests: 3 passes.
- CLI built-in tool tests: 4 groups pass.
- Extension tests: `9 passed, 0 failed`.
- Server tests: `all offline tests passed` and `Phase 2 TestClient mock tests passed`.

## 6. Files Changed

- Added `deepseek_cli.py` — Rich CLI.
- Deleted `deepseek_tui.py` — Textual TUI removed.
- Deleted `tests/test_tui_tools.py`, rewrote `tests/e2e_test.py`.
- Added `tests/test_cli.py` and `tests/test_cli_tools.py`.
- Fixed `deepseek/tools.py`, `deepseek/extensions.py`, `deepseek/client.py`.
- Updated `requirements.txt` — confirmed Textual is absent; added explicit Rich.
- Updated `README.md` — CLI usage, command-line reference, slash-command table,
  project layout.
- Updated extension examples/docs for emit-aware commands.
- Added `ToDo_List.md` and this report.

## 7. What Was Not Done and Why

- The OpenAI-compatible server was not modified. The new `Reply.tool_call`
  compatibility alias keeps it working, but its OpenAI response shape still
  returns only the first tool call. That limitation is pre-existing and outside
  this conversion’s server no-change constraint.
- No background session refresh thread was added; the deterministic pre-turn
  freshness check was chosen instead.
- No network update check is performed at startup. An update notice is displayed
  only if `DEEPSEEK_CLI_UPDATE_NOTICE` is set.
- Manual tool approval needs an attached terminal or a piped `y`/`n`/`a`
  response. With no input available, the CLI treats the tool as rejected.

## 8. Known Limitations

- Streaming answers are rendered as plain styled text, not re-rendered as
  Markdown after completion. Buffered `--no-stream` answers are also plain.
- The context estimate is approximate (`characters / 4`), not a server-reported
  token count.
- `--json` with an interactive REPL is rejected as a usage error; JSON mode is
  intended for one-shot or piped prompts.
- The tool preamble cache is in-memory per client process. A new process that
  resumes an existing thread must send the preamble once because it has no prior
  fingerprint for that server-side session.

## 9. First 5 Commands to Try

```bash
venv/bin/python deepseek_cli.py "say hello in one word"
echo "hi" | venv/bin/python deepseek_cli.py
venv/bin/python deepseek_cli.py --thinking --show-thinking "2+2"
venv/bin/python deepseek_cli.py --tools auto "what is the weather in Tokyo"
venv/bin/python deepseek_cli.py
```

Inside the REPL, type `/help` to see every slash command.
