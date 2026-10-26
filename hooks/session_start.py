#!/usr/bin/env python3
"""SessionStart: tell Claude where check-tree's command line is, and which programs this repo has.

The plugin installs into a versioned folder deep inside Claude Code's config, so Claude cannot guess the path.
"""
from __future__ import annotations

import json
import os
import pathlib

import _common as C
from checktree import core


def main(data: dict) -> None:
    plugin = pathlib.Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or C.HERE.parent)
    cli = plugin / "bin" / "check-tree"
    lines = [f"check-tree is installed. Its command line is: {cli}",
             f"Its method, checklists and templates are in {plugin} (docs/method.md, templates/)."]
    root = C.root_for(data)
    if root is not None:
        cfg = core.load_config(root)
        programs = core.list_programs(root, core.binding_ref(root, cfg))
        if programs:
            lines.append("Programs in this repo: " + ", ".join(programs)
                         + f". Run `{cli} start <program>` before working on one.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": "\n".join(lines)}}))


if __name__ == "__main__":
    C.run(main)
