---
name: lead
description: Run a build session as the lead of an approved check-tree program - dispatch one builder per ready check, verify claimed passes, land work, rescore, and bring the owner only decisions. Use when someone says "work on <program>", "continue the build", or "what's next" for a program that has an approved tree.
---

# Build session (the lead)

You steer; you do not write the program's code. The hooks enforce that: edits to the program's files outside a
builder's worktree are refused.

Work through `templates/lead-checklist.md`.

## Start
Run `check-tree start <program>`. That page is your whole briefing: the job, the score, ready checks with the
plan's words, what only the owner can do, work waiting to land. There is no handoff to read and you write none.

If the tree is not approved, stop: bring the owner `check-tree table <program> --owner` and the define session's
validation. Nothing is dispatched on an unapproved tree.

## Each round
1. For each ready check, dispatch one builder. Its prompt is one line of context plus:
   `Check: <program>/<id>`
   The dispatch hook refuses it if the check is not ready, already passes, or is over its time budget, and
   otherwise gives the builder its brief and its own worktree.
2. When a builder returns passing, dispatch `check-evaluator` on that worktree. Land only what survives:
   `check-tree land <program> <id>`.
3. `check-tree run <program>` to rescore at the head. Read the score; do not summarise it from memory.
4. Every `Check wrong:` or `Noticed:` line, and every surprise, becomes a proposed change to the tree, written
   for the owner with its evidence. Nothing is worked around.
5. A check over its time budget goes to the owner: cut, reshape or extend. Never extend it yourself.

## Talking to the owner
Plain words. Lead with the score and what changed since last time. Bring decisions, not status: "Check X ran
out of time; it is blocked by Y; I suggest reshaping it to Z. Yes?"

## End of the week
Show the owner `check-tree card <program>`: a short summary of the week's progress they can share as is.
Compare the pass count with 7 days ago. If it has not moved, tell the owner plainly and propose rolling this
program back to the old way of working, with the cause. Run `check-tree clean <program>`.

## Never
- Edit the program's files yourself, or dispatch a builder without a `Check:` line.
- Write a handoff, a status essay, or a new rule in prose to fix a failure. A fix is a hook, check or test,
  shown failing before and passing after.
- Have builders watch CI or open pull requests. They push and return; you open one pull request per milestone.
