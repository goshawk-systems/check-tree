# Working in this repo

<!--
A starter instructions file for a repo run by AI agents. Two rules shaped it:
  1. Keep it short. Long instruction files get skimmed; adherence falls as instructions pile up.
     tests/test_claude_md_size.py keeps this file under 200 lines and lets it only shrink.
  2. Every rule says what enforces it, in brackets. A rule tagged [prose] is a hope. When a [prose] rule keeps
     being broken, turn it into a hook, a test or a CI check, and change its tag. Never answer a broken rule
     with another sentence.
-->

Read these before changing code. A hook checks that you opened them. [hook: required_reads.py]

```required-reads
docs/ARCHITECTURE.md
```
<!-- List files that exist in your repo. A listed file that does not exist yet is skipped, not enforced. -->

## How work is planned and done
- A program's plan is its check tree: `.check-tree/programs/<program>/checks.json`. Done means a check passes at
  the head of the base branch, computed by `check-tree run`. Nobody declares done. [check-tree]
- A lead does not edit a program's files. It dispatches one builder per ready check, with `Check: <program>/<id>`
  in the prompt. [hook: check_scope.py, check_brief.py]
- A builder cannot finish while its check fails, while work is uncommitted, or after changing a frozen test.
  [hook: check_stop.py]
- If a check is wrong, say so on a `Check wrong:` line with evidence. Only the owner changes the tree.
  [check-tree: approval pins the tree and its frozen files]

## Gated to the owner. Write the exact command in your report and stop.
1. A production deploy. [prose: add your own hook or environment protection]
2. Deleting data or infrastructure. [prose]

## Agents
- Default model is the cheaper one. The expensive model needs a `MODEL-APPROVED: <who> <date> <why>` line in
  the dispatch prompt. [hook: model_gate.py]
- A subagent does not start other agents, open pull requests, or watch CI. It pushes and returns its commit.
  [hook: model_gate.py, no_subagent_pr.py, no_ci_polling.py]
- At most N agents run at once (`guards.agent_cap`). [hook: agent_cap.py]

## Tests and merging
- The pipeline runs the tests. Nobody runs the full suite by hand to "check first". [CI]
- Never weaken a check to make it pass: no raised timeout, no deleted test, no loosened assertion. If a check is
  wrong, leave it red and say why. [check-tree: frozen files; CI]
- Prove a change where it is used (the screen, the API response, the job's output), not only in a unit test.
  [prose]
