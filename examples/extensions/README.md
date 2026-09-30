# DeepSeek TUI Extensions

This directory contains example extensions for the DeepSeek TUI plugin system.

---

## Extension Contract

An extension is a plain Python `.py` file dropped into one of the search directories.
It may define two things:

### 1. `register()` — required

Returns one `Tool` or a list of `Tool` objects. A `Tool` is created with the
`@tool` decorator from `deepseek`:

```python
from deepseek import tool

@tool
def my_function(arg: str) -> str:
    """One-line description.

    Args:
        arg: What this argument does.
    """
    return f"result: {arg}"

def register():
    return [my_function]   # or a single Tool: return my_function
```

### 2. `COMMANDS` — optional

A `dict` mapping slash-command names to zero-argument callables:

```python
COMMANDS = {
    "/mycommand": lambda: print("hello from my command"),
}
```

> **Note**: Built-in TUI commands (`/help`, `/mode`, `/tools`, `/new`, `/clear`,
> `/thread`, `/exit`, `/extensions`, `/reload`, `/model`, `/thinking`, `/search`)
> always take priority. If your command name clashes, a warning is printed and
> your command is skipped.

---

## Minimal Template

```python
"""
my_extension.py — DeepSeek TUI extension: <brief description>.

Drop into ~/.deepseek-tui/extensions/ and restart or /reload.
"""

from deepseek import tool


@tool
def my_tool(input: str) -> str:
    """Does something useful.

    Args:
        input: The input string.
    """
    return f"processed: {input}"


def register():
    return [my_tool]
```

---

## Search Paths

Extensions are loaded from these directories, in order (later overrides earlier
for name conflicts):

1. `~/.deepseek-tui/extensions/*.py`
2. `<cwd>/.deepseek-tui/extensions/*.py`

Files whose names start with `_` are skipped (use them for helper modules).

---

## Loading Rules

| Situation | Behaviour |
|-----------|-----------|
| File has no `register()` | Skipped with a stderr warning |
| `register()` raises an exception | Skipped; other extensions still load |
| Import fails | Skipped with a stderr error line |
| Two extensions define the same tool name | Later path wins; a warning is printed |
| Extension `COMMANDS` key clashes with built-in | Built-in wins; a warning is printed |

---

## Examples in This Directory

| File | Description |
|------|-------------|
| [`weather.py`](weather.py) | Fetches current temperature via free Open-Meteo API (no key needed). |
| [`notes.py`](notes.py) | Saves and lists Markdown notes; also demonstrates `COMMANDS`. |

---

## How to Use

1. **Copy** an example (or your own extension) to `~/.deepseek-tui/extensions/`:
   ```bash
   mkdir -p ~/.deepseek-tui/extensions
   cp examples/extensions/weather.py ~/.deepseek-tui/extensions/
   ```

2. **Restart** `python deepseek_tui.py`, or type `/reload` if already running.

3. The startup log (`stderr`) will confirm: `[tui] loaded N extension tool(s): ...`

4. Type `/extensions` in the TUI to see a summary of all loaded extensions.

5. Use the tool in a conversation — in `/mode auto` the model can call it
   automatically; in `/mode manual` you approve each call.
