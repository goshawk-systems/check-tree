#!/usr/bin/env python3
"""PreToolUse on Bash, inside a subagent: no watching or polling CI.

A subagent pushes and returns its commit; the pipeline runs the checks and the lead reads results once, for
everyone. Refused: `gh run watch`, `gh pr checks --watch`, any `--watch` on gh, `watch ... gh`, and shell loops
that call gh and sleep. The main session is not affected.
"""
from __future__ import annotations

import re

import _common as C

PATTERNS = [
    (re.compile(r"\bgh\s+run\s+watch\b"), "`gh run watch`"),
    (re.compile(r"\bgh\b[^\n|;&]*\s--watch\b"), "a gh command with --watch"),
    (re.compile(r"\bwatch\b[^\n|;&]*\bgh\b"), "`watch` around gh"),
]
LOOP = re.compile(r"\b(while|until|for)\b.*\bdo\b(.*)\bdone\b", re.S)


def main(data: dict) -> None:
    if data.get("tool_name") != "Bash" or not C.in_subagent(data):
        return
    cmd = (data.get("tool_input") or {}).get("command", "")
    for rx, what in PATTERNS:
        if rx.search(cmd):
            C.deny(f"check-tree: subagents do not watch CI ({what}). Push, return your commit, and let the "
                   "pipeline run the checks; the lead reads every result in one pass.")
            return
    m = LOOP.search(cmd)
    if m and re.search(r"\bgh\b", m.group(2)) and re.search(r"\bsleep\b", m.group(2)):
        C.deny("check-tree: subagents do not poll GitHub in a loop. Push, return your commit, and let the "
               "pipeline run the checks.")


if __name__ == "__main__":
    C.run(main)
