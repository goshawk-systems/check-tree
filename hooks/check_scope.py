#!/usr/bin/env python3
"""PreToolUse on Edit/Write: a program's files change only inside a builder's worktree for that program.

A file matching an approved program's `areas` may not be edited in the main checkout, by the lead or anyone
else; the lead steers by dispatching builders on ready checks. Files no approved program owns are untouched.
Set `"scope": "warn"` or `"off"` in .check-tree/config.json to loosen it.
"""
from __future__ import annotations

import json
import pathlib

import _common as C
from checktree import core


def main(data: dict) -> None:
    if data.get("tool_name") not in C.EDIT_TOOLS:
        return
    ti = data.get("tool_input") or {}
    target = ti.get("file_path") or ti.get("notebook_path")
    if not target:
        return
    root = C.root_for(data)
    if root is None:
        return
    cfg = core.load_config(root)
    if cfg.get("scope") == "off":
        return
    ref = core.binding_ref(root, cfg)
    target_path = pathlib.Path(target).resolve()
    wt_program = C.worktree_program(root, str(target_path))
    if wt_program:
        # inside a builder worktree: relative to that worktree's own root
        wt_root = root / core.WORKTREES_DIR
        rel_parts = target_path.relative_to(wt_root.resolve()).parts[1:]
        rel = str(pathlib.Path(*rel_parts)) if rel_parts else ""
    else:
        try:
            rel = str(target_path.relative_to(root.resolve()))
        except ValueError:
            return
    trees = []
    for name in core.list_programs(root, ref):
        t = core.load_tree(root, name, ref)
        if t and core.approval_state(t, root, ref)[0]:
            trees.append(t)
    owner = core.owner_of(root, rel, trees)
    if owner is None or owner.program == wt_program:
        return
    msg = (f"check-tree: {rel} belongs to the approved program {owner.program!r}. Its files change only through "
           f"a builder on one of its checks: dispatch an agent whose prompt says `Check: {owner.program}/<id>` "
           f"(ready checks: `check-tree score {owner.program}`).")
    if cfg.get("scope") == "warn":
        print(json.dumps({"systemMessage": msg}))
        return
    C.deny(msg)


if __name__ == "__main__":
    C.run(main)
