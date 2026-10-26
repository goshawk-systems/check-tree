# Define checklist

A define session is done when every box is ticked. Do not skip ahead: a tree built on an unvalidated plan is
a faster way to build the wrong thing.

## Objective
- [ ] The owner's objective, verbatim, in one sentence: ______________________________
- [ ] Ambiguities written as questions for the owner, not resolved by an agent.

## Current state (before any question or gap is raised)
- [ ] The current plan read in full.
- [ ] For each step: built / partly built / not started, with the file, commit or run that shows it.
- [ ] Every "blocker" checked against the record; only real ones remain.

## Validation (templates/plan-validation.md)
- [ ] Every open step: true state, whether its done-test still measures the objective, whether its approach holds.
- [ ] Each claim has a source (file, commit, run, or external reference).
- [ ] Research steps written as question + internal half + external half, each ending in proposed steps with done-tests.
- [ ] What holds, what should change, and the order, ready for the owner.

## Tree
- [ ] `check-planner` drafted `.check-tree/programs/<program>/checks.json` from the validated plan.
- [ ] Exactly one objective check, measured where the outcome is used (not "unit tests pass").
- [ ] Every component whose test does not exist has a harness check: the test exists and fails today.
- [ ] Defining tests listed in `frozen`; written by a different agent than the one who will make them pass.
- [ ] Every check has `fails_when`, `appetite_hours` (under a day each), and `plan_text`.
- [ ] `check-evaluator` attacked it; every finding fixed or written up as an open question.
- [ ] `check-tree validate <program>` prints `valid`.

## Approval (templates/owner-approval.md)
- [ ] The owner read `check-tree table <program>` and said yes or no to each check in plain words.
- [ ] `check-tree approve <program> --by owner --words "<their words>"`, committed to the base branch.
