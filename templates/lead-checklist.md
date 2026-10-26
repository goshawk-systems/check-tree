# Lead checklist (every build session)

## Start
- [ ] `check-tree start <program>`: read the job, score, ready checks, owner-only items, work waiting to land.
- [ ] If the tree is not approved: stop and bring the owner the table. Nothing is dispatched on an unapproved tree.

## Each round
- [ ] One builder per ready check: prompt carries `Check: <program>/<id>`. No builder without a check.
- [ ] `check-evaluator` on every claimed pass before landing it.
- [ ] `check-tree land <program> <id>` for each verified pass; `check-tree run <program>` to rescore.
- [ ] Every `Check wrong:` / `Noticed:` line and every surprise written up as a proposed tree change for the owner.
- [ ] Any check over its time budget: brought to the owner to cut, reshape or extend. Never quietly extended.

## Never
- [ ] You did not edit the program's files yourself.
- [ ] You did not write a handoff. The scoreboard and progress log are the state.
- [ ] You did not add a rule in prose to fix a failure. A fix is a hook, check or test, shown red then green.

## End of week
- [ ] Pass count compared with 7 days ago. Not moved: roll back for this program and write down why.
- [ ] `check-tree clean <program>` to remove finished worktrees.
