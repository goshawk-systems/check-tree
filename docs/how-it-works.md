# How check-tree works

## The tree

`.check-tree/programs/<program>/checks.json`:

```json
{
  "program": "payments",
  "objective": "A customer can pay an invoice by card and gets a receipt (the owner's words)",
  "areas": ["src/payments/**", "web/checkout/**"],
  "integration_branch": "check-tree/payments",
  "counts_on": "main",
  "checks": [
    {
      "id": "objective",
      "kind": "objective",
      "title": "A scripted customer pays a test invoice end to end and the receipt arrives",
      "run": "npx playwright test e2e/pay-invoice.spec.ts",
      "fails_when": "Any step of paying fails for a real customer, or no receipt is sent",
      "needs": ["h-e2e-spec", "card-charge"],
      "frozen": ["e2e/pay-invoice.spec.ts"],
      "appetite_hours": 6,
      "plan_text": "Customers pay invoices online (plan step 3)"
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `objective` | One sentence, in the owner's words. |
| `areas` | Glob patterns of the files this program owns. Once the tree is approved, those files change only in a builder's worktree for this program. |
| `integration_branch` | Where builders' work collects. Default `check-tree/<program>`. |
| `counts_on` | Where passes count. Default: `base_ref` from the config, usually `main`. |
| `kind` | `objective` (exactly one; measured where the outcome is used), `component`, or `harness` (the test or tool a component check needs, usually "the defining test exists and fails on today's code"). |
| `run` | A shell command run from the repository root; exit 0 means pass. It gets `CHECK_TREE_PROGRAM`, `CHECK_TREE_PROGRAM_DIR`, `CHECK_TREE_ROOT` and `CHECK_TREE_BIN` (the command line, for `red-at`). To run a harness command by hand, set it first:
`export CHECK_TREE_BIN=<path to bin/check-tree>`. Variables that look like credentials (cloud keys, tokens, passwords, database URLs) are removed. |
| `fails_when` | What a failure means, in plain words. Required. |
| `uses_stand_ins` | True when a check runs against fakes or test doubles; it then needs `proven_with`. |
| `proven_with` | Required on the objective check, and on any check with `uses_stand_ins`: what it is measured against, in plain words ("the real checkout page in staging", "a fake card processor in tests"). Shown to the owner. |
| `needs` | Check ids that must pass first. A check whose needs fail is `blocked`, not run. |
| `frozen` | Files that define the check. The approval pins their contents. |
| `appetite_hours` | The time the owner bets on this check. After that many hours since its first dispatch, the dispatch hook refuses it until the owner cuts, reshapes or extends it. |
| `plan_text` | The existing plan's own words for this step. It goes into the builder's brief, so the plan is handed over rather than looked up. |
| `dropped` | Plan steps the owner dropped, with their words. Shown on the approval sheet. |
| `open_for_the_owner` | Questions only the owner can answer. Shown on the approval sheet. |
| `independence` | Tree-level. Who wrote the defining tests, who drafted the checks and who evaluated them, in plain words. Shown at the top of the owner's sheet ("not stated" if missing). |
| `commits_product_to` | Tree-level. A plain-words list of what the tests decide about the product. Shown on the owner's sheet. |
| `may_change` | Per check. Extra paths, beyond the program's `areas`, that a builder on this check may change. |
| `validation` | Path of the plan-validation file. Approval requires it (or `--without-validation`, which the scoreboard shows). |
| `approved` | Written by `check-tree approve`: who, their words, when, the tree's hash and each frozen file's hash. |

`check-tree validate` refuses a tree with no objective, more than one, duplicate ids, unknown or looping needs,
a missing frozen file, a non-positive appetite, a missing `fails_when`, or a command that cannot fail.

## Which copy binds

Hooks and the runner read the tree from `<remote>/<base_ref>` (after a fetch), not from the checkout a session
happens to sit in. A session opened in a checkout a day behind still reads today's approved tree.

## Commands

| Command | What it does |
|---|---|
| `init <program>` | Writes a starter tree, `.check-tree/config.json`, and ignores the local state folders. |
| `validate <program>` | Every reason the working copy's tree cannot be approved or run. |
| `table <program> [--owner]` | The tree with ids and the hash; `--owner` prints the plain-words approval sheet. |
| `red-at <commit> [--expect RE] -- <command>` | Passes if the command FAILS at that commit and not merely because it could not run (a missing file, import, syntax or usage error). `--expect` pins the reason: the failure output must contain it. The harness check for a defining test: it stays green after the work is done. |
| `approve <program> --by --words` | Records the approval in the working copy. It binds once committed to the base branch. |
| `run <program> [--check ID]` | Runs checks in dependency order, in a fresh detached worktree at the head of `counts_on`, and records each result with the commit it ran at. A builder's own branch is scored by the stop hook in its worktree; it reaches the scoreboard once landed and merged. Nothing counts while the tree is not approved. |
| `score <program>` | The scoreboard: results recorded for the current head only; anything else is `not run`. Shows ready checks, over-appetite checks and `STALLED`. |
| `start <program>` | The lead's opening page, in place of a handoff: the job, the score, ready checks with the plan's words, what only the owner can do, branches waiting to land. |
| `brief <program> <check>` | The brief a builder gets for one check. |
| `land <program> <check>` | Merges the latest passing builder branch for that check into the integration branch (fast-forward after bringing the builder up to date). |
| `card <program> [--days 7]` | A short summary to share: passing count now and N days ago, whether the goal is reached, what newly passes. No internals. |
| `clean <program> [--hours N]` | Removes builder worktrees that were landed, or that never got a commit and are older than N hours (default 24). A worktree with uncommitted or unlanded work is always kept. |
| `progress <program> "<line>"` | Adds a line to the program's progress log. |
| `list` | Every program and its pass count. |

Local state (results, dispatches, progress, history) lives in `.check-tree/state/` and builders' worktrees in
`.check-tree/worktrees/`. Both are ignored by git.

## The hooks, in order of a builder's life

1. **Dispatch** (`check_brief.py`, PreToolUse on Agent/Task). The prompt names `Check: <program>/<id>`. The hook
   fetches, reads the binding tree, and refuses if it is invalid, unapproved, the check is unknown, already
   passing, not ready, or over its appetite. It also asks the cost and cap guards first (hooks run in
   parallel), so a dispatch they refuse never leaves a worktree behind. Otherwise it creates the integration branch if needed, makes the
   builder a worktree on a fresh branch from it, and appends the brief (with a `Worktree:` line) to the prompt.
2. **Editing** (`check_scope.py`, PreToolUse on Edit/Write). A file in an approved program's `areas` may be
   edited only inside a worktree belonging to that program.
3. **Stopping** (`check_stop.py`, SubagentStop). The hook finds the `Check:` and `Worktree:` lines in the
   builder's first message and checks, in order: uncommitted work, changed frozen files, then the check itself.
   Each problem sends the builder back with the reason as its next instruction. After `max_send_backs` failing
   runs it may return, logged as failed. A pass is logged with its commit, ready for `land`.

## Recording owner approval honestly

`approve` stores the owner's own words. Use their words, not a summary. If an agent ran `approve`, the commit
that lands the tree should quote the message where the owner said yes. This is the weakest link in the design,
and it is stated as such in the README.
