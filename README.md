# DeepSeek Unofficial — library, CLI, and OpenAI-compatible server

> **Unofficial project.** Not affiliated with or endorsed by DeepSeek. It automates the consumer experience at [chat.deepseek.com](https://chat.deepseek.com) using **your own signed-in account**. No API key, no credits, no billing. Use it responsibly and within DeepSeek's terms.

Turn your free DeepSeek web chat into three things:

- 🐍 **A Python library** — `client.chat("Hi")` returns text, reasoning trace, and a resumable `conversation_id`.
- 🖥️ **A terminal CLI** — a Codex-style Rich interface with streaming output, tool-call visibility, DeepThink reasoning, and web search.
- 🔌 **A local OpenAI-compatible API** — runs at `http://localhost:8000/v1` so any OpenAI SDK or app works as a drop-in.

You sign in once in a browser; the session is captured and refreshed automatically.

---

## What's new

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
| `--thinking` | flag | Enable DeepThink reasoning |
| `--show-thinking` | flag | Display the reasoning trace during streaming |
| `--search` | flag | Enable DeepSeek web search |
| `--tools` | `off`, `manual`, `auto` | Strip tools, approve each call, or run unattended |
| `--json` | flag | Emit one JSON object per line on stdout |
| `--resume` | conversation id | Continue an existing thread |
| `--no-stream` | flag | Buffer the answer instead of streaming tokens |
| `--legacy-tools` | flag | Re-enable the legacy file-oriented tool tier |
| `--compact-at` | tokens | Compact context above this estimated token threshold |
| `--plan-mode` | flag | Enable structured `request_user_input` questions |
| `--mode` | `normal`, `agent` | Agent mode enables persistent plan tracking |
| `--no-markdown` | flag | Disable Markdown rendering during streaming |
| `--no-diff` | flag | Suppress `apply_patch` diff previews |

Exit codes are `0` success, `1` runtime error, `2` auth required, `3` usage
error, and `141` for a truncated pipe (`SIGPIPE` convention).

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

Slash commands are available in the REPL. Tab completion is provided through
`readline`.

| Command | Effect |
| --- | --- |
| `/help` | Show commands and usage |
| `/new` | Start a fresh thread |
| `/thread` | Print the current `conversation_id` |
| `/clear` | Clear the terminal display |
| `/model [chat\|expert]` | Show or set the model |
| `/thinking [on\|off]` | Toggle DeepThink |
| `/search [on\|off]` | Toggle web search |
| `/mode manual\|auto` | Set tool approval mode |
| `/tools off\|manual\|auto\|list` | Configure tools or list them |
| `/compact` | Manually summarize and restart the context window |
| `/plan [clear\|resume]` | Show, clear, or resume the persistent plan |
| `/extensions` | List loaded extensions and tools |
| `/reload` | Reload extensions from disk |
| `/exit` | Quit |

Extensions may also register their own slash commands through `COMMANDS`.

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
