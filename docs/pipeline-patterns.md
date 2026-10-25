# Pipeline patterns

"Done is a passing check" is only as trustworthy as the pipeline that runs the tests. These are the changes that
made ours trustworthy and cheaper, with the problem each one answered. Patterns 2, 5 and 6 come with
ready-to-use scripts in `pipeline/`, and `pipeline/example-workflow.yml` wires them, with pattern 3, into
GitHub Actions. Adapt them: every CI setup is different.

## 1. The pipeline owns the tests

**Rule:** no person or agent runs a test suite by hand "to check first". Opening the pull request is the whole
job; the pipeline runs every test.

**Why:** agents ran the full suite locally and on demand, and again in CI. One branch's full suite ran 8 to 10
times. Local heavy runs also crashed the developer's machine. The only local check left is a secret scan on push.

## 2. The tests a change reaches run on the pull request

**Rule:** before a change can join a batch, the tests it adds or edits, every test that imports a module it
changed, and every guard that names a changed file by path must pass on the pull request.

**Why:** once the full suite moved to the merge queue (pattern 3), a broken change was found only in a batch,
and it cost every other change in that batch a retry. A text-based selector (changed module names matched to
test imports) caught most of these. It misses transitive importers; the queue still catches those.

**Sketch:** list changed files with `git diff --name-only $(git merge-base origin/main HEAD)`, map each changed
module to tests that import it by name, add tests the change touched, and run that set in parallel shards. A
test already failing on the merge base is reported but does not block.

## 3. The full suite runs once, in the merge queue

**Rule:** the full suite runs once per change, on the merge queue's batch of several changes, never again on the
pull request or after the base branch moves.

**Why:** the full suite had to pass on the exact head of the PR, so every update from the base branch bought
another full run. With batching, about 3 to 8 changes share one run. A failed batch is retried once, then split
to find the culprit.

**Sketch:** GitHub's native merge queue (`on: merge_group`) or a queue service. Required status checks on the
pull request are the fast gates of pattern 2; the full suite is required only on the queue's batch.

## 4. Slow tests get another shard, never a longer timeout

**Rule:** when a test tier nears its time limit, split it into more parallel shards using a fresh map of test
durations. Never raise the limit.

**Why:** raised limits hide slow growth until a whole queue stalls. One database tier went from 13 to 16
minutes against a 20-minute limit to about 9 to 10 minutes per shard after resharding.

## 5. Scripted review instead of AI review

**Rule:** anything a reviewer checks that a script can decide (the PR description has its sections, the work
item is cited, every required gate is green, no test assertion was removed without a stated reason) is checked
by a script, as a required status. An AI review is optional and gates nothing.

**Why:** 263 AI reviews over two weeks cost about $1,250 and produced 20 real catches, most of which were about
the PR description's form, which a script now checks. The AI review was also the largest single line of CI
spend. Removing it took that line to zero. What a script cannot judge (whether the change is good) is the job of
the check tree's evaluator and of the owner.

## 6. Rules that can only get stricter

**Rule:** for each code rule (no raw SQL in route handlers, no direct environment reads, one logging setup), a
script counts violations and compares with a committed baseline. New violations fail. The baseline can only go
down. Seed it with today's count, so a new rule never blocks existing work, and reduce it as code is fixed.

**Why:** prose rules in an instructions file were broken constantly. A shrink-only count turned "please don't"
into a number that moves one way. One rule went from 291 violations to 0, and another from 115 to 0.

## 7. One entry point for pull request checks

**Rule:** a single workflow listens to pull request events and calls the others as reusable workflows, and the
list of blocking gates is derived from the workflow files rather than typed by hand.

**Why:** with many workflows each listening to every event, one hour produced 571 workflow runs, more than half
of them skipped. A hand-typed list of required gates also drifted from what actually ran.

## 8. Subagents never watch CI

**Rule:** an agent pushes and returns its commit; one person or lead reads CI results for everyone, in one pass.
This repo's `no_ci_polling.py` and `agent_cap.py` hooks enforce it.

**Why:** about 13 agents each running `gh run watch` exhausted a shared GitHub limit of 5,000 API calls an hour
three times in one week, and a missing status read as a failure took down merging.
