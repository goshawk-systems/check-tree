# check-tree

**Plans as pass/fail checks for teams of AI coding agents.** A plan is a tree of checks the owner approves once.
An agent is handed one check and cannot finish while it fails. Done is computed by running the checks, never
declared. Plus a handful of guardrail hooks that held up under months of real use.

It is a [Claude Code](https://code.claude.com) plugin: Python hooks, two agent definitions and a small command line.
Standard library only.

> Built while developing a healthcare product with teams of Claude Code agents. The story of what failed before
> this, and why: [Managing AI Developers Turns Out to Be A Lot Like Managing Human Developers](https://sarahindev.substack.com/p/managing-ai-developers-turns-out).

## Why

Every failure we had with agent teams had the same shape. We answered it with another rule, another reviewer or
another checklist, and every one of those checked an *input* to the work: did you read the docs, did you name the
ticket, did a reviewer pass it. Agents learned to satisfy the checklist. In one audited week, 10% of the lines
agents changed were product code, and 9 of 12 complaints had been answered with a new guardrail that was never
removed.

check-tree checks the *output*. The plan is a set of commands that pass or fail. An agent's job is to turn one of
them green, and the hooks make that the only way to finish.

## How it works

1. **The plan is a check tree** (`.check-tree/programs/<program>/checks.json`): one `objective` check that
   measures the outcome where it is used, with `component` and `harness` checks under it. Each check is a shell
   command, the tests that define it (`frozen`), what a failure means (`fails_when`), what it waits on
   (`needs`), and a time budget (`appetite_hours`).
2. **The owner approves it once**, in their own words. The approval pins the tree's hash and every frozen file.
   Editing either unapproves it, so an agent cannot quietly weaken a test or extend its own budget.
3. **Done is computed.** `check-tree run` runs the checks in a fresh worktree at the head of the base branch.
   The scoreboard is the runner's output. A result from an older commit counts as not run. A program whose
   pass count has not moved in three days shows `STALLED`.
4. **A builder gets one check.** A dispatch whose prompt says `Check: <program>/<id>` is refused unless the tree
   is approved and the check is ready. Otherwise the hook creates the builder's own git worktree and appends a
   short brief: the objective, its one check, the frozen files and the rules. That replaces the long reading list.
5. **A builder cannot finish while its check fails.** When it stops, its check runs in its worktree. It is sent
   back for a failing check, uncommitted work or a changed frozen test, up to two times, and then its return is
   logged as failed.
6. **The files decide who may change them.** A file matching an approved program's `areas` can be edited only
   inside a builder's worktree for that program. The lead steers; it does not code.
7. **A planner drafts the tree and a skeptical evaluator attacks it**, before the owner sees it and again on
   every claimed pass.

## Guardrails included

| Hook | Refuses | Why it exists |
|---|---|---|
| `required_reads.py` | Code edits before the files in CLAUDE.md's `required-reads` block were actually opened | One agent type opened the core design doc in 0 of 208 runs while being told to read it |
| `agent_cap.py` | A dispatch beyond `agent_cap` live agents | About 13 agents each watching CI exhausted a shared GitHub API limit (5,000 calls an hour) three times in a week |
| `no_ci_polling.py` | A subagent running `gh run watch`, `--watch`, or a gh-and-sleep loop | Same incident: agents push and return; the pipeline runs the checks |
| `model_gate.py` | The expensive model without a `MODEL-APPROVED:` line, and subagents starting subagents | "Use the cheaper model" in every prompt did not stop dozens of expensive dispatches |
| `no_subagent_pr.py` | A subagent opening a pull request | A lead's agents opened five pull requests for one objective before anything was reviewed |

Each hook allows the action if it crashes, and says why on stderr: a guard bug must never stop all work.

## Install

```bash
claude plugin marketplace add goshawk-systems/check-tree
claude plugin install check-tree@check-tree
```

Or inside Claude Code: `/plugin marketplace add goshawk-systems/check-tree`, then `/plugin install check-tree@check-tree`.

The command line has no dependencies. Run it from the plugin folder, or clone the repo and put `bin/` on your
`PATH`:

```bash
git clone https://github.com/goshawk-systems/check-tree && export PATH="$PWD/check-tree/bin:$PATH"
```

## Quickstart

```bash
check-tree init payments            # a starter tree and .check-tree/config.json
$EDITOR .check-tree/programs/payments/checks.json
check-tree validate payments        # every reason it cannot be approved yet
check-tree table payments           # what the owner reads before approving
check-tree approve payments --by owner --words "yes, this is what done means"
git add .check-tree && git commit -m "payments: approved check tree"
check-tree run payments             # score it at the head of main
check-tree start payments           # the lead's opening page: ready checks, what is waiting to land
```

Then dispatch builders from Claude Code with `Check: payments/<id>` in the prompt. When a builder returns
passing, `check-tree land payments <id>` merges its branch into the program's integration branch
(`check-tree/payments`). Open one pull request from there when the milestone is proven.
`check-tree clean payments` removes builder worktrees that hold no unlanded work.

`examples/quickstart.sh` builds a throwaway repo and walks through all of it in under a minute. It needs only git and Python 3.9 or newer.

## Configuration

`.check-tree/config.json`. All keys are optional; defaults are in `templates/config.json`.

| Key | Default | Meaning |
|---|---|---|
| `base_ref` | `main` | The branch whose copy of a tree binds, and where passes count |
| `remote` | `origin` | Fetched before reading the binding tree; `null` for a local-only repo |
| `max_send_backs` | `2` | Failing stops before a builder may return as failed |
| `check_timeout_seconds` | `1800` | Per check command |
| `stalled_after_days` | `3` | When an unmoving pass count shows `STALLED` |
| `scope` | `enforce` | `warn` or `off` to loosen the file-ownership rule |
| `guards.*` | see template | Agent cap, expensive models, approval marker, required-reads file |

`templates/CLAUDE.md` is a starting instructions file in which every rule names what enforces it, and
`templates/test_claude_md_size.py` keeps it under 200 lines and shrink-only.

## What it does not prevent

Stated, not hidden:
- **Approval is recorded on the owner's words**, by whoever runs `approve`. The tree shows the words and the
  commit that landed them; nothing proves the owner said them.
- **A file a check reads but does not list as `frozen` can be edited.** The evaluator attacks claimed passes for
  this; it is not a wall.
- **Writes through Bash are not covered** by the scope rule or the required-reads gate; they watch the edit tools.
- **Whether a tree's checks are the right ones is judgment.** That is the planner's, the evaluator's and the owner's job.
- **It does not make agents understand your system.** Every step an agent takes still re-reads its context; the
  loop narrows what it needs, not what it costs.

## Also in this repo

- `docs/how-it-works.md`: the tree format and every command, in detail.
- `docs/pipeline-patterns.md`: the CI changes that made "done is a passing check" trustworthy for us (the
  pipeline owns the tests, affected tests before batching, the full suite once in the merge queue, scripted
  review instead of AI review, shrink-only rule ratchets).
- `pipeline/`: ready-to-use scripts for three of those patterns, standard library only:
  `impacted_tests.py` (which tests a change reaches, in parallel shards), `ratchet.py` (rules whose violation
  counts can only go down) and `scripted_review.py` (the review a script can decide), plus
  `example-workflow.yml` wiring them into GitHub Actions with the full suite once in the merge queue.
- `tests/`: every hook driven the way Claude Code drives it, shown refusing and allowing (run with pytest).

## Status

Version 0.1. The design has run in a real multi-agent build since October 2026; this is a from-scratch, general
version of it, not a copy. Early numbers from that build, honestly: product code per token used went up about
threefold over two weeks, two of four programs moved steadily on their checks, and no program's objective check
passes yet. Issues and pull requests welcome.

MIT licensed.
