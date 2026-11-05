#!/usr/bin/env python3
"""PreToolUse on Edit/Write/MultiEdit/NotebookEdit and Bash: do not write into another session's checkout.

A write is refused when its target is inside a git checkout that is not this session's own project root AND is
either listed in `shared_checkouts` (`.check-tree/repo.json`; default none) or holds a `.check-tree/session.lock`
naming a different session id (plain text, or JSON with `session_id`). Edit through your own clone or worktree.

Bash writes it reads: `>`/`>>`/`&>` targets, `mv`, `rm`, `cp` (the destination), `touch`, `mkdir`, `tee`,
`sed -i`, `ln`, `install`, and `git -C <path>` or a git command run after `cd <path>` with a subcommand that
changes the checkout (commit, add, checkout, reset, merge, rebase, stash, clean, ...).

It cannot see: writes made by a script or interpreter (`python x.py`, `make`, `npm`), paths built from variables
other than $HOME, `ssh`, or a lock file that is stale. A session with no `session_id` on the event never matches
a lock. It is a fence against the common slip, not a sandbox.
"""
from __future__ import annotations

import json
import os
import pathlib

import _common as C
import _shell as S

WRITE_ALL_ARGS = {"mv", "rm", "touch", "mkdir", "rmdir", "tee", "ln", "unlink", "truncate", "chmod"}
GIT_READ_ONLY = {"status", "log", "diff", "show", "rev-parse", "ls-files", "ls-tree", "cat-file", "blame", "grep",
                 "describe", "for-each-ref", "rev-list", "shortlog", "show-ref", "merge-base", "name-rev",
                 "ls-remote", "fetch", "remote", "config", "branch", "worktree", "tag", "reflog", "help", "version"}


def _toplevel(path: str) -> "pathlib.Path | None":
    p = pathlib.Path(os.path.realpath(path))
    while not p.exists() and p != p.parent:
        p = p.parent
    code, top = C.core.git(p if p.is_dir() else p.parent, "rev-parse", "--show-toplevel")
    return pathlib.Path(os.path.realpath(top)) if code == 0 and top else None


def _lock_owner(top: pathlib.Path) -> str:
    try:
        text = (top / ".check-tree" / "session.lock").read_text().strip()
    except OSError:
        return ""
    try:
        obj = json.loads(text)
        return str(obj.get("session_id", "")) if isinstance(obj, dict) else text
    except json.JSONDecodeError:
        return text.splitlines()[0].strip() if text else ""


def _bash_targets(command: str, cwd: str) -> list:
    out = []
    for c in S.simple_commands(command, cwd):
        out += [S.expand(t, c.cwd) for op, t in c.redirects if op in (">", ">>", ">|", "&>", "&>>")]
        args, v = [a for a in c.argv[1:] if not a.startswith("-")], c.verb
        if v in WRITE_ALL_ARGS:
            out += [S.expand(a, c.cwd) for a in args]
        elif v in ("cp", "install") and args:
            out.append(S.expand(args[-1], c.cwd))
        elif v == "sed" and any(a.startswith("-i") for a in c.argv[1:]):
            out += [S.expand(a, c.cwd) for a in args[1:]]
        elif v == "git":
            rest, where = c.argv[1:], c.cwd
            if len(rest) > 1 and rest[0] == "-C":
                where, rest = S.expand(rest[1], c.cwd), rest[2:]
            sub = next((a for a in rest if not a.startswith("-")), "")
            if sub and sub not in GIT_READ_ONLY:
                out.append(where)
    return out


def main(data: dict) -> None:
    tool, ti = data.get("tool_name"), data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()
    if tool in C.EDIT_TOOLS:
        targets = [S.expand(ti.get("file_path") or ti.get("notebook_path") or "", cwd)] if (
            ti.get("file_path") or ti.get("notebook_path")) else []
    elif tool == "Bash":
        targets = _bash_targets(ti.get("command", ""), cwd)
    else:
        return
    own = _toplevel(cwd)
    shared = {os.path.realpath(os.path.expanduser(s)) for s in C.repo_config(data)["shared_checkouts"]}
    me = str(data.get("session_id") or "")
    for t in targets:
        if t in ("/dev/null", "/dev/stderr", "/dev/stdout"):
            continue
        top = _toplevel(t)
        if top is None or top == own:
            continue
        owner = _lock_owner(top)
        if str(top) in shared or (owner and me and owner != me):
            why = "a shared checkout" if str(top) in shared else f"session {owner}'s checkout"
            C.deny(f"check-tree: {t} is inside {why} ({top}), not this session's own project root. Make your "
                   "change in your own clone or worktree.")
            return


if __name__ == "__main__":
    C.run(main)
