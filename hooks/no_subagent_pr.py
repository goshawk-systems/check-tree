#!/usr/bin/env python3
"""PreToolUse on Bash, inside a subagent: subagents do not open pull requests.

A builder pushes its branch and returns; the lead opens one pull request per milestone. Why: a lead's agents
once opened five pull requests for one objective before anything was reviewed.
"""
from __future__ import annotations

import _common as C
import _shell as S


def main(data: dict) -> None:
    if data.get("tool_name") != "Bash" or not C.in_subagent(data):
        return
    # Read as commands, not text: catches `cd x && gh pr create`, env prefixes, `bash -c`, and a `gh api .../pulls`
    # POST with fields and no -X (gh then POSTs). pr_open_rules.py reads the same parsed call.
    if S.pr_open_calls((data.get("tool_input") or {}).get("command", ""), data.get("cwd") or ""):
        C.deny("check-tree: subagents do not open pull requests. Push your branch and return; the lead opens "
               "one pull request for the milestone.")


if __name__ == "__main__":
    C.run(main)
