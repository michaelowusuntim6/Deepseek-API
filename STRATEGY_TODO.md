# STRATEGY.md implementation checklist

Tick a box only after the task's attached test passes with real output.

- [x] Task 1 — Response processing (search RESPONSE only; strip <<DONE>>)
- [x] Task 2 — Tool call handling (first call only + synthetic errors)
- [x] Task 3 — Completion token handling (<<DONE>> strict rules)
- [x] Task 4 — Nudge on no tool call and no <<DONE>>
- [x] Task 5 — Remove the planning rule
- [x] Task 6 — Config file and stall detector
- [x] Task 7 — Update AGENTS.md
- [x] Task 8 — Update README.md
- [x] Regression suite — tests/test_cli.py
- [x] Live verification
- [x] Commit (no push)
