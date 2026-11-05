"""Flaky-test quarantine, proposed from the merge queue's own evidence.

A test is FLAKY when it failed in a queue run and PASSED, within 24 hours, in another successful run whose tree holds
the identical pull-request heads (every (PR, head sha) of the failing run is also in the passing one). Never on one
failure alone; never a test whose failure REPRODUCES (it failed in two runs of the same tree); never a run that failed
more tests than the cap (one environment fault, not a flaky test).

An entry is one file `tests/quarantine/<id>.json`, expires 48 hours after it was added, and at most MAX_CAP entries
live at once. The script writes the files and a PR body; the workflow opens a DRAFT pull request. It never merges.

How a run's PR heads are read (`heads_of`): the run's `display_title` carries tokens `pr-<N>@<sha>`
(have the queue workflow's `run-name:` print them), else the run's `pull_requests` array. A run whose heads cannot
be read is never evidence.

Run: `python3 -m ops.quarantine [--propose]`."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from .common import BudgetExceeded, Gh, iso, load_settings, now_utc, parse_time

MAX_CAP = 5
MAX_HOURS = 48
WINDOW_HOURS = 24
QUARANTINE_DIR = "tests/quarantine"
FAILED_LINE = re.compile(r"^(?:\S+\s+)?FAILED\s+(\S+?::\S+?)(?:\s+-\s|\s*$)", re.M)
HEAD_TOKEN = re.compile(r"pr-(\d+)@([0-9a-f]{7,40})", re.I)


def heads_of(run: dict) -> frozenset:
    heads = {(int(n), s.lower()[:7]) for n, s in HEAD_TOKEN.findall(run.get("display_title") or "")}
    if not heads:
        heads = {(int(p["number"]), p["head"]["sha"][:7]) for p in run.get("pull_requests") or [] if p.get("head", {}).get("sha")}
    return frozenset(heads)


def failed_tests(gh: Gh, run: dict) -> set[str]:
    """Test node ids that failed in the run, read from the pytest summary lines of its failed jobs' logs."""
    found: set[str] = set()
    jobs = (gh.get(f"/repos/{gh.repo}/actions/runs/{run['id']}/jobs?per_page=100&filter=latest") or {}).get("jobs") or []
    for job in jobs:
        if job.get("conclusion") == "failure":
            log = gh.get(f"/repos/{gh.repo}/actions/jobs/{job['id']}/logs")
            if isinstance(log, str):
                found.update(FAILED_LINE.findall(log))
    return found


def load_entries(folder: Path) -> list[dict]:
    out = []
    for p in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        out.append(json.loads(p.read_text()))
    return out


def live_entries(entries: list[dict], now: dt.datetime) -> list[dict]:
    return [e for e in entries if (parse_time(e.get("expires")) or now) > now]


def find_flaky(runs: list[dict], tests_by_run: dict, quarantined: set[str] = frozenset(), cap: int = MAX_CAP) -> list[dict]:
    """`runs` are queue runs (dicts with id, conclusion, created_at, head_sha, display_title, head_commit.tree_id);
    `tests_by_run[id]` the set of tests that failed in it. Returns [{node, failed_run, passed_run}] for flaky tests only."""
    usable = [r for r in runs if r.get("conclusion") in ("failure", "success") and heads_of(r)]
    passing = [r for r in usable if r["conclusion"] == "success"]
    cands: dict[str, list[dict]] = {}
    for r in usable:
        fails = tests_by_run.get(r["id"], set()) if r["conclusion"] == "failure" else set()
        if not fails or len(fails) > cap:  # nothing failed, or an environment fault
            continue
        for t in fails:
            cands.setdefault(t, []).append(r)
    out = []
    for node, failing in sorted(cands.items()):
        if node in quarantined:
            continue
        trees = [(r.get("head_commit") or {}).get("tree_id") for r in failing]
        if any(t and trees.count(t) >= 2 for t in trees):
            continue  # failed twice on the same tree: it reproduces
        for f in sorted(failing, key=lambda r: r["created_at"]):
            ft = parse_time(f["created_at"])
            hit = next((p for p in sorted(passing, key=lambda r: r["created_at"])
                        if p["id"] != f["id"] and heads_of(f) <= heads_of(p)
                        and abs((parse_time(p["created_at"]) - ft).total_seconds()) <= WINDOW_HOURS * 3600), None)
            if hit:
                out.append({"node": node, "failed_run": f, "passed_run": hit})
                break
    return out


def _slug(node: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", node.lower()).strip("-")[:60]


def propose(flaky: list[dict], folder: Path, now: dt.datetime, cap: int = MAX_CAP) -> tuple[list[Path], str]:
    """Write one entry file per flaky test, within the cap on live entries. Returns (files, pull request body)."""
    room = cap - len(live_entries(load_entries(folder), now))
    written: list[Path] = []
    folder.mkdir(parents=True, exist_ok=True)
    for item in flaky[:max(room, 0)]:
        f, p = item["failed_run"], item["passed_run"]
        entry = {"id": _slug(item["node"]), "node": item["node"], "tracker": None,
                 "reason": "failed in a queue run and passed in another run holding the identical PR heads",
                 "evidence": f"failed run {f['id']}, passed run {p['id']}", "run_sha": f.get("head_sha", ""),
                 "added": iso(now), "expires": iso(now + dt.timedelta(hours=MAX_HOURS))}
        path = folder / f"{entry['id']}.json"
        path.write_text(json.dumps(entry, indent=1) + "\n")
        written.append(path)
    body = "Flaky-test quarantine proposal (draft; merge nothing until a person reads the evidence).\n\n"
    if room <= 0 and flaky:
        body += f"The cap of {cap} live entries is full: no entry written.\n"
    for pth in written:
        e = json.loads(pth.read_text())
        body += f"- `{e['node']}`: {e['evidence']}; expires {e['expires']}. Fix or delete it by then.\n"
    return written, body


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ops.quarantine")
    ap.add_argument("--propose", action="store_true")
    ap.add_argument("--max-calls", type=int, default=120)
    ap.add_argument("--body-file", default="quarantine-pr-body.md")
    a = ap.parse_args(argv)
    cfg = load_settings()
    gh = Gh(os.environ["GITHUB_REPOSITORY"], a.max_calls)
    now = now_utc()
    wf = os.path.basename(cfg["workflows"]["queue_suite"])
    since = iso(now - dt.timedelta(hours=2 * WINDOW_HOURS))
    folder = Path(QUARANTINE_DIR)
    quarantined = {e["node"] for e in live_entries(load_entries(folder), now)}
    runs, tests = [], {}
    try:
        runs = (gh.get(f"/repos/{gh.repo}/actions/workflows/{wf}/runs?status=completed&created=%3E{since}&per_page=60") or {}).get("workflow_runs") or []
        for r in runs:
            if r.get("conclusion") == "failure" and heads_of(r):
                tests[r["id"]] = failed_tests(gh, r)
    except BudgetExceeded as e:
        print(f"call ceiling reached: {e}")
    flaky = find_flaky(runs, tests, quarantined)
    print(f"{len(flaky)} flaky test(s): {[f['node'] for f in flaky]}; API calls {gh.calls}")
    if a.propose and flaky:
        files, body = propose(flaky, folder, now)
        Path(a.body_file).write_text(body)
        print("wrote", [str(f) for f in files])
    return 0


if __name__ == "__main__":
    sys.exit(main())
