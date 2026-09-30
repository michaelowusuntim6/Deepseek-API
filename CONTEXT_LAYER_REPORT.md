# Context Layer Report

Date: 2026-09-30

## Summary

Added a shared BM25 module, a Markdown memory extension, AGENTS.md discovery
and generation, and a progressive-disclosure skills system with three default
skills. All context additions are capped and measured.

## Task 0 — BM25

Command:

```bash
PYTHONPATH=. venv/bin/python tests/test_bm25.py
```

Observed:

```text
all bm25 tests passed
```

`tool_search.py` regression:

```text
all CLI regression tests passed
```

## Task 1 — Memory

M1:

```bash
cd /tmp/memory_cli2
HOME=/tmp/memory_home2 DEEPSEEK_CLI_HOME=/tmp/memory_home2/.deepseek-cli \
venv/bin/python deepseek_cli.py --tools auto \
  "remember that the project uses Python 3.11 and pytest"
```

Observed:

```text
tool search_tools {"query": "memory"}
tool memory_write {"arguments": {"target": "long_term", "content": "- Project uses Python 3.11 and pytest.\n", "mode": "append"}}
  result memory_write wrote long_term memory to /tmp/memory_home2/.deepseek-cli/memory/MEMORY.md.
```

M2:

```bash
cd /tmp/memory_cli2
HOME=/tmp/memory_home2 DEEPSEEK_CLI_HOME=/tmp/memory_home2/.deepseek-cli \
venv/bin/python deepseek_cli.py --show-preamble
```

Observed:

```text
[memory] injected 1 memories (43 chars)
## Long-term memory (auto-retrieved)
- Project uses Python 3.11 and pytest.
```

M3:

```text
memory_search("pytest") returned the Python 3.11/pytest memory.
```

M4:

```bash
PYTHONPATH=. venv/bin/python tests/test_memory.py
```

Observed:

```text
all memory tests passed
```

The test writes 250 entries and verifies `MEMORY.md` stays at or below 200 lines
and archive files are created.

M5:

```bash
cd /tmp/memory_cli2
HOME=/tmp/memory_home2 DEEPSEEK_CLI_HOME=/tmp/memory_home2/.deepseek-cli \
venv/bin/python deepseek_cli.py --no-memory --show-preamble
```

Observed:

```text
ABSENT
```

M6:

Measured with `--show-preamble`: empty baseline versus memory/AGENTS/skills
added was `460` bytes, far below 4000 characters.

## Task 2 — AGENTS.md

A1:

```bash
cd /tmp/agents_test2
HOME=/tmp/agents_home venv/bin/python deepseek_cli.py --generate-agents
```

Observed in generated `AGENTS.md`:

```text
# Commands
- Test: `pytest`
```

A2:

```bash
cd /tmp/agents_test2
HOME=/tmp/agents_home venv/bin/python deepseek_cli.py --show-preamble
```

Observed:

```text
## Project instructions (AGENTS.md)
# Fixture Repo
```

A3:

```bash
cd /tmp/agents_test2
HOME=/tmp/agents_home venv/bin/python deepseek_cli.py --no-agents --show-preamble
```

Observed:

```text
ABSENT
```

A4:

```bash
cd /tmp/agents_trunc
HOME=/tmp/agents_home venv/bin/python deepseek_cli.py --show-preamble
```

Observed:

```text
[AGENTS.md truncated at 500 lines]
```

A5:

```bash
cd /tmp/agents_test2/sub/dir
HOME=/tmp/agents_home venv/bin/python deepseek_cli.py --show-preamble
```

Observed:

```text
## Project instructions (AGENTS.md)
# Fixture Repo
```

## Task 3 — Skills

S1:

```bash
cd /tmp/skills_test
HOME=/tmp/skills_home DEEPSEEK_CLI_HOME=/tmp/skills_home/.deepseek-cli \
venv/bin/python deepseek_cli.py --show-preamble
```

Observed:

```text
## Available skills
- skill-creator: Author a new DeepSeek CLI SKILL.md with valid frontmatter.
- review: Perform structured code review with prioritized findings.
- agents-init: Generate AGENTS.md with the CLI's repo scanner.
```

The preamble did not include skill bodies.

S2:

```bash
cd /tmp/skills_live
HOME=/tmp/skills_live_home DEEPSEEK_CLI_HOME=/tmp/skills_live/.deepseek-cli \
venv/bin/python deepseek_cli.py --tools auto \
  "Use the hello skill and follow its instructions."
```

Observed:

```text
tool use_skill {"name": "hello"}
  result use_skill ---
BANANA
```

S3:

```text
[skills] more than 30 skills found; truncated to 30
30
```

S4:

```text
Report findings by severity: critical, high, medium, low.
# Review Checklist
Error: unsafe skill file path.
```

S5:

Second-turn debug request prompt was:

```text
[request-prompt] {"chat_session_id": "...", "prompt": "Now just say ready."}
```

The first skill body did not persist into the second request.

## Task 4 — Default Skills

D1:

```text
- agents-init: Generate AGENTS.md with the CLI's repo scanner.
- review: Perform structured code review with prioritized findings.
- skill-creator: Author a new DeepSeek CLI SKILL.md with valid frontmatter.
```

D2/D3/D4:

```text
skill-creator BODY_OK True
review BODY_OK True
agents-init BODY_OK True
# SKILL.md Frontmatter
# Review Checklist
```

## Task 5 — Preamble Assembly

P1 baseline:

```text
baseline=7654 bytes
```

P2 installed memory + AGENTS.md + 3 skills:

```text
full=8114 delta=460 bytes
```

P3 30 skills:

```text
many=8294 delta=640 bytes
```

P4 caching:

```text
identical
```

## Task 6 — Documentation and Cleanup

README and `.gitignore` updated. Temporary fixtures live under `/tmp` and do not
enter the repository.

## Known Limitations

- Memory injection uses BM25 plus a recency fallback when the cwd/AGENTS query
  has no lexical match.
- Skill frontmatter parsing is intentionally simple and supports single-line
  `name` and `description` fields.
- `/agents-init` drafts a practical starter file; users should refine commands
  and layout notes.
- The API vision path remains future work and requires a DeepSeek API key.

## First 5 Commands

```bash
cp examples/extensions/memory.py ~/.deepseek-tui/extensions/
cp -r examples/skills/* ~/.deepseek-cli/skills/
venv/bin/python deepseek_cli.py --show-preamble
venv/bin/python deepseek_cli.py --tools auto "remember that this project uses pytest"
venv/bin/python deepseek_cli.py --tools auto "use the review skill on the current diff"
```
