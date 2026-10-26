#!/usr/bin/env python3
"""PreToolUse on Edit/Write: no code change until the required reading has actually been opened.

The list is a fenced block in CLAUDE.md (configurable):

    ```required-reads
    docs/ARCHITECTURE.md
    docs/RULES.md
    ```

The agent's own transcript is searched for a Read of each file. Telling an agent to read something is not
enough; in our record one agent type opened the core design document in 0 of 208 runs before this check.
A builder working inside a check-tree worktree is exempt: it gets its check's brief instead of a reading list.
A listed file that does not exist in the repo does not block anything.
"""
from __future__ import annotations

import pathlib
import re

import _common as C

BLOCK = re.compile(r"```required-reads\s*\n(.*?)```", re.S)


def required(root: pathlib.Path, rel: str) -> list[str]:
    p = root / rel
    if not p.is_file():
        return []
    m = BLOCK.search(p.read_text(errors="replace"))
    if not m:
        return []
    return [ln.strip() for ln in m.group(1).splitlines() if ln.strip() and not ln.strip().startswith("#")]


def opened(transcript: str | None, root: pathlib.Path) -> set[str]:
    seen: set[str] = set()
    for use in C.tool_uses(transcript):
        if use["name"] != "Read":
            continue
        fp = use["input"].get("file_path", "")
        try:
            seen.add(str(pathlib.Path(fp).resolve().relative_to(root.resolve())))
        except ValueError:
            seen.add(fp)
    return seen


def main(data: dict) -> None:
    if data.get("tool_name") not in C.EDIT_TOOLS:
        return
    root = C.root_for(data)
    if root is None:
        return
    target = (data.get("tool_input") or {}).get("file_path", "")
    if target and C.worktree_program(root, target):
        return
    cfg = C.guard_config(root)
    need = [f for f in required(root, cfg["required_reads_file"]) if (root / f).is_file()]
    if not need:
        return
    have = opened(C.agent_transcript(data), root)
    missing = [f for f in need if f not in have and not any(h.endswith("/" + f) for h in have)]
    if missing:
        C.deny("check-tree: open the required reading before changing code (listed in "
               f"{cfg['required_reads_file']}). Not yet opened: " + ", ".join(missing))


if __name__ == "__main__":
    C.run(main)
