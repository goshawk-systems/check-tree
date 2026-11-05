# Working in {{repo_name}}

Short on purpose. Every rule names what enforces it, in brackets. A rule tagged `[prose]` is a hope: when one is
broken twice, turn it into a hook, a test or a CI check and change its tag. A rule with no tag is deleted.
Settings every tool reads live in `.check-tree/repo.json`.

Read these before changing code. A hook checks that you opened them. [hook: required_reads.py]

```required-reads
docs/ARCHITECTURE-RULES.md
docs/RULINGS.md
```

## Gated to the owner. Write the exact command in your report and stop.
1. A production deploy (any workflow matching `prod.deploy_workflow_patterns`). [prose: protect the environment in
   GitHub so a deploy needs the owner's approval]
2. A write to a production database, or a change to production infrastructure or permissions. [prose: add a
   cloud-guard hook listing `prod.*` from repo.json]
3. Deleting data, a table, a module or a cloud resource without proof it is unused. [prose]
Everything else is autonomous. Queue the gated command, then continue with the work that is not gated.

## Rules
1. Never touch another session's checkout (`shared_checkouts` in repo.json). Work in your own clone:
   `check-tree workspace new <name>`. [prose]
2. A test run never reaches a real system: no network, no cloud credentials, no database, unless the test is marked
   `live`. [test: conftest.py]
3. Never weaken a check to make it pass: no raised baseline, no deleted or skipped test, no waiver, no loosened
   assertion. If a check is wrong, leave it red and say why with evidence. [CI: ratchet.py, check-tree frozen files]
4. Never print a secret, a patient or customer value, or a raw production log line: counts and masked shapes only.
   [hook: .githooks/pre-push (gitleaks)]
5. This file stays under 100 lines and only shrinks. [test: tests/test_claude_md_size.py]
6. Prove a change where it is used (the rendered screen, the API response, the job's output), not only in a unit
   test. [CI: scripted_review.py: the PR body's proof section]

## How work is planned
- A program's plan is its check tree (`.check-tree/programs/<program>/checks.json`). Done is a check passing at the
  head of `{{default_branch}}`, computed by `check-tree run`. Nobody declares done. [check-tree]
- A lead steers and does not edit a program's files; one builder per ready check, `Check: <program>/<id>` in the
  prompt. A builder cannot finish while its check fails. [hook: check_scope.py, check_brief.py, check_stop.py]
- Every work item has an id (`work_item_pattern` in repo.json) before work starts. [CI: scripted_review.py]

## Merging
- The merge unit is a finished step of ONE capability, proven at its consumer. Not a backlog, not a milestone.
  [CI: PR file cap `max_pr_files`]
- Open the pull request as a DRAFT. Iterate locally. Mark it ready once, when the change is done: every push
  costs CI minutes. [CI: the workflows run on ready_for_review, not on draft pushes]
- One open pull request per work item. [CI: scripted_review.py]
- The merge queue is the only path to `{{default_branch}}`, and it runs the full suite. Nobody merges by hand and
  nobody runs the full suite by hand to "check first". [CI: Mergify queue, required status `all-green`]
- Branch from `origin/{{default_branch}}`. Stage by explicit path, never `git add -A`. [prose]

## Agents
- The cheaper model is the default. The expensive one needs a `MODEL-APPROVED: <who> <date> <why>` line in the
  dispatch prompt. [hook: model_gate.py]
- A subagent does not start agents or open pull requests. [hook: model_gate.py, no_subagent_pr.py]
- Agents never watch or poll CI (`gh run watch`, `--watch`, a sleep loop): push, return the commit, and the
  pipeline runs the checks. [hook: no_ci_polling.py]
- At most `guards.agent_cap` agents at once. [hook: agent_cap.py]

## Commands
- Tests: `{{test_command}}`   Lint: `{{lint_command}}` (pre-push runs lint on changed files, never tests).
- Run tests with the fence on (it is on by default): see `conftest.py`.
