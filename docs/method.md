# The method: how work is defined and run so it moves

The hooks in this repo enforce a way of working. This page is that way of working. It is the part that matters
most: a team can write a bad check tree as easily as a bad plan, and machinery alone does not stop spinning.

Two kinds of session, never mixed: **define** (with the owner, decides what done means) and **build** (run by
a lead, makes checks pass). In Claude Code: `/check-tree:define` and `/check-tree:lead`.

## Why it is shaped this way

Our own audit, after months of running agent teams, before this method:
- 0 of 51 planned tasks had a test that could show they were done.
- One plan was revised 23 times before any code was written.
- 10% of the lines agents changed in a week were product code; half the changes touched no product file.
- 9 of 12 complaints about execution had been answered with a new rule, and no rule was ever removed. Every rule
  checked an input (did you read it, did a reviewer pass it); none checked whether the work moved.

The published lessons of teams building with long-running agents point the same way: the plan is a list of
pass/fail tests written first, agents may not edit them, a separate skeptical evaluator judges claimed passes,
and instruction files stay short. This method puts those together.

## Defining the work (a define session, with the owner)

1. **The objective, in the owner's words.** Verbatim, one sentence. Not a summary. If the words are ambiguous,
   the ambiguity is a question for the owner, never something an agent resolves.
2. **Current state first.** Before raising any gap or asking any question, establish what already exists: the
   current plan in full, what is built, what is merged, what each step's status really is. A research note or a
   past report is a lead to check, never a finding to pass on. Most "blockers" agents raise are answered in the
   record.
3. **Validate the plan before the owner is asked to judge it.** For every open step, with evidence:
   - its true state (built, partly built, not started), with the file, commit or run that shows it;
   - whether its done-test still measures what the objective needs, given what has been learned;
   - whether its approach holds, from research inside the record and outside it.
   The owner is brought what holds, what the evidence says should change, and in what order, each with its
   source. Never a plan to assess that nobody has validated. (Template: `templates/plan-validation.md`.)
4. **Research is a step with an end.** A question, an internal half (what our own record and code say) and an
   external half (what published practice says), written down. It is done when it proposes plan steps, each
   with a done-test. Research that ends in "more research needed" has not ended. (Template:
   `templates/research-step.md`.)
5. **Tests before code, by a different agent.** For each step whose defining test does not exist, a different
   agent writes it first, and it is committed before any product work. Its harness check uses
   `check-tree red-at <that commit> -- <the test command>`: it passes only if the test FAILED at that commit for a
   real reason (not a missing file or import). Because it looks at that fixed commit, it stays green after the
   work is done. To pin the reason, add `--expect "<text the failure must contain>"`, such as the assertion
   message the test prints when the behaviour is missing. Several defining tests: one harness check per test
   file, or several `red-at` runs joined with `&&` in one check.
   When the code a test needs does not exist yet, an import error would be rejected as "could not run". Make the
   test fail on purpose instead, with a marker the harness can pin, for example in Python
   `assert hasattr(invoices, "pay_invoice"), "MISSING: invoices.pay_invoice"`, and then
   `red-at <commit> --expect "AssertionError: MISSING" -- <test command>`.
   Pin each test, not each file: run `red-at` on each test (for pytest, `file.py::test_name`), so one
   test failing for the right reason cannot cover another failing for the wrong one.
   Write tests a builder cannot game: check every computed result for at least two different inputs (a
   hard-coded answer then fails), and assert on the behaviour the user sees, not on internal details.
   A step the old plan called done gets its own test too, inside the objective check, so it cannot quietly
   break. Approval pins every file under the test folders (`test_paths`), not only the named tests.
   Before approval, show the tests can pass: an honest reference version, written in a scratch copy and
   never committed, passes them all. A test nothing can pass is as useless as one everything passes.
   Pin the error as it is raised (exception name and message), never a word alone or an alternative like
   `"MISSING|AssertionError"`: those match any failure, and a pin that matches any failure proves nothing.
   `red-at` ignores lines that only echo the test's source and prints the line it matched, so check that line. If the place a user meets the outcome does not exist yet (no web page, no email service), ask
   the owner what the objective will be measured against for now, and write it in the check's `proven_with`
   (for example "a fake card processor in tests"). The owner sees it before approving.
6. **The planner drafts the check tree** from the validated plan (`check-planner`). Every check carries the plan
   step's own words (`plan_text`), so the plan is handed to the builder, not looked up.
7. **The evaluator attacks it** (`check-evaluator`): any check that could pass while its outcome is false, any
   part of the objective with no check. Fix rounds until it holds.
8. **The owner approves in plain words, one line per check.** Show them `check-tree table <program> --owner`:
   what done means, when it fails, what it is proven with, the time budget. No ids, file names or hashes. Their
   words are recorded with the approval, which pins the tree and its frozen tests. Any later edit unapproves it.
9. **Retire the old plan.** Put a line at the top of the old plan document saying it is superseded by the check
   tree, and record dropped steps in the tree's `dropped` list, with the owner's words. Otherwise agents keep
   working from the old document.

### Order of commits
1. The validation file, `.check-tree/config.json` and the `.gitignore` lines `init` adds.
2. The defining tests (they fail now). Note this commit: it is `<TESTS-COMMIT>` in the harness checks.
3. The tree, with `<TESTS-COMMIT>` replaced and `areas` set to the folders this program really owns;
   `check-tree validate` until `valid`. If the tests are amended later, commit them and update `<TESTS-COMMIT>`
   to the new commit (the tree then needs the owner's approval again).
4. `check-tree approve ...`, then commit the tree again (it now carries the approval). From here it binds.

Checklist: `templates/define-checklist.md`.

## Running the build (a build session, run by a lead)

- **Start from the scoreboard, not a handoff.** `check-tree start <program>` is the whole briefing: the job, the
  score, the ready checks with the plan's words, what only the owner can do, what is waiting to land. No prose
  handoffs; the scoreboard and the progress log are the state.
- **One builder per ready check**, dispatched with `Check: <program>/<id>`. If several checks change the same
  files, chain them with `needs` so builders do not collide, or expect to resolve conflicts when landing. The builder gets a short brief
  instead of a reading list, works in its own worktree, and cannot finish while its check fails.
- **The evaluator on every claimed pass.** It runs the check itself and tries to make the pass false: it
  tries inputs the tests did not use, and looks for hard-coded answers.
- **A builder on a product check changes only the product.** The stop hook sends it back for any change outside
  the program's `areas` (plus the check's own `may_change`): a new test helper, fixture or file that changes
  what the tests run against is the owner's call, not the builder's.
- **The lead does not write the program's code.** It steers: dispatch, land passing work, rescore.
- **Every gap becomes a proposed change, back to the owner.** A builder's `Check wrong:` or `Noticed:` line, a
  research result, a surprise: each goes back through define steps 3 to 8. Nothing is quietly worked around.
- **Every check has a time budget** (`appetite_hours`). When it runs out, work on it stops and the owner decides:
  cut it, reshape it, or extend it. Work is never extended by default.
- **If the pass count does not move for 7 days, roll back** to the old way of working for that program, with the
  cause written down. The method has to earn its place.
- **A process change counts only when it is mechanical**: a hook, a check or a test, shown failing before and
  passing after. A new sentence in an instructions file, a memory note or a handoff section is not a change.

Checklist: `templates/lead-checklist.md`.

## What the owner does, and only the owner

- States the objective.
- Approves each check tree, in their own words.
- Decides when a check's time budget runs out: cut, reshape or extend.
- Approves any change to an approved tree.

Everything else is the lead's and the builders'. An agent that says it is "blocked on the owner" for anything
else is not blocked.
