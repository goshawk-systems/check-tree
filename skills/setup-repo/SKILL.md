---
name: setup-repo
description: Set up a repository the check-tree way from the start - the CLAUDE.md, rulings file, architecture rules, test fence, pre-push hook, CI and merge queue - with one command, and list what only the owner can do by hand. Use when someone starts a new repo (or adopts check-tree in an existing one) and says "set this repo up", "set up Goshawk", or wants agents to work in it safely from day one.
---

# Set up a repository: `/check-tree:setup-repo`

One command copies the package's repo template into the repository and fills it from one settings file.

## 1. Explain, briefly
- `check-tree init-repo` writes the files a repo run by agents needs from the first commit: a short `CLAUDE.md`
  where every rule names what enforces it, `docs/RULINGS.md` (the owner's decisions, in the owner's words),
  `docs/ARCHITECTURE-RULES.md`, a test fence (`conftest.py`) that stops tests reaching real systems, a pre-push
  hook (secret scan, then lint on changed files), a CLAUDE.md size test, and the CI and merge-queue files.
- It asks nothing. It never overwrites a file that exists (it lists what it skipped), and running it twice
  changes nothing. `--force` overwrites; `.check-tree/repo.json` is never overwritten.

## 2. Run it
1. Confirm the repository (a git repository, ideally with one commit) and that it is the right directory.
2. Run `check-tree init-repo` (add `--target <dir>` to set up another directory).
3. The first run creates `.check-tree/repo.json` with defaults. Tell the person to open it and set, at least:
   `repo_name`, `default_branch`, `product_prefixes` (the work-item prefixes, like `ABC`), `test_command`,
   `lint_command`, and under `prod` the deploy workflow names, database names and cloud profiles or accounts that
   are production. Do not fill these in for them from guesses.
4. Files already written keep the old values. After they edit repo.json, delete the files that carry
   placeholders (or pass `--force`) and run `check-tree init-repo` again.

## 3. Hand over the checklist
`init-repo` ends by printing what only the owner can do. Read it to them and do not do these yourself:
- Install the Mergify GitHub app and commit `.mergify.yml`.
- In branch protection, require one status, `all-green`, and the merge queue.
- Apply the infrastructure module with their cloud credentials (a production change).
- Fill in `docs/ARCHITECTURE-RULES.md` and add the first row to `docs/RULINGS.md`.
- `git config core.hooksPath .githooks`.

## 4. Per-workstream clones
`check-tree workspace new <name>` makes a fresh clone of the repo's origin at `~/workbench/ws-<name>` (set another
place with `--root` or `CHECK_TREE_WORKSPACE_ROOT`), with the pre-push hook on. `check-tree workspace sweep`
lists clones whose work is merged and pushed and removes nothing; `--apply` removes them. A clone with
uncommitted work, a stash, or commits on no remote branch is never removed.
