Read ~/Deepseek-API/docs/Strategy.md. It is the specification for
response processing, tool execution, completion signaling, and network
retry in the DeepSeek CLI. Implement it faithfully with the
clarifications below.

Do NOT touch server/, deepseek/auth.py, deepseek/pow.py, or the DSML
parser in deepseek/client.py.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OPERATING MODE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Autonomous. Do not stop to ask questions. When the spec is ambiguous,
make the senior-engineer choice, note it in the commit body, and move
on.

CRITICAL RULE: after EVERY implementation task below, run its
attached test and paste the real command output. Do NOT proceed to
the next task until the current task's test passes. If a test fails,
fix the implementation and re-run the test until it passes.

Create ~/Deepseek-API/STRATEGY_TODO.md with a checkbox list before
starting. Tick each box only after its test passes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CLARIFICATIONS TO THE SPEC
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. COMPLETION TOKEN NAME
   The spec uses "ahoi". Rename the token to:

       <<DONE>>

   Reason: "ahoi" is a real word in German and nautical English and
   could appear accidentally in generated prose or code. <<DONE>> is
   collision-proof and cannot appear in any normal text.

   Strict rules:
     - Exactly one occurrence of <<DONE>> per response.
     - It must be on its own line.
     - Must be preceded by a blank line.
     - Must be followed by a blank line (or end of response).
     - If any rule is violated, do NOT treat the response as complete.

2. NO TOOL CALL AND NO <<DONE>>
   If the response contains neither a tool call nor a valid <<DONE>>,
   nudge the model with this exact message:

       "No tool call or <<DONE>> detected. Finish properly: either
        emit a <tool_call> block now, or write <<DONE>> on its own
        line. Do not write prose."

   Keep a consecutive_failures counter. Reset it to 0 after any
   successful tool call. Increment it on each nudge. Give up after 5
   consecutive failures and end the turn.

3. REMOVE THE PLANNING RULE BUT KEEP update_plan
   - Delete the mandatory-planning rule from AGENTS.md.
   - Delete the planning instruction from the system preamble.
   - Keep update_plan registered in the tool registry so the model
     may still call it if it wants to.

4. TIMEOUT SEMANTICS
   The timeout is a STALL DETECTOR, not a wall-clock cap. It fires
   when no bytes have arrived from the server for the configured
   duration (default 3 minutes). A response that streams continuously
   for 10 minutes will never trigger it.

5. RETRY BEHAVIOR ON STALL
   When the stall detector fires:
     a. If no response bytes were received at all — delete the
        previous message from the chat session, then resend the same
        input as a fresh message.
     b. If partial bytes arrived and then the stream stalled — reuse
        the same message id and re-open the SSE stream. Do not
        resend, do not delete.
   The retry counter tracks consecutive failures of EITHER kind. Any
   successful response resets it to 0.

6. NUDGE MESSAGE HISTORY
   The previous NUDGE_MESSAGES array contained a hardcoded command
   ("ls ~/Downloads/Hugginface/hf_results/"). This is gone. The new
   nudge message is exactly as specified in point 2 above. No
   hardcoded commands, no example paths.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TASK 1 — Response processing
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Per spec Part I:

- Only search RESPONSE fragments. Never search THINK fragments.
- Search for a tool call first. If found, that is the signal.
- If no tool call, search for a valid <<DONE>> using the strict rules.
- Strip <<DONE>> and its surrounding blank lines before rendering the
  response to the user.

TEST 1 — Write a small script that feeds a synthetic SSE stream
containing both THINK and RESPONSE fragments through the response
processor. Verify:
  - the THINK fragment is not searched for markers
  - the RESPONSE fragment is searched
  - a tool call inside THINK is not detected
  - a tool call inside RESPONSE is detected

Paste the test output. Do not proceed until it passes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TASK 2 — Tool call handling
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Per spec Part II:

- Exactly one tool call executes per response.
- If multiple <tool_call> blocks are present, execute only the first.
- Return the first result plus, for each additional call, a synthetic
  error result:

    TOOL RESULT for <name>:
    ERROR: Multiple tool calls in one response. Only the first was
    executed. Re-emit this call separately in the next turn.

- The harness must enforce this regardless of model compliance.

TEST 2 — Mock a response with three <tool_call> blocks. Verify:
  - only the first tool actually executes
  - the model receives the first result plus two error results
  - the errors name the correct tools

Paste the test output. Do not proceed until it passes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TASK 3 — Completion token handling
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Per spec Part III and clarification 1:

- <<DONE>> alone (no tool call) → task complete. No nudge. End turn.
- <<DONE>> with a tool call → tool call wins. Execute the tool.
- Malformed <<DONE>> (missing blank lines, multiple occurrences,
  same line as other text) → NOT complete.
- <<DONE>> is stripped from all user-visible output.

TEST 3 — Five mock responses:
  a. <<DONE>> alone with correct blank lines → turn ends
  b. <<DONE>> with a tool call → tool executes
  c. <<DONE>> inline with text on same line → not complete, nudge
  d. Two <<DONE>> lines → not complete, nudge
  e. <<DONE>> without blank line before → not complete, nudge

Verify in each case that <<DONE>> does not appear in stdout.

Paste the test output. Do not proceed until it passes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TASK 4 — Nudge on no tool call and no <<DONE>>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Per clarification 2:

- When a response has no tool call and no valid <<DONE>>, send the
  nudge message.
- Track consecutive_failures. Reset to 0 on any successful tool call.
- Give up after MAX_NUDGES_PER_TURN = 5 consecutive failures.
- On give-up, write /tmp/prose_stop_<ts>.txt and end the turn.

TEST 4 — Two mock scenarios:
  a. prose-only → successful tool call → prose-only. Assert total
     nudges == 2, counter reset to 0 in the middle.
  b. five consecutive prose-only responses with no tool call.
     Assert: five nudges fire, then turn ends, prose_stop file
     written.

Paste the test output. Do not proceed until it passes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TASK 5 — Remove the planning rule
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Per clarification 3:

- AGENTS.md: remove the "Mandatory planning" / "call update_plan
  first" rule.
- Preamble: remove the instruction that requires update_plan first.
- Tool registry: keep update_plan registered and callable.

TEST 5 —
  grep -c "update_plan first" AGENTS.md        → must print 0
  grep -c "Mandatory planning" AGENTS.md        → must print 0
  grep -c "update_plan" deepseek/agent_tools.py → must print >0
  grep -c "call update_plan" deepseek/client.py → must print 0

Paste the output. Do not proceed until it passes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TASK 6 — Config file and stall detector
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Per spec Parts IV–VII and clarification 4:

- Create ~/Deepseek-API/config.json:

    {
      "network_retry": {
        "response_timeout_minutes": 3,
        "max_consecutive_retries": 5
      }
    }

- Read the config fresh at the start of every request lifecycle.
  Do not cache it across requests.
- The timeout is a stall detector: fires when no bytes arrive for
  the configured duration, not when total elapsed time exceeds it.
- On stall:
    a. No bytes at all → delete previous message, resend.
    b. Partial bytes then stall → reattach to the same message id.
- Track consecutive retry failures. Reset on success.
- After max_consecutive_retries, print:

    Network Connection Error
    The model did not respond after N consecutive attempts.
    The operation has been stopped.

  and stop. No further retries.
- There must never be more than one active timer per logical request.

TEST 6 —
  a. Assert config.json exists and has the correct keys.
  b. Mock a stall (no bytes). Assert the delete-and-resend path fires.
  c. Mock a successful response after two timeouts. Assert the
     counter goes 0 → 1 → 2 → 0.
  d. Mock five consecutive timeouts. Assert the error message
     prints and the process stops. No sixth retry.
  e. Change config.json to a different timeout between two requests.
     Assert the second request uses the new value.

Paste the test output. Do not proceed until it passes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TASK 7 — Update AGENTS.md
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Add these rules to AGENTS.md, keeping the existing tool-call format
section:

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

TEST 7 —
  grep -c "<<DONE>>" AGENTS.md                → must print >= 1
  grep -c "at most ONE <tool_call>" AGENTS.md → must print 1
  grep -c "dangling preamble" AGENTS.md       → must print >= 1
  grep -c "update_plan first" AGENTS.md       → must print 0

Paste the output. Do not proceed until it passes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TASK 8 — Update README.md
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Add a section titled "Response protocol" to README.md that documents:

  - the <<DONE>> completion marker with the strict formatting rules
  - one tool call per response
  - no dangling preambles
  - the config.json network retry settings and where to find them
  - the stall-detector semantics (no bytes for N minutes)
  - the maximum consecutive retries and what happens when reached

TEST 8 —
  grep -c "Response protocol" README.md            → must print 1
  grep -c "<<DONE>>" README.md                     → must print >= 1
  grep -c "network_retry" README.md                → must print >= 1

Paste the output. Do not proceed until it passes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REGRESSION SUITE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Add to tests/test_cli.py:

  a. test_response_search_excludes_thinking
  b. test_tool_call_takes_priority_over_done
  c. test_done_alone_ends_turn
  d. test_done_stripped_from_output
  e. test_done_malformed_not_complete
  f. test_multiple_tool_calls_first_only
  g. test_multiple_tool_calls_synthetic_errors
  h. test_nudge_message_has_no_hardcoded_paths
  i. test_nudge_counter_resets_on_tool_success
  j. test_nudge_counter_dies_after_five_consecutive
  k. test_update_plan_still_registered
  l. test_update_plan_rule_removed
  m. test_config_reloaded_per_request
  n. test_stall_detector_no_bytes
  o. test_stall_retry_resets_on_success
  p. test_stall_max_retries_stops

Run: venv/bin/python -m pytest tests/test_cli.py -v

All must pass before proceeding.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LIVE VERIFICATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Fresh session:

    venv/bin/python deepseek_cli.py

Paste this prompt:

    Read ~/Downloads/Hugginface/ReallyHelpfulClean.md and report:
      - grep -c "^## " on the file
      - grep -c "^- " on the file
      - head -20 of the file

    Report the three outputs and finish.

PASS requires:
  1. The model emits one tool call per turn, not three at once.
  2. After the third tool result, the model writes the report and
     emits <<DONE>> on its own line.
  3. <<DONE>> does not appear in the rendered output.
  4. Zero network retries fire.
  5. Turn ends cleanly without any nudge.

Screenshot the session at the moment the model writes the report and
finishes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMMIT (only after live verification passes)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    git add -A
    git commit -m "Implement Strategy.md: <<DONE>> completion marker, one tool call per response, stall-detector network retry with config.json, optional planning, updated AGENTS.md and README.md"

Do NOT push.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONSTRAINTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  - Do NOT touch server/, deepseek/auth.py, deepseek/pow.py.
  - Do NOT change the DSML parser or the <tool_call> format.
  - Do NOT hardcode retry values — they come from config.json.
  - Do NOT silently execute multiple tool calls.
  - Do NOT render <<DONE>> in user-visible output.
  - Every task's test must paste real command output. No PASS labels
    without evidence.
  - Do NOT proceed to the next task until the current task's test
    passes.
  - Screenshot required for live verification.
