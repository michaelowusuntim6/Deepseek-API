# Codex Tool Layer Report

Date: 2026-09-30

## 1. Summary

The DeepSeek CLI now has a Codex-style agent layer on top of the Rich CLI:

- `deepseek/agent_tools.py` adds `exec_command`, `write_stdin`, `apply_patch`,
  `update_plan`, `request_user_input`, PTY/pipe session tracking,
  and ordered multi-tool execution.
- `deepseek/tool_search.py` adds pure-stdlib BM25 deferred-tool search with
  exact-name bias and one-turn visibility.
- `deepseek/compaction.py` adds client-side context compaction with summary
  persistence and session restart.
- `deepseek/plan_store.py` adds persistent plan files keyed by stable DeepSeek
  session id.
- `deepseek_cli.py` wires these into the default tool set, agent loop, slash
  commands, JSONL events, and startup status.
- `deepseek/client.py` now parses both `<tool_call>` JSON and DeepSeek's native
  fullwidth-pipe DSML tool-call markup.

No server changes were made. No new pip dependencies were added.

## 2. E-Test Evidence

All commands were run with `venv/bin/python` from `/home/mike/Deepseek-API`
unless noted. Outputs are abbreviated only where repeated answer text is not
material to the test.

### E1 — `exec_command` runs `ls`

Command:

```bash
venv/bin/python deepseek_cli.py --tools auto \
  "run ls in the current directory and summarize the files"
```

Observed stdout:

```text
Assistant
I'll list the current directory contents.

Assistant
This is a Python project directory for a DeepSeek CLI tool.

**Key files:**
- `deepseek_cli.py` (51KB) — main CLI entry point, the largest and most recently modified source file
- `app.py` — small entry script
- `test_sdk.py` — SDK tests
...
```

Observed tool lines:

```text
tool exec_command {"cmd": "ls -la"}
  result exec_command total 204
conversation_id = 6f35562c-9c9c-4721-8738-83513ef8bc85:4
```

### E2 — `exec_command` honors `workdir`

Command:

```bash
venv/bin/python deepseek_cli.py --tools auto \
  "Use exec_command with workdir=/tmp to run pwd and report the output."
```

Observed:

```text
tool exec_command {"cmd": "pwd", "workdir": "/tmp"}
  result exec_command /tmp
conversation_id = 19918a57-0e29-4c4c-a292-e8dfcaabd667:4
```

Final answer: `The output is /tmp.`

### E3 — interactive Python through `write_stdin`

Command:

```bash
venv/bin/python deepseek_cli.py --tools auto \
  "Launch an interactive python3 shell using exec_command with tty=true. Then use write_stdin to send the characters 'print(2+2)\n' to that session and report the result. Finally send 'exit()\n'."
```

Observed tool lines:

```text
tool exec_command {"cmd": "python3", "tty": true, "yield_time_ms": 1000}
  result exec_command Python 3.14.4 ... on linux
tool write_stdin {"session_id": "sess_2e3d19bef89f", "chars": "print(2+2)\n", "yield_time_ms": 1000}
  result write_stdin ...4...
tool write_stdin {"session_id": "sess_2e3d19bef89f", "chars": "exit()\n", "yield_time_ms": 1000}
  result write_stdin ...[process exited with code 0]
```

Final answer confirmed the output was `4`.

### E4 — `apply_patch` Update

Command:

```bash
printf 'alpha\nbeta\ngamma\n' > /tmp/codex_patch_update.txt
venv/bin/python deepseek_cli.py --tools auto \
  "Use apply_patch to change the line 'beta' to 'BETA' in /tmp/codex_patch_update.txt. Do not use any other edit tool."
```

Observed file:

```text
alpha
BETA
gamma
```

Observed tool lines:

```text
tool apply_patch {"patch": "*** Begin Patch\n*** Update File: /tmp/codex_patch_update.txt\n@@\n-beta\n+BETA\n*** End Patch"}
  result apply_patch applied 1 hunks to 1 files
```

### E5 — `apply_patch` Add, verified 3/3

After adding a concrete Add File example to the preamble:

```text
Assistant
Done. Added `/tmp/codex_patch_added_1.txt` with `hello world`.
run 1: CREATED
Assistant
Done. Created `/tmp/codex_patch_added_2.txt` with `hello world`.
run 2: CREATED
Assistant
Done. Created `/tmp/codex_patch_added_3.txt` with `hello world`.
run 3: CREATED
```

### E6 — `apply_patch` Delete

Command:

```bash
venv/bin/python deepseek_cli.py --tools auto \
  "Use apply_patch to delete /tmp/codex_patch_added_unique2.txt."
```

Observed:

```text
tool apply_patch {"patch": "*** Begin Patch\n*** Delete File: /tmp/codex_patch_added_unique2.txt\n*** End Patch"}
  result apply_patch applied 1 hunks to 1 files
DELETED
```

### E7 — malformed `apply_patch`

Command:

```bash
printf 'guard\n' > /tmp/codex_patch_guard.txt
venv/bin/python deepseek_cli.py --tools auto \
  "Use apply_patch with the exact malformed patch text 'oops' and report the error. Do not use any other tool."
```

Observed:

```text
tool apply_patch {"patch": "oops"}
  result apply_patch Error applying patch: patch must start with *** Begin Patch
```

Guard file remained:

```text
guard
```

### E8 — `update_plan`

Command:

```bash
DEEPSEEK_CLI_HOME=/tmp/deepseek-cli-e8-final venv/bin/python deepseek_cli.py \
  --tools auto \
  "Use update_plan to create a 3-step plan for a small coding task: inspect files in_progress, make change pending, run tests pending. Then reply with a brief confirmation."
```

Observed:

```text
tool update_plan {"arguments": {"plan": [{"step": "Inspect files", "status": "in_progress"}, {"step": "Make change", "status": "pending"}, {"step": "Run tests", "status": "pending"}]}, "name": "update_plan"}
Plan
◐ Inspect files
☐ Make change
☐ Run tests
  result update_plan Plan updated:
conversation_id = 21c8566d-8204-4d84-8907-d72584471a3a:4
```

### E9 — plan persists across CLI restart

Create plan:

```bash
DEEPSEEK_CLI_HOME=/tmp/deepseek-cli-e9b venv/bin/python deepseek_cli.py \
  --tools auto \
  "Use update_plan to create a 3-step plan for a coding task: inspect code in_progress, patch code pending, run tests pending. Then reply with a brief confirmation."
```

Resume:

```bash
DEEPSEEK_CLI_HOME=/tmp/deepseek-cli-e9b venv/bin/python deepseek_cli.py \
  --tools auto --resume 39cb18e1-2630-4b0d-b672-86a153e73cd5:4 \
  "What is the current plan? Reply briefly."
```

Observed resume output:

```text
Assistant
Current plan:
◐ Inspect code (in progress)
☐ Patch code
☐ Run tests
```

Observed stderr:

```text
Plan
◐ Inspect code
☐ Patch code
☐ Run tests
Injected active plan from previous session.
```

### E10 — `request_user_input`

Command:

```bash
printf '2\n' | venv/bin/python deepseek_cli.py --plan-mode --tools manual \
  "Use request_user_input to ask one question: choose between option A, option B, and option C. After the answer, report which option was selected."
```

Observed:

```text
Choose an op: Which option would you like to select?
  1. Option A
  2. Option B
  3. Option C
Answer number or free text:
Assistant
Option B was selected.
```

Tool line:

```text
tool request_user_input {"questions": [...]}
  result request_user_input {"answers": [{"id": "choice", "answer": "Option B"}]}
```

### E11 — default preamble hides deferred tools

Command:

```bash
venv/bin/python - <<'PY'
import json
from deepseek_cli import build_tool_registry
from deepseek.client import TOOL_SYSTEM_PREAMBLE
registry = build_tool_registry(...)
schema = json.dumps([t.schema() for t in registry.visible_tools()], indent=2)
print(TOOL_SYSTEM_PREAMBLE.format(tools_schema=schema))
PY
```

Observed visible names:

```text
exec_command, write_stdin, apply_patch, search_tools, update_plan, request_user_input
```

Observed absence check:

```text
get_weather: absent
view_image: absent (later removed entirely; see CLI_POLISH_REPORT.md)
read_file: absent
list_dir: absent
find_files: absent
```

The only `grep` match was the shell guideline “Prefer rg over grep”, not a tool
schema.

### E12 — search_tools → get_weather

Command:

```bash
venv/bin/python deepseek_cli.py --tools auto "what is the weather in Tokyo"
```

Observed tool lines:

```text
tool search_tools {"query": "weather"}
  result search_tools {
tool get_weather {"city": "Tokyo"}
  result get_weather 19.0°C in Tokyo, Japan · wind 5.2 km/h · WMO code 3
conversation_id = d3e9a8d4-231e-4683-9ed3-c8739ee29726:6
```

### E13 — exact-name search

Command:

```bash
venv/bin/python deepseek_cli.py --tools auto \
  "Use search_tools with the exact query string 'get_weather' and report which tool name is returned."
```

Observed:

```text
tool search_tools {"query": "get_weather"}
  result search_tools {
Assistant
The tool returned by the exact query `get_weather` is named **`get_weather`**.
```

### E14 — deferred tool reverts after one turn

Turn 1:

```text
tool search_tools {"query": "weather"}
tool get_weather {"city": "Tokyo"}
```

Turn 2, same thread:

```text
tool search_tools {"query": "weather"}
  result search_tools {
tool get_weather {"city": "Paris"}
  result get_weather 20.1°C in Paris, France · wind 7.1 km/h · WMO code 63
```

The model explicitly said:

```text
I need to look up the weather tool again since it's only available for one turn.
```

### E15 — auto-compaction triggers

Command shape:

```bash
DEEPSEEK_COMPACT_AT=5000 DEEPSEEK_CLI_HOME=/tmp/deepseek-cli-e15b \
venv/bin/python - <<'PY'
... app.run_prompt(long_text)
... app.run_prompt("Now reply with HI.")
PY
```

Observed:

```text
FIRST_CODE 0 FIRST_CID 132ba3de-1e0d-498a-b5a9-0159646fe238:2 FIRST_CHARS 39374
SECOND_CODE 0 SECOND_CID 1ac74cce-3cd9-4945-85d9-942785437ced:2 SECOND_CHARS 8369
Context compacted. New thread: 1ac74cce-3cd9-4945-85d9-942785437ced. Previous summary saved to /tmp/deepseek-cli-e15b/summaries/132ba3de-1e0d-498a-b5a9-0159646fe238_2.md.
```

### E16 — summary file is real

Command:

```bash
latest=$(ls -t /tmp/deepseek-cli-e15b/summaries/*.md | head -1)
cat "$latest"
```

Observed excerpt:

```text
# Conversation summary: 132ba3de-1e0d-498a-b5a9-0159646fe238:2

## Structured Brief

**Conversation summary:**
- User sent a long block of repeated text...
- Requested reply: “Reply with OK.”
- Assistant replied: `OK`

**Current task state:**
- Task complete. No coding or file operations requested.

**Files mentioned:**
- None.

**Commands run:**
- None.
```

### E17 — conversation id changes after compaction

Observed in E15:

```text
old = 132ba3de-1e0d-498a-b5a9-0159646fe238:2
new = 1ac74cce-3cd9-4945-85d9-942785437ced:2
```

### E18 — summary is prepended to the new session's first message

Command used `DEEPSEEK_SSE_DEBUG=/tmp/E18b_sse.log`. Observed request log:

```text
[request-prompt] {"chat_session_id": "93fb7985-f7a1-4617-ab85-14e30a09d776",
"prompt": "Previous conversation summary:\n## Conversation Brief\n\n**Task state:** ..."}
```

### E19 — manual `/compact`

PTY command:

```bash
DEEPSEEK_CLI_HOME=/tmp/deepseek-cli-e19 venv/bin/python deepseek_cli.py --tools off
> say hi
> /compact
```

Observed:

```text
/compact
Context compacted. New thread: 4cf2abff-97c4-4412-a358-c0ae0178663f. Previous summary saved to /tmp/deepseek-cli-e19/summaries/b7ca924d-bee0-4cfc-ba8d-f2e1a9587293_2.md.
Compaction complete. The summary will prefix the next user message.
```

Next prompt confirmed:

```text
Prepended previous-conversation summary to the new session.
```

### E20 — active plan survives compaction

Observed:

```text
Plan
◐ Inspect code
☐ Patch code
☐ Run tests
...
Context compacted. New thread: cae4e242-b1a3-4082-9bd7-3e6c36e30663. Previous summary saved to /tmp/deepseek-cli-e20/summaries/d710b790-8489-4c0e-a3f2-956ea0ede78b_10.md.
Prepended previous-conversation summary to the new session.
Injected active plan from previous session.
```

Plan file existed under the new session id:

```text
/tmp/deepseek-cli-e20/plans/cae4e242-b1a3-4082-9bd7-3e6c36e30663.json
```

### E21 — autonomous multi-step plan writes and updates plan file

Command:

```bash
DEEPSEEK_CLI_HOME=/tmp/deepseek-cli-e21b venv/bin/python deepseek_cli.py \
  --tools auto \
  "Use update_plan to create a 3-step plan for reading three files: read /tmp/codex_plan_a.txt, read /tmp/codex_plan_b.txt, read /tmp/codex_plan_c.txt. Then read each file with exec_command. Mark steps completed as you go."
```

Observed plan/tool progression:

```text
tool update_plan {"explanation": "Creating a 3-step plan...", "plan": [...]}
Plan
◐ Read /tmp/codex_plan_a.txt
☐ Read /tmp/codex_plan_b.txt
☐ Read /tmp/codex_plan_c.txt
tool exec_command {"cmd": "cat /tmp/codex_plan_a.txt"}
  result exec_command alpha
tool update_plan {"plan": [... a completed, b in_progress ...]}
tool exec_command {"cmd": "cat /tmp/codex_plan_b.txt"}
  result exec_command bravo
tool update_plan {"plan": [... a/b completed, c in_progress ...]}
tool exec_command {"cmd": "cat /tmp/codex_plan_c.txt"}
  result exec_command charlie
tool update_plan {"plan": [... all completed ...]}
```

Completed plan moved to:

```text
/tmp/deepseek-cli-e21b/plans/completed/c8ae8b41-17ba-4d6d-9d0c-db7ed8896647.json
```

### E22 — Ctrl+C mid-task and resume

PTY commands:

```bash
DEEPSEEK_CLI_HOME=/tmp/deepseek-cli-e22 venv/bin/python deepseek_cli.py --tools auto
> Use update_plan to create a 3-step plan: run a diagnostic command in_progress, inspect output pending, report results pending. Do not execute anything yet.
> /thread
> Start step one now: use exec_command to run sleep 60.
<Ctrl+C>
> /thread
> /exit
```

Observed interrupt:

```text
tool exec_command {"cmd": "sleep 60", "yield_time_ms": 1000}
  result exec_command
tool write_stdin {"session_id": "sess_bd5e5836ef3e", "chars": "", "yield_time_ms": 60000}
^Cerror: interrupted
```

Resume:

```bash
DEEPSEEK_CLI_HOME=/tmp/deepseek-cli-e22 venv/bin/python deepseek_cli.py \
  --tools auto --resume 48414ae0-46ee-4749-83a4-c6b063e2b125:10 \
  "Continue by executing step one with exec_command: sleep 1. Then mark it complete."
```

Observed:

```text
Plan
◐ Run diagnostic command
☐ Inspect output
☐ Report results
Injected active plan from previous session.
tool exec_command {"cmd": "sleep 1", "yield_time_ms": 5000}
  result exec_command
conversation_id = 48414ae0-46ee-4749-83a4-c6b063e2b125:14
```

### E23 — `/plan`, `/plan clear`, `/plan resume`

PTY output:

```text
/plan resume
Plan
◐ Run diagnostic command
☐ Inspect output
☐ Report results

/plan
Plan
◐ Run diagnostic command
☐ Inspect output
☐ Report results

/plan clear
Plan cleared.

/plan
No active plan.
```

### E24 — previous T-tests

T2:

```text
echo "hi" | venv/bin/python deepseek_cli.py
Assistant
Hi! What can I help you with today?
```

T3:

```text
venv/bin/python deepseek_cli.py "say hello in one word"
Assistant
Hello!
```

T7:

```text
venv/bin/python deepseek_cli.py --search "latest news about X"
Assistant
Here's a roundup of the latest notable news involving X (formerly Twitter) as of October 2026:
...
```

T10:

```text
tool search_tools {"query": "weather"}
  result search_tools {
tool get_weather {"city": "Tokyo"}
  result get_weather 19.1°C in Tokyo, Japan · wind 5.2 km/h · WMO code 3
Assistant
Tokyo right now: **19.1 °C**, wind 5.2 km/h, overcast (WMO code 3).
```

T15:

```text
first: Assistant
OK

resumed: Assistant
nebula
```

### E25 — offline suites

Command:

```bash
PYTHONPATH=. venv/bin/python tests/test_cli.py
PYTHONPATH=. venv/bin/python tests/test_cli_tools.py
PYTHONPATH=. venv/bin/python tests/test_extensions.py
PYTHONPATH=. venv/bin/python tests/e2e_test.py
PYTHONPATH=. venv/bin/python tests/test_server_tools.py
```

Observed:

```text
PASS: native DSML tool call parses to ToolCall JSON
all CLI regression tests passed
all CLI tool tests passed
9 passed, 0 failed
all extension tests passed
all CLI e2e smoke tests passed
all offline tests passed
Phase 2 TestClient mock tests passed
```

### E26 — JSONL tool events

Command:

```bash
venv/bin/python deepseek_cli.py --json --tools auto \
  "what is the weather in Tokyo" | grep -E '"kind": "tool_'
```

Observed:

```json
{"kind": "tool_call", "name": "search_tools", "arguments": {"query": "weather"}}
{"kind": "tool_result", "name": "search_tools", "result": "{"}
{"kind": "tool_call", "name": "get_weather", "arguments": {"city": "Tokyo"}}
{"kind": "tool_result", "name": "get_weather", "result": "19.1°C in Tokyo, Japan · wind 5.2 km/h · WMO code 3"}
```

Validation confirmed `tool_result` events include `result` and an `answer`
event is present.

## 3. Bugs Found and Fixed

### DSML tool-call parser

File: `deepseek/client.py:593`

DeepSeek sometimes emits native fullwidth-pipe DSML tool-call markup instead of
the project's `<tool_call>` JSON wrapper. E8 showed the model silently produced
raw markup and no tool execution. Fixed by parsing DSML invoke/parameter blocks
into the same `ToolCall` JSON shape and stripping the trailing `FINISHED` token.

### Flaky `apply_patch` Add

File: `deepseek/client.py:81`

E5 initially produced no tool call. Fixed by adding a concrete `*** Add File`
example to the system preamble. Re-ran E5 3 times: all created files.

### Plan-store message-id drift

File: `deepseek/plan_store.py:45`

Plans were saved under `conversation_id` including `:<message_id>`, which changes
every turn. E9 could not find the plan after resume. Fixed by keying plan files
by the stable session id before the colon.

### Preamble format braces

File: `deepseek/client.py:66`

Adding a literal JSON tool example to a `str.format()` template caused
`KeyError: '"name"'`. Fixed by escaping braces in the example.

### Weak deferred-tool instruction

File: `deepseek/client.py:66`

E12 initially answered “I don't have a weather tool” without searching. Fixed by
making the preamble explicitly require `search_tools` before declaring a
capability unavailable, with a weather example.

### Nested/malformed tool arguments

File: `deepseek/client.py:549`

The model sometimes emitted `{"name": "...", "arguments": {"arguments": {...}}}`.
This caused TypeErrors in E13/E21. Fixed by normalizing that wrapper before
constructing `ToolCall`.

### Buffered tail leaked raw tool-call markup

File: `deepseek/client.py:880`

E21 showed raw `<tool_call>` blocks in assistant text when the block landed in
the parser's buffered tail. Fixed by reprocessing the final buffer during flush.

## 4. Known Limitations

- The text-only `view_image` metadata stub was removed in a later polish pass;
  terminal image rendering now lives in the optional `image_view.py` extension.
- Deferred tool matching is one-turn scoped. If a model needs the same deferred
  tool on a later turn, it must call `search_tools` again.
- Auto-compaction uses a conservative char/4 token estimate, not server-reported
  usage.
- Compaction restart creates a default DeepSeek session; expert-model identity is
  not guaranteed across the restarted context.
- Manual approval prompts for each approved tool call; a piped approval test
  must provide enough `y` lines for both discovery and final tool calls.
- `request_user_input` is visible in the eager set, but returns an error unless
  plan mode or agent mode is active.

## 5. First 5 Commands to Try

```bash
venv/bin/python deepseek_cli.py --tools auto "run ls in the current directory"
venv/bin/python deepseek_cli.py --tools auto "what is the weather in Tokyo"
venv/bin/python deepseek_cli.py --mode agent --tools auto "Use update_plan to plan a 3-step coding task"
DEEPSEEK_COMPACT_AT=5000 venv/bin/python deepseek_cli.py
venv/bin/python deepseek_cli.py --plan-mode --tools manual "Use request_user_input to ask me to choose an option"
```
