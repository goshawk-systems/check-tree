#!/usr/bin/env python3
"""PreToolUse on Bash, on `gh pr create`, a POST to a repo's pulls endpoint (`gh api`) and every way of marking a PR ready: the rules
for opening a PR and for marking it ready.

(a) it opens as a draft (`--draft`/`-d`; for `gh api`, `-F draft=true`); (c) one open PR per work item: an id in
the body's `Closes-item:`/`Refs-item:` line may not already appear on another open PR (`gh pr list`), unless
this is a revert or the body has `Fixes-breakage:`; (d) at most `max_pr_files` (default 50) files changed against
`origin/<base>`; (e) a PR naming only items whose prefix is in `process_prefixes` (default PROC) is refused unless
an item is in `freeze_allowed_items` or the body has `Fixes-breakage:`. (b) "not from inside a subagent" is
`no_subagent_pr.py`, which reads the same parsed call. In `gh api`, `-f`/`-F` are fields (`-F` is typed; `@file`
reads a file), not a body file: only `gh pr create --body-file` is one.

(f) A PR is marked ready only when its CURRENT head has a successful `all-green` (the check `pr-check.yml` posts). The ways of marking
ready that are read: `gh pr ready <n>` (not `--undo`), the GraphQL mutation `markPullRequestReadyForReview`, and `gh api -X PATCH
.../pulls/<n> -F draft=false`. A draft runs no checks, so the refusal names the command that runs them on it:
`gh workflow run pr-check.yml -f pr=<n>`. A revert and a body with `Fixes-breakage:` are exempt, as in (c) and (e). A head whose latest
`all-green` is failing, skipped, pending or missing is refused; if `gh` or the API is unavailable the rule passes (it cannot be checked).

Settings: `.check-tree/repo.json` keys max_pr_files, process_prefixes, freeze_allowed_items. If `gh` or the base
ref is unavailable, rules (c) and (d) pass (they cannot be checked); a body read from stdin is refused.
It cannot see: a PR opened or marked ready from a script, the web page or another tool, or a body written after opening. (The web
page's "Ready for review" is covered after the fact: `ready_for_review` runs pr-check, and a failure there sends the PR back to draft.)
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


GREEN_CHECK = "all-green"


def _gh_json(args: list, cwd: str):
    """`gh <args>` parsed, or None when gh is missing, fails or does not answer with JSON."""
    try:
        p = subprocess.run(["gh", *args], cwd=cwd or None, capture_output=True, text=True, timeout=60)
        return json.loads(p.stdout) if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _pull_request(call: S.ReadyCall) -> "dict | None":
    """{number, slug (owner/name), sha, title, body} of the pull request being marked ready, or None if it cannot be read."""
    if call.kind == "rest":
        d = _gh_json(["api", f"repos/{call.repo}/pulls/{call.number}"], call.cwd)
        return d and {"number": d.get("number") or call.number, "slug": call.repo, "sha": (d.get("head") or {}).get("sha", ""),
                      "title": d.get("title") or "", "body": d.get("body") or ""}
    if call.kind == "graphql":
        q = "query($id:ID!){node(id:$id){... on PullRequest{number url headRefOid title body}}}"
        d = ((_gh_json(["api", "graphql", "-f", f"query={q}", "-f", f"id={call.node_id}"], call.cwd) or {}).get("data") or {}).get("node")
    else:
        d = _gh_json(["pr", "view", *([call.selector] if call.selector else []), *(["-R", call.repo] if call.repo else []),
                      "--json", "number,url,headRefOid,title,body"], call.cwd)
    m = re.match(r"https?://[^/]+/([^/]+/[^/]+)/pull/", (d or {}).get("url") or "")
    if not d or not m:
        return None
    return {"number": d.get("number"), "slug": m.group(1), "sha": d.get("headRefOid") or "", "title": d.get("title") or "",
            "body": d.get("body") or ""}


def _green_state(slug: str, sha: str, cwd: str) -> "str | None":
    """The conclusion of the LATEST `all-green` on a commit (check run first, then a commit status of that name); "missing" if there is
    none, "pending" if it has not completed; None if the API cannot be read."""
    d = _gh_json(["api", f"repos/{slug}/commits/{sha}/check-runs?check_name={GREEN_CHECK}&per_page=100"], cwd)
    if d is None:
        return None
    runs = [r for r in d.get("check_runs") or [] if r.get("name", GREEN_CHECK) == GREEN_CHECK]
    if runs:
        last = max(runs, key=lambda r: r.get("id") or 0)
        return (last.get("conclusion") or "pending") if last.get("status", "completed") == "completed" else "pending"
    st = _gh_json(["api", f"repos/{slug}/commits/{sha}/status"], cwd)
    for s in (st or {}).get("statuses") or []:
        if s.get("context") == GREEN_CHECK:
            return "success" if s.get("state") == "success" else (s.get("state") or "pending")
    return "missing"


def ready_refusal(call: S.ReadyCall) -> "str | None":
    if call.opaque:
        return ("check-tree: this marks a pull request ready, but its id cannot be read from the command. Name it in the mutation "
                "(`pullRequestId: \"<id>\"`, or a variable passed with -f/-F) or use `gh pr ready <number>`.")
    pr = _pull_request(call)
    if not pr or not pr["sha"]:
        return None                       # gh or the API is unavailable: this rule cannot be checked
    body, title = pr["body"], pr["title"]
    if re.search(r"^\s*Fixes-breakage:", body, re.M | re.I) or re.match(r"\s*revert\b", title, re.I) or "This reverts commit" in body:
        return None
    state = _green_state(pr["slug"], pr["sha"], call.cwd)
    if state is None or state == "success":
        return None
    n = pr["number"]
    return (f"check-tree: pull request #{n} cannot be marked ready: its current head {pr['sha'][:7]} has not passed `pr-check` "
            f"(`{GREEN_CHECK}` is {state} on it). A draft runs no checks. Run them on the draft with "
            f"`gh workflow run pr-check.yml -f pr={n}`; it posts `{GREEN_CHECK}` on this head when it finishes, then mark it ready. "
            "A new push starts over. To repair something broken, add a `Fixes-breakage: <what is broken>` line to the description.")


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
    command, cwd = (data.get("tool_input") or {}).get("command", ""), data.get("cwd") or ""
    for call in S.pr_open_calls(command, cwd):
        reason = refusal(call, cfg)
        if reason:
            C.deny(reason)
            return
    for ready in S.pr_ready_calls(command, cwd):
        reason = ready_refusal(ready)
        if reason:
            C.deny(reason)
            return


if __name__ == "__main__":
    C.run(main)
