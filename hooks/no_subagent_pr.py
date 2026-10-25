#!/usr/bin/env python3
"""PreToolUse on Bash, inside a subagent: subagents do not open pull requests.

A builder pushes its branch and returns; the lead opens one pull request per milestone. Why: a lead's agents
once opened five pull requests for one objective before anything was reviewed.
"""
from __future__ import annotations

import re

import _common as C

PR_CREATE = re.compile(r"\bgh\s+pr\s+create\b|\bgh\s+api\b[^\n]*\bpulls\b[^\n]*(-X\s*POST|--method\s+POST)")


def main(data: dict) -> None:
    if data.get("tool_name") != "Bash" or not C.in_subagent(data):
        return
    if PR_CREATE.search((data.get("tool_input") or {}).get("command", "")):
        C.deny("check-tree: subagents do not open pull requests. Push your branch and return; the lead opens "
               "one pull request for the milestone.")


if __name__ == "__main__":
    C.run(main)
