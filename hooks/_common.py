"""Shared helpers for check-tree's Claude Code hooks.

Every hook reads one JSON object on stdin and either prints nothing (allow, no opinion) or prints one JSON
decision. A hook that crashes allows the action and says why on stderr: a guard bug must never stop all work.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import traceback
from typing import Callable, Optional

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from checktree import core  # noqa: E402

AGENT_TOOLS = ("Agent", "Task")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def read_input() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}


def in_subagent(data: dict) -> bool:
    """A tool call made by a subagent carries agent_id (Claude Code docs: hooks, common input fields)."""
    return bool(data.get("agent_id"))


def deny(reason: str, event: str = "PreToolUse") -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))


def allow_with_input(updated: dict, reason: str = "") -> None:
    out = {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": updated}
    if reason:
        out["permissionDecisionReason"] = reason
    print(json.dumps({"hookSpecificOutput": out}))


def block_stop(reason: str) -> None:
    """SubagentStop: keep the subagent working; the reason is its next instruction."""
    print(json.dumps({"decision": "block", "reason": reason}))


def root_for(data: dict) -> Optional[pathlib.Path]:
    cwd = pathlib.Path(data.get("cwd") or os.getcwd())
    try:
        return core.repo_root(cwd)
    except SystemExit:
        return None


def guard_config(root: Optional[pathlib.Path]) -> dict:
    """The `guards` section of .check-tree/config.json, with defaults."""
    defaults = {
        "agent_cap": 8,
        "agent_max_age_hours": 6,
        "expensive_models": ["opus"],
        "model_approval_marker": "MODEL-APPROVED:",
        "subagents_may_dispatch": False,
        "required_reads_file": "CLAUDE.md",
    }
    if root is not None:
        p = root / core.CONFIG_PATH
        if p.is_file():
            try:
                defaults.update(json.loads(p.read_text()).get("guards", {}))
            except json.JSONDecodeError:
                pass
    return defaults


def state_file(root: Optional[pathlib.Path], name: str) -> pathlib.Path:
    base = (root / core.STATE_DIR) if root else pathlib.Path(os.environ.get("CLAUDE_PLUGIN_DATA", "/tmp"))
    base.mkdir(parents=True, exist_ok=True)
    return base / name


def transcript_rows(path: Optional[str]) -> list[dict]:
    if not path:
        return []
    p = pathlib.Path(path)
    if not p.is_file():
        return []
    rows = []
    with p.open() as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def agent_transcript(data: dict) -> Optional[str]:
    """The calling agent's own transcript: a subagent's file is <session dir>/subagents/agent-<id>.jsonl."""
    if data.get("agent_transcript_path"):
        return data["agent_transcript_path"]
    tp, aid = data.get("transcript_path"), data.get("agent_id")
    if tp and aid:
        cand = pathlib.Path(tp).with_suffix("") / "subagents" / f"agent-{aid}.jsonl"
        if cand.is_file():
            return str(cand)
    return tp


def first_user_text(path: Optional[str]) -> str:
    for r in transcript_rows(path):
        if r.get("type") != "user":
            continue
        c = (r.get("message") or {}).get("content")
        if isinstance(c, list):
            c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
        if c:
            return str(c)
    return ""


def tool_uses(path: Optional[str]) -> list[dict]:
    """Every tool call recorded in a transcript: {'name': ..., 'input': {...}}."""
    out = []
    for r in transcript_rows(path):
        c = (r.get("message") or {}).get("content")
        if isinstance(c, list):
            for x in c:
                if isinstance(x, dict) and x.get("type") == "tool_use":
                    out.append({"name": x.get("name"), "input": x.get("input") or {}})
    return out


def worktree_program(root: pathlib.Path, file_path: str) -> Optional[str]:
    """If file_path is inside a builder worktree, the program that worktree belongs to."""
    try:
        rel = pathlib.Path(file_path).resolve().relative_to((root / core.WORKTREES_DIR).resolve())
    except ValueError:
        return None
    name = rel.parts[0] if rel.parts else ""
    return name.split("__", 1)[0] if "__" in name else None


def run(main: Callable[[dict], None]) -> None:
    data = read_input()
    try:
        main(data)
    except Exception:  # a guard bug must never stop all work
        sys.stderr.write("check-tree hook error (allowing):\n" + traceback.format_exc()[-1500:])
    sys.exit(0)


SHELL_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\n)\s*")
