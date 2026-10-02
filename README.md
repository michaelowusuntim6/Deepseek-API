# DeepSeek Unofficial — library, CLI, and OpenAI-compatible server

> **Unofficial project.** Not affiliated with or endorsed by DeepSeek. It automates the consumer experience at [chat.deepseek.com](https://chat.deepseek.com) using **your own signed-in account**. No API key, no credits, no billing. Use it responsibly and within DeepSeek's terms.

Turn your free DeepSeek web chat into three things:

- 🐍 **A Python library** — `client.chat("Hi")` returns text, reasoning trace, and a resumable `conversation_id`.
- 🖥️ **A terminal CLI** — a Codex-style Rich interface with streaming output, tool-call visibility, DeepThink reasoning, and web search.
- 🔌 **A local OpenAI-compatible API** — runs at `http://localhost:8000/v1` so any OpenAI SDK or app works as a drop-in.

You sign in once in a browser; the session is captured and refreshed automatically.

---

## What's new

- DeepSeek model-side web search is now enabled by default; use
  `--no-search` or `/search` to disable it.
- Added a Markdown memory extension with BM25 retrieval and capped
  session-start injection.
- Added AGENTS.md discovery, `/agents`, `/agents-reload`, and `/agents-init`.
- Added a progressive-disclosure skills system with `list_skills`,
  `use_skill`, and `read_skill_file`.
- Added three example skills: `skill-creator`, `review`, and `agents-init`.
- Added `--no-memory`, `--no-agents`, `--generate-agents`, and
  `--show-preamble`.
- Fixed `BrokenPipeError` when piping JSON output into tools like `head`.
  Truncated pipes now exit `141` cleanly without tracebacks.
- Removed the text-only `view_image` metadata stub.
- Added `examples/extensions/image_view.py`, which renders local images inline
  with `rich-pixels`.
- Added line-buffered streaming Markdown rendering for TTY output.
- Added `apply_patch` diff previews in the terminal and `file_change` JSONL
  events.
- Added batched manual approval prompts with `y/n/a/A/q`.
- Tightened JSONL/event parity and stdout/stderr separation.

---

## Table of contents

- [Why use this](#why-use-this)
- [Requirements](#requirements)
- [Setup (2 minutes)](#setup-2-minutes)
- [Usage 1 — Terminal CLI](#usage-1--terminal-cli)
- [Usage 2 — Python library](#usage-2--python-library)
- [Usage 3 — OpenAI-compatible server](#usage-3--openai-compatible-server)
- [Models, DeepThink, and web search](#models-deepthink-and-web-search)
- [Human-check & proof-of-work](#human-check--proof-of-work)
- [Rate limits & concurrency](#rate-limits--concurrency)
- [Project layout](#project-layout)
- [Notes & limitations](#notes--limitations)
- [License](#license)

---

## Why use this

- **Free.** Uses your normal signed-in DeepSeek account — no API billing, no credits.
- **Three front-ends.** Library, terminal CLI, and HTTP server all share the same core client.
- **Full DeepSeek toolset.** Fast (Instant) and Expert models, plus DeepThink reasoning and web search as orthogonal toggles.
- **Real streaming.** Token-by-token output with the reasoning trace kept separate from the answer.
- **Drop-in OpenAI replacement.** Point any OpenAI client at `localhost` and existing code just works.

---

## Requirements

- **Python 3.9+**
- A **DeepSeek account** (the free one at [chat.deepseek.com](https://chat.deepseek.com))
- **Windows, macOS, or Linux**

---

## Setup (2 minutes)

```bash
# 1. Clone
git clone https://github.com/michaelowusuntim6/Deepseek-API.git
cd Deepseek-API

# 2. Create and activate a virtual environment
# macOS / Linux:
python3 -m venv venv && source venv/bin/activate
# Windows (PowerShell):
# python -m venv venv; venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install -r requirements.txt

# 4. Install the browser Playwright uses (one-time)
playwright install chromium

# 5. Sign in once — a browser opens, log into your DeepSeek account
python -m deepseek.auth
```

The login window handles the human-check and captures your session (bearer token + cookies) into `session/` (git-ignored, never shared). Every subsequent run reuses it — the session refreshes headlessly for ~6 hours, then prompts you again only if it fully expires.

---

## Usage 1 — Terminal CLI

The Rich CLI supports one-shot prompts, piped stdin, and an interactive REPL.

```bash
# One-shot
python deepseek_cli.py "say hello in one word"

# Piped stdin
echo "hi" | python deepseek_cli.py

# Interactive REPL
python deepseek_cli.py
```

Interactive startup shows a bordered banner, a compact session panel with model,
directory, permissions, DeepThink/search/tool state, short thread id, and a live
context estimate, followed by a dim status footer before each prompt.

### Command-line reference

| Flag | Values | Effect |
| --- | --- | --- |
| `--model` | `chat`, `expert` | Select DeepSeek Instant or Expert |
| `--no-thinking` | flag | Disable DeepThink reasoning (enabled by default) |
| `--show-thinking` | flag | Display the reasoning trace during streaming |
| `--no-search` | flag | Disable DeepSeek model-side web search (enabled by default) |
| `--tools` | `off`, `manual`, `auto` | Strip tools, approve each call, or run unattended (default: `auto`) |
| `--json` | flag | Emit one JSON object per line on stdout |
| `--resume` | conversation id | Continue an existing thread |
| `--no-stream` | flag | Buffer the answer instead of streaming tokens |
| `--legacy-tools` | flag | Re-enable the legacy file-oriented tool tier |
| `--compact-at` | tokens | Compact above this estimated token threshold (default: `500000`) |
| `--plan-mode` | flag | Enable structured `request_user_input` questions |
| `--mode` | `normal`, `agent` | Agent mode enables persistent plan tracking |
| `--no-markdown` | flag | Disable Markdown rendering during streaming |
| `--no-diff` | flag | Suppress `apply_patch` diff previews |
| `--no-memory` | flag | Skip memory auto-injection |
| `--no-agents` | flag | Skip AGENTS.md discovery |
| `--no-skills` | flag | Skip skills discovery and preamble listing |
| `--generate-agents` | flag | Generate AGENTS.md and exit |
| `--show-preamble` | flag | Print the assembled preamble to stderr and exit |

Exit codes are `0` success, `1` runtime error, `2` auth required, `3` usage
error, and `141` for a truncated pipe (`SIGPIPE` convention).

### Response protocol

The CLI accepts whatever the model naturally produces and parses it — it does
not force a single format.

**Tool calls.** One unified parser extracts every call in a response, in the
order it appears, whatever its shape: a `<tool_call>{"name": ..., "arguments":
...}</tool_call>` JSON block, a freeform patch block, or a DSML
`invoke`/`parameter` block. Mixed responses work too. All extracted calls are
executed sequentially; a failing call is reported and the rest still run, and
all results are returned to the model in one message.

**Completion.** A response ends the turn when it carries a valid `<<DONE>>`
marker, contains a completion phrase such as "TASK COMPLETE", or is a
substantial final answer (at least 200 characters with no trailing question or
forward-looking phrase). `<<DONE>>` is never shown to the user.

**Soft continuation.** Otherwise the harness sends exactly `Continue.` and
lets the model pick up where it left off — no correction, no format lecture.
The counter resets after any successful tool call and the turn stops after
five continuations, saving the last response to `/tmp/incomplete_<ts>.txt`.

**Network retry.** Retry behaviour is configured in `config.json` at the
repository root and is re-read at the start of every request:

```json
{
  "network_retry": {
    "response_timeout_minutes": 3,
    "max_consecutive_retries": 5
  }
}
```

`response_timeout_minutes` is a **stall detector**, not a wall-clock cap: it
fires only when no bytes have arrived from the server for that long, so a
response that keeps streaming for ten minutes never triggers it. When it
fires, a stall with no bytes at all deletes the previous message and resends the
input as a fresh message, while a stall after partial bytes reattaches to the
same message id. The retry counter tracks consecutive failures of either kind
and any successful response resets it to zero. After
`max_consecutive_retries` consecutive attempts the CLI prints a
`Network Connection Error` naming the attempt count, stops the operation, and
does not retry again.

## apply_patch format

`apply_patch` is a freeform tool. The model emits patch text directly — there is
no JSON wrapper.

Patch delimiters:
  `*** Begin Patch ... *** End Patch`

File operations:
  `*** Add File: <path>`     — create a new file
  `*** Update File: <path>`  — modify an existing file
  `*** Delete File: <path>`  — remove a file
  `*** Move to: <path>`      — rename, inside an Update hunk

Every hunk line must begin with `+`, `-`, or a space.

The harness detects freeform patches in the model's response and converts them
to tool calls automatically. Both the freeform format and the legacy JSON
format are accepted, but the FREEFORM format is preferred because it avoids the
JSON escaping tax on diffs.

This matches the format OpenAI Codex uses. Their official model guide reports a
35% reduction in apply_patch failure rates with freeform vs JSON function
calling.

### Agent tool set

By default the CLI exposes the Codex-style tools:

| Tool | Purpose |
| --- | --- |
| `exec_command` | Run commands with pipes or a PTY; long-running processes return a session id |
| `write_stdin` | Send input to an existing `exec_command` session |
| `apply_patch` | Apply Codex-style Update/Add/Delete patches |
| `search_tools` | Search deferred extension/capability tools |
| `update_plan` | Maintain a persistent step-by-step plan |
| `request_user_input` | Ask structured plan-mode questions |

Extension tools are deferred by default. The model must call `search_tools`
before using them, and matched tools stay available for one turn only.
Local image viewing is provided by the optional `examples/extensions/image_view.py`
extension, which renders pixels in the terminal with `rich-pixels`; it does not
send images to DeepSeek.

Auto-compaction runs before a new user turn when accumulated estimated context
exceeds `--compact-at` or `DEEPSEEK_COMPACT_AT`. It summarizes the thread,
stores the summary under `~/.deepseek-cli/summaries/`, and restarts with a new
DeepSeek session. Active plans are carried across compaction.

The default is 500,000 tokens for DeepSeek's 1M-token context window; 500K is a
conservative threshold that leaves retrieval-quality headroom.

### Diff rendering

Every `apply_patch` change is diffed before/after. On a terminal, the CLI renders
a syntax-highlighted unified diff panel titled with the changed file path. In
`--json` mode it emits a `file_change` event with `path`, `additions`,
`deletions`, and the unified diff. Piped non-JSON output receives plain diff
text. Use `--no-diff` to suppress all diff previews.

### Streaming markdown

On an interactive terminal, assistant output is line-buffered: completed lines
are rendered through Rich Markdown, while the last partial line stays plain
until the stream ends. Non-TTY output and `--json` skip Markdown entirely, so
JSONL stays ANSI-free. Use `--no-markdown` to force raw terminal output.

### Image viewing

The optional [`examples/extensions/image_view.py`](examples/extensions/image_view.py)
extension renders local PNG/JPEG/GIF/WebP files in the terminal with
`rich-pixels`. It is deferred, so the model finds it through `search_tools`.

```bash
cp examples/extensions/image_view.py ~/.deepseek-tui/extensions/
python deepseek_cli.py --tools auto "show tests/fixtures/test_image.png"
```

### Slash commands

Slash commands are available in the REPL. Typing `/` opens a prompt_toolkit
completion menu: a flat list with no box border, descriptions dimmed to the
right, differential redraw, scrolling, terminal resize support, and Ctrl+R
history search. Matching uses exact, prefix, then fuzzy order. Arrow keys move
the highlight, Enter submits, Tab completes the highlighted command, and Esc
closes the menu while keeping the text. Commands with arguments switch to an
argument list after the space, for example `/model ` shows `chat` and `expert`.

| Command | Effect |
| --- | --- |
| `/help` | Show commands and usage |
| `/new` | Start a fresh thread |
| `/thread` | Print the current `conversation_id` |
| `/clear` | Clear the terminal display |
| `/model [chat\|expert]` | Show or set the model |
| `/thinking [on\|off]` | Toggle DeepThink |
| `/search [on\|off]` | Toggle DeepSeek model-side web search (enabled by default) |
| `/mode manual\|auto` | Set tool approval mode |
| `/tools off\|manual\|auto\|list` | Configure tools or list them |
| `/compact` | Manually summarize and restart the context window |
| `/plan [clear\|resume]` | Show, clear, or resume the persistent plan |
| `/agents` | Show loaded AGENTS.md path and line count |
| `/agents-reload` | Re-read AGENTS.md from disk |
| `/agents-init` | Generate AGENTS.md for the current repository |
| `/extensions` | List loaded extensions and tools |
| `/reload` | Reload extensions from disk |
| `/exit` | Quit |

Extensions may also register their own slash commands through `COMMANDS`.

### Memory

The optional memory extension stores Markdown memory under
`~/.deepseek-cli/memory/`:

```bash
cp examples/extensions/memory.py ~/.deepseek-tui/extensions/
```

It provides `memory_write`, `memory_read`, `memory_search`, `memory_forget`,
and `memory_status`. At process start and after `/new`, the CLI retrieves up to
three BM25-selected paragraphs using the cwd name and AGENTS.md summary, capped
at 2000 characters. `MEMORY.md` is capped at 200 lines; older lines roll over
to `archive/YYYY-MM.md`.

### AGENTS.md

At startup the CLI searches upward from the current directory for the first
`AGENTS.md`, `.agents/AGENTS.md`, `CLAUDE.md`, or `.agents/CLAUDE.md`. It reads
at most 500 lines and injects them as project instructions. Use `--no-agents`
to skip discovery, `/agents-reload` to refresh, and `/agents-init` or
`--generate-agents` to draft a file.

### Skills

Skills live in `~/.deepseek-cli/skills/<name>/SKILL.md` or
`./.deepseek-cli/skills/<name>/SKILL.md`. The preamble lists only skill names
and descriptions. The body loads only when the model calls `use_skill(name)`,
and reference files load only through `read_skill_file(name, path)`.

Copy the default skills when wanted:

```bash
cp -r examples/skills/* ~/.deepseek-cli/skills/
```

### Context budget

- Memory injection: 2000 characters maximum.
- `MEMORY.md`: 200 lines maximum, with 50-line rollover archives.
- Skill descriptions: 80 characters maximum; 30 skills maximum.
- AGENTS.md: 500 lines maximum.
- Memory injects only at process start and after `/new`, never again after
  compaction.

---

## Usage 2 — Python library

The simplest integration if your code is already Python.

```python
from deepseek import DeepSeekClient

client = DeepSeekClient()                     # loads your signed-in session

# Full reply: text + reasoning + resumable id
reply = client.chat("Say hello in one short sentence.", thinking=True)
print(reply.text)                             # the answer
print(reply.thinking)                         # the DeepThink trace (empty when off)
print(reply.conversation_id)                  # pass back to continue

# Continue the same conversation
reply2 = client.chat("And now in French?", conversation_id=reply.conversation_id)
print(reply2.text)

# Stream answer text (thinking is filtered out for backwards compatibility)
for chunk in client.stream("Tell a short joke"):
    print(chunk, end="", flush=True)

# Rich streaming: interleaved thinking + answer
for kind, text in client.stream("Explain the GIL.", thinking=True).iter_parts():
    if kind == "thinking":
        ...  # route to your reasoning pane
    else:
        ...  # route to your chat bubble

client.close()
```

### The `iter_parts()` contract

`client.stream(...)` returns an object that:

- Iterates as **answer-only strings** (drop-in compatible with earlier versions).
- Exposes **`.iter_parts()`** yielding `(kind, text)` tuples where `kind` is `"thinking"` or `"answer"`.
- Exposes **`.conversation_id`** after consumption for resuming the thread.

See [`examples/`](examples/) for runnable scripts covering each mode.

---

## Usage 3 — OpenAI-compatible server

Start a local server that speaks the OpenAI API, so existing OpenAI tools and SDKs work unchanged.

```bash
python app.py
# -> http://127.0.0.1:8000
```

Then point any OpenAI client at it (the API key is required by the SDK but ignored):

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")

resp = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "Hello!"}],
    extra_body={"thinking": True, "search": True},   # non-OpenAI extras
)
print(resp.choices[0].message.content)
print(resp.model_extra.get("conversation_id"))
```

Or with plain `curl`:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "deepseek-chat",
       "messages": [{"role": "user", "content": "Hello!"}]}'
```

### Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/v1/chat/completions` | Chat. Supports `"stream": true`, plus optional `conversation_id`, `thinking`, `search` |
| `GET`  | `/v1/models` | Lists available models |
| `GET`  | `/healthz` | Health check (rate-limit exempt) |

**Extras beyond OpenAI's schema:**

- **`conversation_id`** — top-level in responses; send it back in the next request body to resume a thread. Echoed on the final streaming chunk.
- **`reasoning_content`** — on the assistant message (non-streaming) or as deltas before `content` (streaming), using DeepSeek's own `deepseek-reasoner` convention. Clients that don't understand it ignore it; clients that do get a proper reasoning pane.

Change the address with env vars: `HOST=0.0.0.0 PORT=8080 python app.py`.

---

## Models, DeepThink, and web search

The `model` name selects **which model** answers. DeepThink and web search are **orthogonal toggles** passed per request.

| Model id | DeepSeek mode | Notes |
| --- | --- | --- |
| `deepseek-chat` | Instant | Fast default |
| `deepseek-expert` | Expert | Stronger, slower |

Toggle them in Python with `thinking=True` / `search=True`, or in the server via `extra_body={"thinking": True, "search": True}`.

A thread's model is fixed at creation, so `model` can't be combined with `conversation_id` on resume — the resuming request simply omits `model`. Unknown model names return `404`.

---

## Human-check & proof-of-work

DeepSeek's chat sits behind two gates, both handled automatically:

- **AWS WAF human-check** — needs a signed-in browser session that has cleared the "verify you're human" check. `python -m deepseek.auth` opens a real browser so you can do that once; the resulting session is cached in `session/` and reused.
- **Proof-of-work** — every completion is gated by a PoW challenge. The bridge solves it by running DeepSeek's own `sha3_wasm_bg.wasm` (the same module the browser loads) inside a `wasmtime` sandbox.

A cached session is reused for ~6 hours and refreshed headlessly from your saved Chrome profile when possible.

---

## Rate limits & concurrency

The server bridges a **single** signed-in account behind one shared client. The PoW solver's `wasmtime` store is not reentrant, so upstream calls are **serialized** — parallel HTTP requests queue behind a lock and run one at a time. Keep concurrent in-flight requests low, and please don't hammer your account.

A self-imposed sliding-window limiter caps requests per client IP (default `30/min`, override with `RATE_LIMIT_PER_MINUTE`). `/healthz` is exempt. On the client side, use exponential backoff for `429`s — the official `openai` SDK does this automatically.

---

## Project layout

| Path | What it does |
| --- | --- |
| [`deepseek/`](deepseek/) | Core library: `DeepSeekClient`, auth (`auth.py`), HTTP driver (`client.py`), PoW solver (`pow.py`) |
| [`deepseek_cli.py`](deepseek_cli.py) | Rich terminal CLI |
| [`server/`](server/) | FastAPI OpenAI-compatible server |
| [`app.py`](app.py) | Server entry point |
| [`examples/`](examples/) | Runnable examples for every feature |
| [`session/`](session/) | Your signed-in session (git-ignored) |

---

## Notes & limitations

- **Piped output to `head`:** If the downstream pipe closes early, the CLI exits
  `141` and suppresses further stdout writes. This is expected SIGPIPE behavior,
  not a crash; stderr should remain traceback-free.
- **AGENTS.md is ignored:** Check that the filename is exactly `AGENTS.md`,
  that it is not over 500 lines, and that it is in the current directory, a
  parent up to the git/filesystem root, or `.agents/AGENTS.md`. Use
  `--no-agents` only if you want discovery disabled.
- **The popup stacks:** This was fixed by replacing the hand-rolled renderer
  with prompt_toolkit. Run `pip install -r requirements.txt` and use the latest
  commit.
- **Sign in once, then reuse.** The cached session refreshes automatically; you only re-sign-in if it fully expires.
- **Be reasonable.** Use it in moderation; don't spam or bulk-automate.
- **No real token counts.** `usage` in server responses is a rough ~4-chars/token estimate.
- **Most OpenAI params are accepted but ignored** (`temperature`, `top_p`, `max_tokens`); only `model`, `messages`, `stream`, `conversation_id`, `thinking`, and `search` do anything.
- **Vision is deferred.** It needs image-upload plumbing that isn't built yet.
- **Your session is private.** Everything in `session/` stays on your machine.

---

## License

Released under the [MIT License](LICENSE). As this is an unofficial project, you remain responsible for complying with DeepSeek's terms of service.
```
