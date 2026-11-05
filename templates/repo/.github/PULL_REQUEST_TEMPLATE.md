<!--
START EVERY PULL REQUEST FROM THIS FILE. `gh pr create --body-file` never shows GitHub's template, so an agent or a script
must copy it in itself. FOUR things are required and a script checks them (ci/check_pr_body.py); everything else is optional,
delete it if it does not help. HTML comments like this one never count.
Never put a secret, a patient value or a raw production log line here.
-->

Closes-item: <ID>
<!-- REQUIRED. Or `Refs-item: <ID>` when this only advances the item. The id matches `work_item_pattern` in .check-tree/repo.json. -->

## What changed (required)

<!-- REQUIRED: EVERY changed file (`git diff --name-only origin/main...`), by its full path, one row each, with why.
A file the description never names fails the check. -->

| file | why |
|---|---|
| | |

## Test edits (required when you edit an existing test file)

<!-- REQUIRED for each EXISTING test file you edit, one line each, exactly this shape (a new test file needs none):
Test edits: path/to/test_file.py — why the edit keeps the test's protection intact
Delete this section if you edit no existing test. -->

## Consumer proof (required)

<!-- REQUIRED, not empty: something your own tests do not own. The served API document's hash, the rendered screen, the job's
output, a query count. Say what you reproduced and how. -->

## What was wrong (optional)

<!-- The defect or gap in plain words, with the evidence you saw it with. -->

## How it was proven (optional)

<!-- The test that fails before and passes after, and the command. -->

## Cost (optional)

<!-- Test wall time before and after for anything that adds tests or CI steps. -->

## Notes (optional)
