# DeepSeek-API — a free LLM API + the `mike` coding harness

**Using your own DeepSeek account.** No API key, no credits, no paid plan: this
turns the free chat at [chat.deepseek.com](https://chat.deepseek.com) into an
OpenAI-compatible API you can call from code — and ships **`mike.py`**, a
self-contained coding-agent CLI that runs on top of it.

Two things live here:

- 🐍 **The library + server** — `deepseek/` (a pure-HTTP client) and `server/`
  (a FastAPI app at `http://127.0.0.1:8000/v1` that speaks the OpenAI format).
- 🤖 **`mike.py`** — one file, no sibling dependencies, that auto-launches the
  server and gives you an interactive coding agent with tools, thinking-block
  handling, inline compaction and resumable sessions.

> **Unofficial project.** Not affiliated with or endorsed by DeepSeek. It
> automates the consumer DeepSeek web experience for personal use, so use it
> responsibly and within DeepSeek's terms.

---

## The single-model story

DeepSeek's free web chat now exposes **exactly one model**. Everywhere in this
project it is surfaced as:

| | |
| --- | --- |
| **Display name** | `DeepSeek V4.1 Flash` |
| **Wire id** | `deepseek-v4.1-flash` |

That is the *only* model you will ever see, in `mike.py` or from `/v1/models`.
For compatibility, these legacy aliases are still accepted and silently routed
to it (no warning beyond one info line at startup, and a single INFO log line on
the server per alias per process):

```
deepseek-chat   deepseek-reasoner   deepseek-v3      deepseek-v4
deepseek-v4.1-flash   deepseek-flash   deepseek-web   default
```

Think of **thinking** (DeepThink reasoning) and **web search** as toggles, not
models — they are requested per call and never encoded in the model name.

---

## Install

```bash
git clone https://github.com/sums001/Deepseek-API.git
cd Deepseek-API

python3 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium         # one-time: browser for sign-in

python -m deepseek.auth             # sign in once (opens a browser)
```

Your session (bearer token + cookies) is saved under `session/` and refreshed
automatically afterwards. The server can also open the login window on demand
the first time it needs a session.

---

## Run the harness: `mike.py`

```bash
python mike.py            # start (or attach to) the server, then chat
python mike.py resume     # pick a saved session from a table, then continue
python mike.py --help     # usage (does not start the server)
```

`mike.py` is a single self-contained file. On startup it:

1. probes `127.0.0.1:8000` and reuses a server that is already running
   (it never starts a second one);
2. otherwise launches `python app.py` from this directory and polls
   `/v1/models` until it answers (30s timeout, 250 ms backoff);
3. registers exit/signal handlers that stop the child **only if mike started it**;
4. detects whether the backend supports native OpenAI `tools=` (it probes with a
   dummy `ping` tool) and caches the answer in `.mike_state.json`.

Everything is local: the only network calls are to `127.0.0.1` and, when a tool
asks for it, to URLs you fetch.

### Slash commands

```
/help                 show this help
/quit, /exit          quit (saves the session)
/clear                clear the screen
/model [name]         show the model (there is exactly one)
/thinking on|off|last thinking display: on / off / show last block
/sessions             list saved sessions
/resume <n|name>      switch to a saved session
/rename <name>        rename the current session
/compact              force compaction now
/tokens               show the current token estimate + threshold
/tools                list the available tools
/rollover             force a session rollover
```

### Tools

`read_file`, `write_file`, `edit_file`, `list_dir`, `glob`, `grep`, `bash`,
`python`, `web_fetch`, `todo_write`, `todo_read`.

The server's web backend does not implement OpenAI's `tools=` schema, so mike
runs in **prompted mode**: it describes the tools in a system prompt and parses
the model's tool calls out of the reply. It understands both the strict
`<tool_call>{"name": ..., "arguments": {...}}</tool_call>` envelope *and*
DeepSeek's native DSML syntax (`invoke`/`parameter` tags), and multiple calls per
turn. If a backend ever does return native `message.tool_calls`, mike detects it
at startup and uses that path instead.

### Thinking blocks

DeepSeek streams reasoning (fragment type `THINK`). By default mike shows it in a
dimmed single-line spinner and never dumps it to scrollback; `/thinking on`
streams the full reasoning in dim text, `/thinking off` hides it, and
`/thinking last` prints the most recent full reasoning block once. Reasoning is
stored in the session under a separate `reasoning` field and is **never** fed
back to the model as assistant content.

### Sessions

Sessions live in `sessions/` as pretty-printed, sorted-key JSON named
`YYYYMMDD-HHMMSS__<slug>.json`, where the slug is derived from your first
message (or set with `/rename`). Each file holds the **full** history, the
compaction log, and the model name. Compaction shrinks only the *in-context*
view; the on-disk transcript is never truncated. When a session is still over
budget after compaction, mike transparently **rolls over** to a new
`<slug>-contN` session seeded with the summary.

### Changing the context budget

The max context is **500,000 tokens** by default, and auto-compaction triggers at
**80%** of it (i.e. **400,000 tokens**). Override the max with an environment
variable — the threshold follows automatically:

```bash
MIKE_MAX_CONTEXT=200000 python mike.py     # => compacts at 160,000
```

Token counting uses `tiktoken` when available and falls back to a `len//4`
heuristic; the choice is persisted in `.mike_state.json`.

---

## The OpenAI-compatible server

Start it directly (mike does this for you):

```bash
python app.py       # -> http://127.0.0.1:8000
```

Then point any OpenAI client at it (the key is required by the SDK but ignored):

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="sk-local")
resp = client.chat.completions.create(
    model="deepseek-v4.1-flash",
    messages=[{"role": "user", "content": "Hello!"}],
    extra_body={"thinking": True, "search": False},   # DeepThink / web search
)
print(resp.choices[0].message.content)
```

**Endpoints**

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/v1/chat/completions` | Chat. Supports `stream: true`; non-OpenAI extras: `conversation_id`, `thinking`, `search` |
| `GET`  | `/v1/models` | Advertises the one model, with `aliases: [...]` |
| `GET`  | `/healthz` | Health check (rate-limit exempt) |

`/v1/models` returns exactly one entry:

```json
{"id": "deepseek-v4.1-flash", "object": "model", "name": "DeepSeek V4.1 Flash",
 "aliases": ["deepseek-chat", "deepseek-reasoner", "deepseek-v3", "deepseek-v4",
             "deepseek-flash", "deepseek-web", "default"]}
```

Requests naming any alias are rewritten to the canonical id and logged once per
alias per process. Genuinely unknown names still return `404` (no silent
fallback — there is only one model anyway).

Change the address with env vars: `HOST=0.0.0.0 PORT=8080 python app.py`, or run
`uvicorn server.api:app --host 0.0.0.0 --port 8080`.

---

## Usage as a Python library (no server)

```python
from deepseek import DeepSeekClient

client = DeepSeekClient()                # loads your signed-in session
reply = client.chat("Say hello in one short sentence.")
print(reply.text, "|", reply.reasoning)

reply2 = client.chat("And now in French?", conversation_id=reply.conversation_id)
print(reply2.text)

for chunk in client.stream("Tell me a short joke"):
    print(chunk, end="", flush=True)
```

See [examples/](examples/) for runnable versions.

---

## Concurrency, rate limiting, notes

- The server bridges a **single** signed-in account and serializes upstream
  calls (the PoW solver is not reentrant). Throughput is sequential.
- A self-imposed sliding-window rate limit applies per client IP
  (`RATE_LIMIT_PER_MINUTE`, default `30`); `/healthz` is exempt. Use exponential
  backoff on `429`.
- No real token counts: `usage` is a rough ~4-chars/token estimate.
- Most OpenAI params (`temperature`, `top_p`, `max_tokens`) are accepted but
  ignored; only `model`, `messages`, `stream`, `conversation_id`, `thinking` and
  `search` do anything.
- Everything in `session/`, `sessions/` and `.mike_state.json` is local and
  git-ignored.

## Project layout

| Path | What it does |
| --- | --- |
| `mike.py` | The self-contained coding harness (this is the deliverable) |
| `deepseek/` | Core client: auth, HTTP driver, PoW solver |
| `server/` | FastAPI OpenAI-compatible server |
| `tests/test_mike.py` | Hermetic pytest suite for mike |
| `RECON.md`, `DECISIONS.md` | Reconnaissance notes and design decisions |
| `app.py` | Starts the server |

## License

MIT. `mike.py` is derived from `nanocode.py` (MIT) and ports tool/compaction
logic from `cipher-harness` (MIT); those notices are preserved in its header. As
this is an unofficial project, you remain responsible for complying with
DeepSeek's terms of service.
