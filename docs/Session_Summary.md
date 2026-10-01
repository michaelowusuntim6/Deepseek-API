# Session Summary - DeepSeek CLI Upgrade

Date: 2026-10-01  
Repository: `/home/mike/Deepseek-API`  
Current HEAD: `98d3f06`  
Working tree: clean at the time this file was created

## 1. Project Purpose

This repository turns the DeepSeek web chat at `chat.deepseek.com` into three
interfaces:

1. A Python library in `deepseek/`.
2. A Codex-style terminal coding agent in `deepseek_cli.py`.
3. An OpenAI-compatible FastAPI server in `server/`.

The original Textual TUI, `deepseek_tui.py`, was removed. The active CLI is now
Rich + prompt_toolkit based.

## 2. Main Work Completed

### TUI to CLI Conversion

- Replaced `deepseek_tui.py` with `deepseek_cli.py`.
- Added one-shot, piped stdin, and interactive REPL modes.
- Added Rich banner, static startup panel, live toolbar, streaming output, and
  JSONL mode.
- Added exit codes:
  - `0` success
  - `1` runtime error
  - `2` auth required
  - `3` usage error
  - `141` broken pipe
- Added legacy tool tier for older file tools.

### Codex-Style Tool Layer

`deepseek/agent_tools.py` adds:

- `exec_command`: PTY or pipe execution with persistent session IDs.
- `write_stdin`: write to a running command session.
- `apply_patch`: Codex-style Update/Add/Delete patches.
- `update_plan`: persistent plan tracking.
- `request_user_input`: structured questions.

`deepseek/tool_search.py` adds:

- BM25 search over deferred tools.
- Exact-name bias.
- One-turn visibility for matched tools.

`deepseek/tools.py` now tracks:

- `deferred`
- `eager`
- `read_only`

### Auto-Compaction

`deepseek/compaction.py`:

- Default threshold is now `500,000` tokens.
- Rationale: current context is treated as 1M tokens, and 500K leaves
  retrieval-quality headroom.
- Override with `--compact-at` or `DEEPSEEK_COMPACT_AT`.
- Summaries stored in `~/.deepseek-cli/summaries/`.
- `/compact` triggers the same flow manually.

### Persistent Plans

`deepseek/plan_store.py`:

- Plans stored in `~/.deepseek-cli/plans/`.
- Completed plans archived.
- Plans reload on `--resume`.
- Commands: `/plan`, `/plan clear`, `/plan resume`.
- Plans survive compaction.

### Context Layer

Added:

- `deepseek/bm25.py`: shared BM25.
- `examples/extensions/memory.py`: Markdown memory with BM25 retrieval.
- `deepseek/agents_md.py`: AGENTS.md discovery and generator.
- `deepseek/skills.py`: progressive-disclosure skills.
- `examples/skills/`: default `skill-creator`, `review`, and `agents-init`.

Caps:

- Memory injection: 2000 characters.
- `MEMORY.md`: 200 lines.
- Skill description: 80 characters.
- Max skills in preamble: 30.
- AGENTS.md: 500 lines.

### prompt_toolkit REPL

`deepseek/repl.py` provides:

- slash command completion menu
- exact, prefix, fuzzy matching
- argument completion
- picker commands: `/model`, `/mode`, `/tools`, `/thinking`, `/search`, `/plan`
- Enter accepts the selected completion
- Esc walks back one menu level
- Ctrl+R history search

### Image Viewer Extension

`examples/extensions/image_view.py` uses `rich-pixels` to render local images in
the terminal. It is deferred and discoverable via `search_tools`.

### Web Search and Fetch

Eager tools:

- `fetch_url`: HTTP GET, strips HTML, caps output.
- `web_search`: DuckDuckGo HTML parser with Instant Answer API fallback.

Known caveat: DuckDuckGo HTML currently returns a landing page in this
environment, so niche queries may return no useful results. The shell fallback
is reliable:

```bash
curl -sL "https://html.duckduckgo.com/html/?q=..."
curl -sL <url>
git clone <url> <dest>
wget <url>
```

## 3. Current Defaults

| Setting | Default |
| --- | --- |
| Model | `deepseek-chat` |
| Thinking | `on` |
| Model-side web search | `on` |
| Tools | `auto` |
| Compact threshold | `500,000` tokens |
| Toolbar | model, think, model-search, tools, agent, thread, ctx |
| Static panel | dir, permissions, deferred count, compact threshold |

## 4. Important Flags

```text
--model {chat,expert}
--no-thinking
--no-search
--tools {off,manual,auto}
--show-thinking
--json
--resume <conversation_id>
--no-stream
--no-markdown
--no-diff
--legacy-tools
--compact-at <tokens>
--plan-mode
--mode {normal,agent}
--no-memory
--no-agents
--no-skills
--generate-agents
--show-preamble
```

## 5. Slash Commands

```text
/help
/new
/thread
/clear
/model
/thinking
/search
/mode
/tools
/compact
/plan
/agents
/agents-reload
/agents-init
/extensions
/reload
/status
/exit
```

Notable behavior:

- `/search` toggles DeepSeek model-side web search.
- `/search` is not an agent tool.
- `/tools` picker values are `off`, `manual`, `auto`, and `list`.
- `/tools on` is intentionally invalid.

## 6. Rendering and UX

- Code blocks are syntax-highlighted, indented, and not boxed.
- Exactly one blank line is placed before and after code blocks.
- JSON and non-TTY modes keep raw fenced text.
- Diff panels preserve aligned markers:
  - `+` lines use green background.
  - `-` lines use red background.
  - `@@` hunk headers are cyan.
  - file headers are bold.
- Edit summary line:

```text
• Edited path/to/file (+X -Y)
```

- Startup panel is static only and no longer reprints.
- Live state is in the prompt_toolkit bottom toolbar.
- Banner includes `By Michael Owusu Ntim`.

## 7. DSML and Tool-Call Handling

DeepSeek sometimes emits native DSML markup instead of the requested JSON tool
call wrapper. The parser now handles:

- plain JSON tool calls
- JSON followed by trailing DSML tags
- JSON inside DSML invoke/parameter blocks
- JSON string arguments
- trailing whitespace and extra text

Parser diagnostics were added:

- unparsed candidates are logged to stderr
- full raw answer is written to `/tmp/parser_fail_<timestamp>.txt`

The preamble explicitly tells the model to use only:

```text
<tool_call>{"name": "tool_name", "arguments": {...}}</tool_call>
```

and not DSML/XML and not extra prose around tool calls.

## 8. Tests Currently Present

```text
tests/test_bm25.py
tests/test_cli.py
tests/test_cli_tools.py
tests/test_extensions.py
tests/test_image_view.py
tests/test_memory.py
tests/test_repl.py
tests/test_server_tools.py
tests/test_skills.py
tests/e2e_test.py
```

The full suite has been passing after each committed change.

## 9. Recent Commit Trail

```text
98d3f06 Add parser-failure diagnostics; fix DSML variant captured from live session; reinforce tool-call format in preamble
6750bcd Fix DSML parser for wrapped JSON; default thinking and tools to on/auto; add animated working indicator; add edited-file status line
005adb6 Recover JSON tool calls with trailing DSML tags
8b85bb3 Add single blank line around code blocks
7bd7384 Fix streaming markdown code blocks and diff panel rendering
1ccf5ab Enable DeepSeek web search by default; replace --search with --no-search
05ca1d2 Add fetch_url and web_search tools; add shell fallback to preamble; fix extension-loaded leak; clarify /search
44b3e57 Fix /tools picker; slim startup panel; add author to banner
611623d Replace stacked panel reprints with prompt_toolkit bottom_toolbar
5170dc9 Replace hand-rolled popup with prompt_toolkit completions menu
```

## 10. Current Blocker

The dataset acceptance test could not be completed because this input path does
not exist in the environment:

```text
~/Downloads/Huggingface/hf_results
```

The requested report path would be:

```text
~/Downloads/Huggingface/Helpful.md
```

Because the source dataset is absent, the 26-section dataset report cannot be
truthfully generated or verified here. The parser was verified against known
DSML/JSON variants, but the original dataset task remains unverified until the
dataset directory is restored.

## 11. How To Run

Activate the venv:

```bash
source venv/bin/activate
python deepseek_cli.py
```

Or call it directly:

```bash
venv/bin/python deepseek_cli.py
```

Examples:

```bash
venv/bin/python deepseek_cli.py "say hi"
venv/bin/python deepseek_cli.py --tools auto "what is the weather in Tokyo"
venv/bin/python deepseek_cli.py --json "hi"
venv/bin/python deepseek_cli.py --show-preamble
```

## 12. Next Useful Steps

1. Restore the missing dataset directory.
2. Re-run the full dataset task and inspect `/tmp/parser_fail_*.txt` if it stops.
3. Decide whether to keep or remove the working checklist files after audit.
4. Verify the prompt_toolkit nested picker behavior interactively for every
   picker command (`/model`, `/mode`, `/tools`, `/thinking`, `/search`, `/plan`).
5. Consider whether `web_search` needs a more reliable backend than DuckDuckGo.
