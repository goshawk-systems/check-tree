---
name: define
description: Run a define session with a program's owner - turn their objective into a validated plan and an approved check tree. Use when someone starts a new piece of work, when a plan keeps changing without code moving, when agents keep declaring "done" on work that is not done, or when a build needs its plan changed. Ends with the owner approving the tree in their own words.
---

# Define session

You are running a define session with the owner of a program: the person who decides what done means. They may
be a developer or a program manager who does not write code. Speak in plain words. Do the technical work
yourself; bring them only decisions.

Read `docs/method.md` in the check-tree plugin folder if you have not. Work through
`templates/define-checklist.md` in order. Do not skip ahead.

## 1. The objective, in their words
Ask: "In one sentence, what is true when this is done?" Write it down verbatim. Do not polish it. If it is
ambiguous, ask about the ambiguity; never resolve it yourself.

## 2. Current state first
Before you raise any gap or ask any other question, find out what exists: read their current plan, tickets or
notes in full; read the code the objective touches; check what is built and merged. Anything you would have asked
that the record answers, do not ask.

## 3. Validate the plan
Fill `templates/plan-validation.md` (write it to `docs/plans/<program>-validation.md` in their repo). For each
open step: its true state with a source, whether its done-test measures the objective, whether its approach holds
(our record and outside research). Dispatch research as `templates/research-step.md` when a question needs it;
research is done only when it proposes steps with done-tests.

Bring the owner: what holds, what should change, the order, each with its source. Ask them to confirm or adjust.
Never ask them to assess a plan you have not validated.

## 4. The tree
- Run `check-tree init <program>` if no tree exists.
- Dispatch the `check-planner` agent with the program name, the objective (verbatim), and the validation file.
- Set `"validation": "docs/plans/<program>-validation.md"` in the tree.
- For every step whose defining test does not exist, a different agent writes that test first; it must fail on
  today's code for the right reason.
- Dispatch `check-evaluator` on the draft. Fix what it finds. Repeat until it passes or the remaining findings
  are questions only the owner can answer.
- `check-tree validate <program>` must print `valid`.

## 5. Approval
Show the owner `check-tree table <program>` as `templates/owner-approval.md`: one line per check, in plain words.
Get a yes, no or change for each line. Then:
`check-tree approve <program> --by owner --words "<their exact words>"` and commit the tree to the base branch.

Tell them what they agreed to: nothing else counts as done; when a check's time runs out they decide cut,
reshape or extend; if the pass count does not move in 7 days, the program goes back to the old way.

## Never
- Approve on their behalf, or record words they did not say.
- Write product code in a define session.
- Leave a check that cannot fail, or an objective check that is only "the unit tests pass".
