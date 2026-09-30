# CLI Polish Report

Date: 2026-09-30

## Summary

This polish pass fixed the JSON BrokenPipeError crash, removed the dead
`view_image` stub, added a real terminal image viewer extension, added streaming
Markdown and diff rendering, batched manual approvals, tightened JSON/exit-code
behavior, cleaned superseded reports, updated dependencies/docs, and committed
the result locally.

## Test Evidence

### P1 — `--json "hi" | head -1`

Command:

```bash
venv/bin/python deepseek_cli.py --json "hi" | head -1
```

Observed:

```json
{"kind": "answer", "text": "Hi! What can I help you with today?"}
exit=0
stderr_bytes=0
```

### P2 — early pipe close

Command:

```bash
venv/bin/python deepseek_cli.py --json --tools auto \
  "search_tools for get_weather" | head -5
```

Observed:

```json
{"kind": "tool_call", "name": "search_tools", "arguments": {"query": "get_weather"}}
{"kind": "tool_result", "name": "search_tools", "result": "{"}
{"kind": "answer", "text": "The"}
{"kind": "answer", "text": " "}
{"kind": "answer", "text": "`get_w"}
exit=141
stderr_bytes=0
```

No traceback.

### P3 — plain one-shot

Command:

```bash
venv/bin/python deepseek_cli.py "say hello in one word"
```

Observed:

```text
Assistant
Hello
exit=0
```

### P4 — streaming Markdown

PTY command:

```bash
venv/bin/python deepseek_cli.py
> Give me a short markdown example with one bold word and one bullet.
```

Observed:

```text
Assistant
Here's a quick example:
Important: remember these points:

 • Keep it simple and direct.
```

The bold text was rendered with ANSI style codes (`\x1b[1m`) rather than literal
asterisks.

### P5 — JSON has no ANSI escapes

Command:

```bash
venv/bin/python deepseek_cli.py --json "show me a code example"
```

Observed:

```text
exit=0
ansi_escape= False
b'{"kind": "answer", "text": "What language or concept would you like the code example to demonstrate?"}\n'
```

### P6 — diff panel renders

PTY command:

```bash
venv/bin/python deepseek_cli.py --tools auto \
  "create /tmp/polish_panel.txt with three lines alpha beta gamma, then change beta to BETA using apply_patch"
```

Observed:

```text
╭─────────────────────────── /tmp/polish_panel.txt ────────────────────────────╮
│ --- /tmp/polish_panel.txt                                                    │
│ +++ /tmp/polish_panel.txt                                                    │
│ @@ -1,3 +1,3 @@                                                              │
│  alpha                                                                       │
│ -beta                                                                        │
│ +BETA                                                                        │
│  gamma                                                                       │
╰──────────────────────────────────────────────────────────────────────────────╯
  result apply_patch applied 1 hunks to 1 files
```

Observed with `--no-diff`:

```text
Assistant
Done. Added `/tmp/polish_nodiff.txt` with one line: `hello`.
no diff markers
file_exists
```

### P7 — JSON `file_change`

Command:

```bash
venv/bin/python deepseek_cli.py --json --tools auto \
  "Use apply_patch to add /tmp/polish_diff_final.txt containing one line hello"
```

Observed:

```json
{"kind": "file_change", "path": "/tmp/polish_diff_final.txt", "additions": 1, "deletions": 0, "diff": "--- /tmp/polish_diff_final.txt\n+++ /tmp/polish_diff_final.txt\n@@ -0,0 +1 @@\n+hello\n"}
```

### P8 — batched manual approvals

Command:

```bash
printf 'y\ny\ny\ny\n' | venv/bin/python deepseek_cli.py --tools manual \
  "read /tmp/polish_read_a.txt, /tmp/polish_read_b.txt, and /tmp/polish_read_c.txt, then summarize them in one sentence"
```

Observed:

```text
Pending tool calls:
1. exec_command {"cmd": "cat /tmp/polish_read_a.txt", "workdir": "/tmp"}
  result exec_command a
Pending tool calls:
1. exec_command {"cmd": "cat /tmp/polish_read_b.txt", "workdir": "/tmp"}
2. exec_command {"cmd": "cat /tmp/polish_read_c.txt", "workdir": "/tmp"}
  result exec_command b
  result exec_command c
Assistant
The files contain "a", "b", and "c" respectively.
```

Approval prompts use `[y]es/[n]o/[a]lways/[A]ll/[q]uit`.

### P9 — image viewer extension

A temporary extension directory was populated with
`examples/extensions/image_view.py`, and the test image was copied under a
relative `tests/fixtures/` path.

Command:

```bash
cd /tmp/polish_image_cli
/home/mike/Deepseek-API/venv/bin/python /home/mike/Deepseek-API/deepseek_cli.py \
  --tools auto "show tests/fixtures/test_image.png"
```

Observed tool lines:

```text
tool search_tools {"query": "image"}
  result search_tools {
tool show_image {"path": "tests/fixtures/test_image.png"}
  result show_image Rendered tests/fixtures/test_image.png at 40x40 pixels.
```

Observed block characters:

```text
▄
▄
▄
```

### P10 — redirected JSON output

Command:

```bash
venv/bin/python deepseek_cli.py --json "hi" > /dev/null ; echo $?
```

Observed:

```text
0
```

### P11 — CLI regression tests

Command:

```bash
PYTHONPATH=. venv/bin/python tests/test_cli.py
```

Observed:

```text
  PASS: B1 unknown tool returns error string
  PASS: B4 parser emits every tool-call block
  PASS: native DSML tool call parses to ToolCall JSON
  PASS: B4 Reply.tool_calls and compatibility alias
  PASS: B4 stream_with_tools executes every requested tool
  PASS: B5 unchanged tool preamble is skipped for the same session
  PASS: JSON tool_result event includes name and result
  PASS: B2 extension commands receive emit/app or keep legacy print
  PASS: JSON help emits JSON and broken pipes exit 141 cleanly
all CLI regression tests passed
```

### P12 — image view tests

Command:

```bash
PYTHONPATH=. venv/bin/python tests/test_image_view.py
```

Observed:

```text
all image view tests passed
```

### P13 — CLI tool tests

Command:

```bash
PYTHONPATH=. venv/bin/python tests/test_cli_tools.py
```

Observed:

```text
  PASS: edit_file
  PASS: grep
  PASS: find_files
  PASS: fetch_url
all CLI tool tests passed
```

### P14 — extension tests

Command:

```bash
PYTHONPATH=. venv/bin/python tests/test_extensions.py
```

Observed:

```text
9 passed, 0 failed
all extension tests passed
```

Additional suites also passed:

```text
all CLI e2e smoke tests passed
all offline tests passed
Phase 2 TestClient mock tests passed
```

### P15 — git hygiene

Command:

```bash
git status --short
```

Verified after staging:

```text
no tests/fixtures/*.png
no session/
no .deepseek-cli/
no __pycache__/
```

## Bugs Found and Fixed

1. **BrokenPipeError crash**
   - File: `deepseek_cli.py`
   - Fixed with top-level `BrokenPipeError` handling, fd 1 redirected to
     `/dev/null`, exit code `141`, and explicit re-raise from JSON event flush.
   - Also guarded the `--json --help` path and interpreter-exit flush.

2. **JSON `head -1` over-streamed**
   - File: `deepseek_cli.py`
   - JSON answers are buffered into one line when no tools were used, while
     tool-using turns keep streaming so early pipe close still exercises 141.

3. **Dead `view_image` stub**
   - File: `deepseek/agent_tools.py`
   - Removed entirely; terminal image rendering is now an optional
     `rich-pixels` extension.

4. **Raw tool markup in final text**
   - File: `deepseek_cli.py`
   - Complete `<tool_call>...</tool_call>` blocks are stripped from answer text
     if they leak through as answer chunks.

5. **String-form tool arguments**
   - File: `deepseek/client.py`
   - Tool arguments arriving as a JSON string are decoded before ToolCall
     construction.

## Dependencies

- Added `rich-pixels>=3.0`.
- Added explicit `pillow>=10.0` because the image extension and fixture tests
  import Pillow.
- Removed Textual from the working venv; it is not in `requirements.txt`.
- The OpenAI API vision path (`deepseek-flash` at `api.deepseek.com`, base64
  images up to 32 MiB) is intentionally not implemented in this pass because it
  requires an API key. It can be added later as a separate optional extension.

## Known Limitations

- The image viewer renders to stdout. Do not combine image rendering with
  `--json` if strict JSONL is required.
- Streaming Markdown holds the final partial line as plain text until the stream
  completes.
- Manual approval batches are per sampling turn; multi-turn tool loops may show
  more than one batch.

## First 5 Commands to Try

```bash
venv/bin/python deepseek_cli.py --json "hi" | head -1
venv/bin/python deepseek_cli.py --tools auto "what is the weather in Tokyo"
venv/bin/python deepseek_cli.py --tools auto "create /tmp/demo.txt with one line hello"
venv/bin/python deepseek_cli.py "show me a markdown code example"
python -c "from PIL import Image; Image.new('RGB',(10,10),(32,128,224)).save('tests/fixtures/test_image.png')" && venv/bin/python deepseek_cli.py --tools auto "show tests/fixtures/test_image.png"
```
