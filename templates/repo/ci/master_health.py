#!/usr/bin/env python3
"""Keep master honest: its health verdict is the commit status `master-health`, and the merge queue reads it.

    master_health.py post --sha SHA --state pending|success|failure [--description TEXT]   post it on one commit
    master_health.py broadcast --branch B --state success|failure                        copy a verdict onto every open PR into B
    master_health.py copy-to-pr --branch B --sha PR_HEAD_SHA                             copy master's latest SETTLED verdict onto one PR head

Why a copy: Mergify's `check-success=master-health` is read on the pull request, not on the base branch. So the verdict
is written onto each open PR when master changes (broadcast) and onto a PR when it is pushed (copy-to-pr). A verdict still
`pending` (the suite is running) is never copied: the last settled one stands, so a healthy master does not stall the
queue for the length of every full run. No verdict at all reads as success (a repo that has just installed this).
Needs GH_TOKEN and GITHUB_REPOSITORY, and `statuses: write`. Uses the `gh` CLI. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

CONTEXT = "master-health"
QUEUE_PREFIX = "mergify/merge-queue/"
LOOKBACK = 20


def settled(statuses: list[dict]) -> dict | None:
    """The newest `master-health` status that is success or failure, from a newest-first status list."""
    for s in statuses:
        if s.get("context") == CONTEXT and s.get("state") in ("success", "failure", "error"):
            return s
    return None


def verdict_to_copy(settled_status: dict | None) -> tuple[str, str]:
    if settled_status is None:
        return "success", "master has no health verdict yet"
    state = "failure" if settled_status["state"] in ("failure", "error") else "success"
    return state, (settled_status.get("description") or f"master is {state}")[:140]


def broadcast_targets(prs: list[dict]) -> list[str]:
    """Head shas to post on: every open PR, except the queue's own drafts (they are never queued themselves)."""
    return [p["head"]["sha"] for p in prs if not str(p["head"].get("ref", "")).startswith(QUEUE_PREFIX)]


def gh(*args: str, stdin: str | None = None) -> str:
    p = subprocess.run(["gh", *args], capture_output=True, text=True, input=stdin)
    if p.returncode != 0:
        raise SystemExit(f"gh {' '.join(args[:2])} failed: {p.stderr.strip()[:300]}")
    return p.stdout


def post(repo: str, sha: str, state: str, description: str, url: str | None = None) -> None:
    fields = ["-f", f"state={state}", "-f", f"context={CONTEXT}", "-f", f"description={description[:140]}"]
    if url:
        fields += ["-f", f"target_url={url}"]
    gh("api", "-X", "POST", f"repos/{repo}/statuses/{sha}", *fields)


def latest_settled(repo: str, branch: str) -> dict | None:
    commits = json.loads(gh("api", f"repos/{repo}/commits?sha={branch}&per_page={LOOKBACK}"))
    for c in commits:
        found = settled(json.loads(gh("api", f"repos/{repo}/commits/{c['sha']}/statuses?per_page=100")))
        if found:
            return found
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("post")
    p1.add_argument("--sha", required=True)
    p1.add_argument("--state", required=True, choices=["pending", "success", "failure"])
    p1.add_argument("--description", default="")
    p1.add_argument("--url")
    p2 = sub.add_parser("broadcast")
    p2.add_argument("--branch", required=True)
    p2.add_argument("--state", required=True, choices=["success", "failure"])
    p2.add_argument("--description", default="")
    p3 = sub.add_parser("copy-to-pr")
    p3.add_argument("--branch", required=True)
    p3.add_argument("--sha", required=True)
    a = ap.parse_args(argv)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        raise SystemExit("GITHUB_REPOSITORY is not set")
    if a.cmd == "post":
        post(repo, a.sha, a.state, a.description, a.url)
    elif a.cmd == "broadcast":
        prs = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repo}/pulls?state=open&base={a.branch}&per_page=100"))
        flat = [x for page in prs for x in (page if isinstance(page, list) else [page])]
        desc = a.description or f"master is {'healthy' if a.state == 'success' else 'RED: the full suite fails on it'}"
        for sha in broadcast_targets(flat):
            post(repo, sha, a.state, desc)
        print(f"{CONTEXT}={a.state} posted on {len(broadcast_targets(flat))} open pull request(s)")
    else:
        state, desc = verdict_to_copy(latest_settled(repo, a.branch))
        post(repo, a.sha, state, desc)
        print(f"{CONTEXT}={state} copied onto {a.sha[:10]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
