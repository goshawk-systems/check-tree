#!/usr/bin/env python3
"""A cap on how many subagents one session runs at once.

SubagentStart records an agent, SubagentStop removes it, and a dispatch (PreToolUse on Agent/Task) is refused
when the session already has `agent_cap` live. Entries older than `agent_max_age_hours` are dropped, because a
stop is not always delivered. Why: about 13 agents each watching their own CI runs exhausted a shared GitHub
API limit (5,000 calls an hour) three times in one week.
"""
from __future__ import annotations

import json
import time

import _common as C


def load(path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def live_agents(data: dict) -> dict:
    root = C.root_for(data)
    cfg = C.guard_config(root)
    max_age = float(cfg["agent_max_age_hours"]) * 3600
    now = time.time()
    return {k: v for k, v in load(C.state_file(root, "live-agents.json")).get(data.get("session_id", "?"), {}).items()
            if now - v < max_age}


def decide(data: dict) -> "str | None":
    """The refusal reason for a dispatch over the cap, or None. Also called by check_brief."""
    if data.get("tool_name") not in C.AGENT_TOOLS:
        return None
    cfg = C.guard_config(C.root_for(data))
    n = len(live_agents(data))
    if n >= int(cfg["agent_cap"]):
        return (f"check-tree: this session already runs {n} agents (cap {cfg['agent_cap']}). "
                "Wait for one to return, or raise `guards.agent_cap` in .check-tree/config.json.")
    return None


def main(data: dict) -> None:
    root = C.root_for(data)
    cfg = C.guard_config(root)
    path = C.state_file(root, "live-agents.json")
    live = load(path)
    session = data.get("session_id", "?")
    max_age = float(cfg["agent_max_age_hours"]) * 3600
    now = time.time()
    agents = {k: v for k, v in live.get(session, {}).items() if now - v < max_age}
    event = data.get("hook_event_name")
    if event == "SubagentStart" and data.get("agent_id"):
        agents[data["agent_id"]] = now
    elif event == "SubagentStop" and data.get("agent_id"):
        agents.pop(data["agent_id"], None)
    elif event == "PreToolUse" and data.get("tool_name") in C.AGENT_TOOLS:
        reason = decide(data)
        if reason:
            C.deny(reason)
    live[session] = agents
    path.write_text(json.dumps(live))


if __name__ == "__main__":
    C.run(main)
