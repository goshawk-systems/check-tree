#!/usr/bin/env python3
"""PreToolUse on Agent/Task: the expensive model needs an approval line, and subagents do not dispatch.

A dispatch that asks for a model in `expensive_models` (default: opus) is refused unless its prompt carries a
line starting with `model_approval_marker` (default `MODEL-APPROVED:`) saying who approved it and why. A
dispatch made from inside a subagent is refused unless `subagents_may_dispatch` is true. Why: writing
"use the cheaper model" in every prompt did not stop dozens of expensive dispatches; a check did.
"""
from __future__ import annotations

import _common as C


def main(data: dict) -> None:
    if data.get("tool_name") not in C.AGENT_TOOLS:
        return
    root = C.root_for(data)
    cfg = C.guard_config(root)
    if C.in_subagent(data) and not cfg["subagents_may_dispatch"]:
        C.deny("check-tree: a subagent does not start other agents. Return to the lead with what you need.")
        return
    ti = data.get("tool_input") or {}
    model = str(ti.get("model") or "").lower()
    if not model or not any(m.lower() in model for m in cfg["expensive_models"]):
        return
    marker = cfg["model_approval_marker"]
    if not any(line.strip().startswith(marker) for line in str(ti.get("prompt", "")).splitlines()):
        C.deny(f"check-tree: model {model!r} is on the expensive list. Use the default model, or add a line "
               f"`{marker} <who> <date> <reason>` to the prompt.")


if __name__ == "__main__":
    C.run(main)
