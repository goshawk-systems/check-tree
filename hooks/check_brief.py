#!/usr/bin/env python3
"""PreToolUse on Agent/Task: a dispatch that names `Check: <program>/<id>` gets its brief and its own worktree.

Refused when the program's tree (the binding copy on the base branch) is missing, invalid or not approved by
the owner, when the check does not exist, already passes, still waits on a check it needs, or has used up its
time budget (the owner cuts, reshapes or extends it). A dispatch with no `Check:` line is left alone.
"""
from __future__ import annotations

import _common as C
from checktree import core


def main(data: dict) -> None:
    if data.get("tool_name") not in C.AGENT_TOOLS:
        return
    tool_input = dict(data.get("tool_input") or {})
    binding = core.parse_binding(tool_input.get("prompt", ""))
    if not binding:
        return
    program, check_id = binding
    root = C.root_for(data)
    if root is None:
        C.deny("check-tree: this session is not inside a git repository, so `Check:` cannot be bound.")
        return
    cfg = core.load_config(root)
    warn = core.fetch(root, cfg)
    ref = core.binding_ref(root, cfg)
    tree = core.load_tree(root, program, ref)
    note = f" ({warn})" if warn else ""
    if tree is None:
        C.deny(f"check-tree: no tree for program {program!r} at {ref}{note}. Commit "
               f"{core.tree_path(program)} to {cfg['base_ref']} and have the owner approve it.")
        return
    problems = core.validate(tree, root, ref)
    if problems:
        C.deny(f"check-tree: the {program} tree at {ref} is not valid:\n  " + "\n  ".join(problems))
        return
    ok, why = core.approval_state(tree, root, ref)
    if not ok:
        C.deny(f"check-tree: the {program} tree is not approved ({'; '.join(why)}). Show the owner "
               f"`check-tree table {program}`; on their yes, `check-tree approve {program} --by owner "
               f"--words \"<their words>\"` and commit it.")
        return
    check = tree.check(check_id)
    if check is None:
        C.deny(f"check-tree: {program} has no check {check_id!r}. Ready: "
               + ", ".join(core.score(root, program, do_fetch=False).ready))
        return
    s = core.score(root, program, do_fetch=False)
    if s.status.get(check_id) == "pass":
        C.deny(f"check-tree: {program}/{check_id} already passes at {s.ref}. Ready checks: {', '.join(s.ready)}")
        return
    if check_id not in s.ready:
        waiting = [n for n in check.get("needs", []) if s.status.get(n) != "pass"]
        C.deny(f"check-tree: {program}/{check_id} is not ready; it needs {', '.join(waiting)} to pass first. "
               f"Ready: {', '.join(s.ready)}. (Run `check-tree run {program}` if the scoreboard is stale.)")
        return
    if check_id in s.over_appetite:
        C.deny(f"check-tree: {program}/{check_id} has used its {check.get('appetite_hours')}h budget. The owner "
               f"decides: cut it, reshape it, or extend its appetite_hours in the tree (a tree change needs "
               f"their approval again).")
        return
    worktree, branch = core.make_worktree(root, tree, check_id, cfg)
    core.append_jsonl(core.state_dir(root, program) / "dispatches.jsonl",
                      {"at": core.now(), "check": check_id, "worktree": str(worktree), "branch": branch,
                       "session": data.get("session_id")})
    core.progress(root, program, f"dispatched a builder on {check_id}", check=check_id, branch=branch)
    tool_input["prompt"] = tool_input.get("prompt", "") + "\n" + core.brief(
        tree, check, s, worktree, branch, tree.integration_branch)
    C.allow_with_input(tool_input, f"check-tree: bound to {program}/{check_id}; worktree {worktree}")


if __name__ == "__main__":
    C.run(main)
