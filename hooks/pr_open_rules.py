#!/usr/bin/env python3
"""PreToolUse on Bash, on `gh pr create` and a POST to a repo's pulls endpoint (`gh api`): the rules for opening a PR.

(a) it opens as a draft (`--draft`/`-d`; for `gh api`, `-F draft=true`); (c) one open PR per work item: an id in
the body's `Closes-item:`/`Refs-item:` line may not already appear on another open PR (`gh pr list`), unless
this is a revert or the body has `Fixes-breakage:`; (d) at most `max_pr_files` (default 50) files changed against
`origin/<base>`; (e) a PR naming only items whose prefix is in `process_prefixes` (default PROC) is refused unless
an item is in `freeze_allowed_items` or the body has `Fixes-breakage:`. (b) "not from inside a subagent" is
`no_subagent_pr.py`, which reads the same parsed call. In `gh api`, `-f`/`-F` are fields (`-F` is typed; `@file`
reads a file), not a body file: only `gh pr create --body-file` is one.

Settings: `.check-tree/repo.json` keys max_pr_files, process_prefixes, freeze_allowed_items. If `gh` or the base
ref is unavailable, rules (c) and (d) pass (they cannot be checked); a body read from stdin is refused.
It cannot see: a PR opened from a script, the web page or another tool, or a body written after opening.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess

import _common as C
import _shell as S

ITEM_LINE = re.compile(r"^\s*(?:Closes|Refs)-item:\s*(.+)$", re.M | re.I)
ITEM_ID = re.compile(r"\b([A-Za-z][A-Za-z0-9]*)-(\d+)\b")


def item_ids(body: str) -> list:
    return [m.group(0).upper() for line in ITEM_LINE.findall(body) for m in ITEM_ID.finditer(line)]


def _open_pr_bodies(cwd: str) -> list:
    try:
        p = subprocess.run(["gh", "pr", "list", "--state", "open", "--limit", "300", "--json", "number,body"],
                           cwd=cwd, capture_output=True, text=True, timeout=60)
        return json.loads(p.stdout) if p.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


def refusal(call: S.PrCall, cfg: dict) -> "str | None":
    if not call.draft:
        return ("check-tree: open the pull request as a draft (`gh pr create --draft`; with gh api, "
                "`-F draft=true`). It is marked ready once its checks pass.")
    if call.opaque:
        return ("check-tree: the pull request body cannot be read (stdin or an unreadable file). Pass `--body-file "
                "<path>` or `--body` so its Closes-item / Refs-item line can be checked.")
    body = call.body
    base = call.base or C.core.DEFAULT_CONFIG["base_ref"]
    if call.fill and not body:
        code, body = C.core.git(pathlib.Path(call.cwd), "log", "--format=%B", f"origin/{base}..HEAD")
        body = body if code == 0 else ""
    escape = bool(re.search(r"^\s*Fixes-breakage:", body, re.M | re.I))
    revert = bool(re.match(r"\s*revert\b", call.title, re.I)) or "This reverts commit" in body
    ids = item_ids(body)
    if ids and not escape and all(i.split("-")[0] in cfg["process_prefixes"] for i in ids) and not any(
            i in cfg["freeze_allowed_items"] for i in ids):
        return (f"check-tree: {', '.join(ids)} is process work, which is frozen. Name an item that is not in "
                f"`process_prefixes`, ask the owner to add it to `freeze_allowed_items`, or add a "
                "`Fixes-breakage: <what is broken>` line if this repairs something broken.")
    if ids and not escape and not revert:
        for pr in _open_pr_bodies(call.cwd):
            shared = set(ids) & set(item_ids(pr.get("body") or ""))
            if shared:
                return (f"check-tree: PR #{pr['number']} is already open for {', '.join(sorted(shared))}. One open "
                        "pull request per work item: add your change to it, or wait for it to land.")
    code, names = C.core.git(pathlib.Path(call.cwd), "diff", "--name-only", f"origin/{base}...HEAD")
    n = len([x for x in names.splitlines() if x.strip()]) if code == 0 else 0
    if n > cfg["max_pr_files"]:
        return (f"check-tree: this pull request changes {n} files against origin/{base} (limit "
                f"{cfg['max_pr_files']}). Land the work in smaller steps, one proven step per pull request.")
    return None


def main(data: dict) -> None:
    if data.get("tool_name") != "Bash":
        return
    cfg = C.repo_config(data)
    for call in S.pr_open_calls((data.get("tool_input") or {}).get("command", ""), data.get("cwd") or ""):
        reason = refusal(call, cfg)
        if reason:
            C.deny(reason)
            return


if __name__ == "__main__":
    C.run(main)
