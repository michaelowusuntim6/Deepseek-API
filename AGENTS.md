# AGENTS.md — DeepSeek CLI working rules

This file governs how the agent behaves when working inside this
repository. It is loaded into the preamble on every turn.

## Response format

Every response is exactly one of:

  1. One sentence of intent, then a single <tool_call> block.
  2. A final answer followed by the completion marker <<DONE>>.

Prose with no tool call and no <<DONE>> is an incomplete turn.
Do not write it.

## Tool-call format

Call tools with exactly this format and nothing else:

    <tool_call>{"name": "tool_name", "arguments": {...}}</tool_call>

Do not wrap the call in DSML, XML, <|tool_calls|>, or any other markup.
Do not include more than one sentence of prose before the <tool_call> block.

## Completion marker

When a task is complete, write a single line containing exactly:

    <<DONE>>

The line must have a blank line before it and a blank line after
it. It must be the only content on its line. It is a control
signal and will not be shown to the user.

## Single tool call per response

Emit at most ONE <tool_call> block per response. If you emit
more, only the first will be executed. Wait for the tool result
before emitting the next call.

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

The completion marker, single tool call, and no-dangling-preamble
rules are non-negotiable. They govern every response.

## Single-task turns

Do one atomic thing per turn. If the task requires more than three
tool calls, do the first one and stop. The harness will continue on
the next turn.

Do NOT scaffold multiple files from one prompt. Do NOT write a
script, run it, and verify it in one turn unless the prompt is short
and single-purpose. Split the work.

## Editing discipline

Use apply_patch. Never applypatch or apply-patch. Use the exact
format:

    *** Begin Patch
    *** Update File: path/to/file
    @@ context
    -old
    +new
    *** End Patch

Fix the root cause, not the symptom. Keep changes minimal. Do not
fix unrelated bugs. Do not add comments unless asked.

## Shell discipline

Prefer rg over grep. Read files in chunks of <=250 lines. Output is
truncated at 10KB or 256 lines. Use exec_command for all shell work.

## When to stop

If a task is complete, write the final answer and end with the
completion marker <<DONE>> on its own line. If a task is not
complete and you need more turns, emit the next tool call. Do not
write a summary of what you have done unless the task is done.
