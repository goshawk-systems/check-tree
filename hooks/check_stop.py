#!/usr/bin/env python3
"""SubagentStop: a builder bound to a check cannot finish while its check fails.

When the builder stops, its approved check runs in its own worktree. It is sent back (the reason becomes its
next instruction) when work is uncommitted, when a frozen file changed, when a product check's builder
changed files outside the program's areas, or when the check fails. After
`max_send_backs` failing runs it may return, and the return is logged as failed. A pass is logged with the
commit, ready for the lead to land on the integration branch.
"""
from __future__ import annotations

import fnmatch
import json
import pathlib

import _common as C
from checktree import core


def main(data: dict) -> None:
    transcript = C.agent_transcript(data)
    prompt = C.first_user_text(transcript)
    binding = core.parse_binding(prompt)
    wt = core.WORKTREE_LINE.search(prompt or "")
    if not binding or not wt:
        return
    program, check_id = binding
    worktree = pathlib.Path(wt.group(1))
    root = core.repo_root(worktree if worktree.is_dir() else None)
    cfg = core.load_config(root)
    ref = core.binding_ref(root, cfg)
    tree = core.load_tree(root, program, ref)
    check = tree.check(check_id) if tree else None
    if check is None or not worktree.is_dir():
        return

    attempts_path = C.state_file(root, f"{program}/stop-{data.get('agent_id', 'unknown')}.json")
    state = json.loads(attempts_path.read_text()) if attempts_path.is_file() else {"fails": 0, "asked": []}

    def save() -> None:
        attempts_path.write_text(json.dumps(state))

    def send_back(reason: str, kind: str) -> None:
        state["asked"].append(kind)
        save()
        core.progress(root, program, f"{check_id}: sent back ({kind})", check=check_id)
        C.block_stop(f"check-tree: {reason}")

    code, dirty = core.git(worktree, "status", "--porcelain")
    # Caches that test runners leave behind are not work.
    dirty = "\n".join(line for line in dirty.splitlines()
                      if not any(c in line for c in ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")))
    if code == 0 and dirty.strip():
        if state["asked"].count("uncommitted") < 2:
            send_back("you have uncommitted work in your worktree. Commit everything, then stop again:\n"
                      + core.tail(dirty, 15), "uncommitted")
            return

    integ = tree.integration_branch
    changed = []
    for f in check.get("frozen", []):
        if core.file_hash_at(worktree, "HEAD", f) != core.file_hash_at(root, integ, f):
            changed.append(f)
    if changed:
        if state["asked"].count("frozen") < 2:
            send_back(f"you changed frozen file(s) {', '.join(changed)}. They define the check and only the "
                      f"owner changes them. Restore them (git checkout {integ} -- <file>) and make the product "
                      f"pass the check instead. If the check is wrong, say so on a `Check wrong:` line.", "frozen")
            return
        core.progress(root, program, f"{check_id}: returned with frozen files changed", check=check_id)
        return

    if check.get("kind") != "harness":
        allowed = list(tree.areas) + list(check.get("may_change", []))
        _, names = core.git(worktree, "diff", "--name-only", f"{integ}...HEAD")
        outside = [f for f in names.splitlines() if f and not any(
            fnmatch.fnmatch(f, g) or fnmatch.fnmatch(f, g.rstrip("/") + "/*") for g in allowed)]
        if outside:
            if state["asked"].count("outside") < 2:
                send_back(f"you changed files outside this program's files ({', '.join(outside[:8])}). A build "
                          f"changes only the product ({', '.join(tree.areas)}); tests, fixtures and helpers that "
                          f"change what the tests run against are the owner's. Undo those changes. If the check "
                          f"needs one, say so on a `Check wrong:` line.", "outside")
                return
            core.progress(root, program, f"{check_id}: returned with changes outside the program's files",
                          check=check_id)
            return
    env = core.check_env(root, program, worktree)
    ok, output, secs = core.run_command(check["run"], worktree, env, int(cfg["check_timeout_seconds"]))
    _, sha = core.git(worktree, "rev-parse", "HEAD")
    core.append_jsonl(core.state_dir(root, program) / "builder-runs.jsonl",
                      {"at": core.now(), "check": check_id, "commit": sha, "status": "pass" if ok else "fail",
                       "seconds": round(secs, 1), "worktree": str(worktree)})
    if ok:
        core.progress(root, program, f"{check_id}: PASSES in the builder's worktree at {sha[:10]}; ready to land",
                      check=check_id, commit=sha, worktree=str(worktree))
        return
    state["fails"] += 1
    save()
    if state["fails"] <= int(cfg["max_send_backs"]):
        send_back(f"your check {program}/{check_id} still fails (attempt {state['fails']} of "
                  f"{cfg['max_send_backs']}). Fix the product, commit, and stop again. Output:\n"
                  + core.tail(output, 30), "fail")
        return
    core.progress(root, program, f"{check_id}: returned FAILING after {state['fails']} runs", check=check_id,
                  commit=sha)


if __name__ == "__main__":
    C.run(main)
