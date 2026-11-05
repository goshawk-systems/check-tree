---
name: work-by-capability
description: How to size and land work. Use when planning a step, opening a PR, or noticing that a work item has many PRs of tests and tooling. A step is a finished piece of one capability; its fixes, tests and harness ride in one PR; a passing check proves the step, it is not the step.
---

# Work by capability, not by machinery

An audit of one team found 12% of merged lines were product code, and one work item had 19 PRs of test
machinery. Each PR was small and green; the user could do nothing new. Work was measured by checks and PRs,
not by what people could now do.

## A step
A step is a finished piece of ONE capability: something a user or caller can do that they could not do
before ("a patient can reschedule from the text message", "the API returns the next open slot").
- Name the capability in one plain sentence, in the user's words.
- Name how you will see it work (see `done-at-the-consumer`).
- A step that only adds a test, a fixture, a script, a flag or a refactor is not a step. It belongs inside a
  step.

## One PR per step
- The fixes, tests and harness a step needs ride in that step's ONE PR. Do not split them into a PR for the
  test tool, one for the fixture, one for the fix.
- A step that is too large to land as one PR is too large: cut it to a smaller capability, not to smaller
  layers of the same one.
- Open as a draft. Iterate locally. A draft runs no checks, so when the capability works run them on the draft:
  `gh workflow run pr-check.yml -f pr=<n>` (it runs every job on the PR's head and posts `all-green` on that commit).
  Mark the PR ready once, after `all-green` is green on its current head: `gh pr ready` is refused (by
  `pr_open_rules.py`) until it is, and a new push starts over. Marking it ready then reuses that result: the jobs do
  not run twice. A ready PR whose `pr-check` fails on a real test or gate failure is sent back to draft with a
  comment naming the failed jobs and tests (`recover.yml`); a cancelled run or a lost runner never does that.
  Every push to a ready PR starts CI and costs money (see `spend`).

## A check passing proves the step; it is not the step
Do not write code to turn a check green. Build the capability, then see the check go green. If the check passes
and the capability does not exist, the check is wrong: say so, do not ship it.

## Stop signs
- A third PR for the same work item that adds no user-visible capability.
- A PR whose description is about the test tool, not about what changed for a user.
- A plan whose steps are layers (schema, service, harness, UI) instead of capabilities.
When you see one, stop and cut the step differently.

## Where it is enforced
check-tree's objective check is measured at the consumer; a harness check proves tests exist and failed
first. The lead should read the product-code share of merged lines, not the number of PRs.
