# AGENTS.md — DeepSeek CLI working rules

This file governs how the agent behaves when working inside this
repository. It is loaded into the preamble on every turn.

## Response format

Every response is exactly one of:

  1. One sentence of intent, then one or more tool calls.
  2. A final answer, optionally followed by <<DONE>> on its own line.

Prose with no tool call and no final answer is an incomplete turn. The
harness replies with "Continue." and lets you carry on.

## Tool-call format

Any of these shapes is accepted, and several may appear in one response:

  * <tool_call>{"name": "tool_name", "arguments": {...}}</tool_call>
  * a freeform patch block (see "Editing files" below)
  * a DSML invoke block with named parameters

Every call the harness finds is executed, in the order it appears.

## Completion marker

<<DONE>> is optional. When a task is complete you may write a single
line containing exactly <<DONE>>, with a blank line before it. It is
a control signal, it is never shown to the user, and stopping with a
plain final answer is just as good — the harness detects both.

## Single tool call per response

Prefer one tool call per response: it keeps results easy to read.
This is a preference, not a rule — if you emit several, all of them
run, in order.

## No dangling preambles

Never write "I'll run..." and then stop without the tool call.
If you intend to call a tool, emit the tool call in the same
response, immediately after the preamble. A preamble without a
tool call is an incomplete turn.

## Planning is optional

Use update_plan if it helps you organize your work. It is not
required. The user's explicit instructions always take priority
over any planning suggestion.

## Never forget these rules

The no-dangling-preamble rule is non-negotiable. Completion and call
formatting are up to you.

## Single-task turns

Prefer an atomic step per turn. When a task needs several tool calls
they may be emitted together — the harness runs them all in order —
but keep each step small enough to check. Avoid scaffolding many
unrelated files from one prompt.

## Editing files

Use `apply_patch`. It is a FREEFORM tool — emit the patch directly,
do NOT wrap it in JSON.

Correct:

    *** Begin Patch
    *** Update File: path/to/file.py
    @@ context
    -old
    +new
    *** End Patch

Wrong (do not do this): wrapping the patch in a JSON tool-call object
(with "name" and "arguments" keys), or using any other editing tool
such as edit_file or write_file.

The tool name is exactly `apply_patch`.

## Patch operations

Add a file:
    *** Begin Patch
    *** Add File: /path/file.txt
    +content line 1
    +content line 2
    *** End Patch

Update a file:
    *** Begin Patch
    *** Update File: /path/file.py
    @@ def function():
    -    old_line
    +    new_line
    *** End Patch

Delete a file:
    *** Begin Patch
    *** Delete File: /path/file.py
    *** End Patch

Move/rename a file:
    *** Begin Patch
    *** Update File: /path/old.py
    *** Move to: /path/new.py
    @@ context
    -old
    +new
    *** End Patch

Every line inside a hunk must begin with +, -, or a space. A
context line missing its leading space breaks the patch.

Fix the root cause, not the symptom. Keep changes minimal. Do not
fix unrelated bugs. Do not add comments unless asked.

## Shell discipline

Prefer rg over grep. Read files in chunks of <=250 lines. Output is
truncated at 10KB or 256 lines. Use exec_command for all shell work.

## When to stop

If a task is complete, write the final answer — with <<DONE>> on its
own line if you like. If a task is not complete, emit the next tool
call. Do not write a summary of what you have done unless the task
is done.
