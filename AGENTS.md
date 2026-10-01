# AGENTS.md — DeepSeek CLI working rules

This file governs how the agent behaves when working inside this
repository. It is loaded into the preamble on every turn.

## Response format

Every response is exactly one of:

  1. One sentence of intent, then one or more <tool_call> blocks.
  2. A plan update via update_plan.
  3. A final answer ending with TASK COMPLETE on its own line.

Prose with no tool call and no TASK COMPLETE is an incomplete turn.
Do not write it.

## Mandatory planning

On any prompt longer than 20 words, the first tool call must be
update_plan. Plan steps are 1-sentence, 5-7 words each, with status
pending/in_progress/completed. Exactly one step is in_progress at a
time.

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

If a task is complete, write the final answer and end with TASK
COMPLETE. If a task is not complete and you need more turns, emit
the next tool call. Do not write a summary of what you have done
unless the task is done.
