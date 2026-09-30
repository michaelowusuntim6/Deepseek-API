---
name: review
description: Perform structured code review with prioritized findings.
---

# Review

## When to use
Use when the user asks for a code review or a diff review.

## Steps
1. Read the diff.
2. Find correctness bugs first.
3. Check style and maintainability second.
4. Check tests and missing coverage third.
5. Use `references/checklist.md`.

## Output
Report findings by severity: critical, high, medium, low.
