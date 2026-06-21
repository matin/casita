---
icon: lucide/thumbs-up
---

# Learning From Votes

The feedback loop lives in `src/casita/llm.py` and the CLI in
`src/casita/__init__.py`.

Votes and pass reasons are stored in SQLite. During ranking, Casita builds:

- inline feedback for listings in the current batch
- a capped few-shot block of recent up/pass examples
- an audit prompt exposed through `casita analyze-prefs`

`analyze-prefs` reads the votes and compares revealed preference against the
static ranking policy. It proposes contradictions and new rules, but it never
edits code. A human decides whether a proposed rule belongs in the prompt.

## Ways This Could Go Further

The loop could gain better fixtures, better diff output, or clearer aging of
old examples. The important property to preserve is reviewability: revealed
preference should become policy through an intentional code change.
