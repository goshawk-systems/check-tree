"""The daily goal report: how the pipeline did on the previous UTC day, beside this repository's own first-day baseline.

Rows: PR-check runs per merged PR; merged PRs by kind (product / process / bot, by the prefixes in `.check-tree/repo.json`);
the share of merged lines that are product code, tests, pipeline (`.github`, `.claude`, `ci/`, `ops/`) and docs;
ready-to-merged minutes (median, p90); master red minutes; minutes a job waited for a runner (median, p90, overall
and per `fleet.classes`). The baseline (`ops/baseline.json`) is written by the first run and never rewritten.

Run: `python3 -m ops.daily_report [--date YYYY-MM-DD] [--baseline PATH] [--out-dir DIR]`."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from .common import BudgetExceeded, Gh, fleet_classes, load_settings, median, now_utc, p90, parse_time

PIPELINE_DIRS = (".github/", ".claude/", "ci/", "ops/")
KINDS = ("product", "process", "bot", "unclassified")
ROWS = [  # key, label, unit
    ("checks_per_pr", "PR-check runs per merged PR", "runs"), ("merged_prs", "Merged PRs", "PRs"),
    ("merged_product", "  product PRs", "PRs"), ("merged_process", "  process PRs", "PRs"),
    ("merged_bot", "  bot PRs", "PRs"), ("merged_unclassified", "  unclassified PRs", "PRs"),
    ("share_product", "Merged lines: product code", "%"), ("share_tests", "Merged lines: tests", "%"),
    ("share_pipeline", "Merged lines: pipeline", "%"), ("share_docs", "Merged lines: docs", "%"),
    ("ready_median", "Ready to merged, median", "min"), ("ready_p90", "Ready to merged, p90", "min"),
    ("master_red_min", "Master red", "min"), ("wait_median", "Runner wait, median", "min"), ("wait_p90", "Runner wait, p90", "min"),
]


def file_class(path: str) -> str:
    if path.startswith(PIPELINE_DIRS):
        return "pipeline"
    if path.startswith("docs/") or path.lower().endswith((".md", ".rst", ".txt")):
        return "docs"
    low = "/" + path.lower()
    if "/tests/" in low or "/test/" in low or re.search(r"/(test_[^/]*|[^/]*_test)\.\w+$", low):
        return "tests"
    return "product"


def pr_kind(pr: dict, cfg: dict) -> str:
    user = pr.get("user") or {}
    if user.get("type") == "Bot" or str(user.get("login", "")).endswith("[bot]"):
        return "bot"
    text, ref = pr.get("title") or "", (pr.get("head") or {}).get("ref") or ""

    def hit(prefixes):
        for p in prefixes:
            stem = p.rstrip("-_/ ")
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(stem)}[-_ ]?\d+", text + " " + ref, re.I) or (p.endswith("/") and ref.startswith(p)):
                return True
        return False
    if hit(cfg.get("process_prefixes") or []):
        return "process"
    if hit(cfg.get("product_prefixes") or []):
        return "product"
    return "unclassified"


def red_minutes(runs: list[dict], start: dt.datetime, end: dt.datetime) -> float:
    """Minutes in [start, end) master was red: from a failed run's finish until the next successful run's finish."""
    events = sorted((parse_time(r["updated_at"]), r["conclusion"] == "failure") for r in runs
                    if r.get("status") == "completed" and r.get("conclusion") in ("failure", "success") and parse_time(r.get("updated_at")))
    red, since, total = False, start, 0.0
    for t, is_red in events:
        if t < start:
            red = is_red
            continue
        if t >= end:
            break
        if red:
            total += (t - since).total_seconds()
        red, since = is_red, t
    if red:
        total += (end - since).total_seconds()
    return total / 60


def wait_minutes(jobs: list[dict], classes: dict[str, list[str]]) -> tuple[list[float], dict[str, list[float]]]:
    allw, by = [], {}
    for j in jobs:
        c, s = parse_time(j.get("created_at")), parse_time(j.get("started_at"))
        if not (c and s):
            continue
        w = max(0.0, (s - c).total_seconds() / 60)
        allw.append(w)
        for name, labels in classes.items():
            if set(labels) & set(j.get("labels") or []):
                by.setdefault(name, []).append(w)
    return allw, by


def summarize(prs: list[dict], waits: list[float], red_min: float) -> dict:
    """`prs`: [{kind, files:[{filename,additions,deletions}], ready_min, check_runs}] for the merged PRs of the day."""
    n = len(prs)
    m: dict = {"merged_prs": n, "master_red_min": round(red_min, 1)}
    m["checks_per_pr"] = round(sum(p["check_runs"] for p in prs) / n, 2) if n else None
    for k in KINDS:
        m[f"merged_{k}"] = sum(1 for p in prs if p["kind"] == k)
    lines = {"product": 0, "tests": 0, "pipeline": 0, "docs": 0}
    for p in prs:
        for f in p["files"]:
            lines[file_class(f["filename"])] += f.get("additions", 0) + f.get("deletions", 0)
    tot = sum(lines.values())
    for k, v in lines.items():
        m[f"share_{k}"] = round(100 * v / tot, 1) if tot else None
    ready = [p["ready_min"] for p in prs if p.get("ready_min") is not None]
    m["ready_median"], m["ready_p90"] = (round(x, 1) if x is not None else None for x in (median(ready), p90(ready)))
    m["wait_median"], m["wait_p90"] = (round(x, 1) if x is not None else None for x in (median(waits), p90(waits)))
    return m


def render(date: str, m: dict, baseline: dict | None, base_date: str | None, by_class: dict | None = None) -> str:
    def fmt(v):
        return "n/a" if v is None else str(v)
    out = [f"## Pipeline goal report for {date} (UTC)", "",
           f"| Measure | {date} | Baseline ({base_date or 'this run'}) |", "|---|---|---|"]
    for key, label, unit in ROWS:
        out.append(f"| {label} ({unit}) | {fmt(m.get(key))} | {fmt((baseline or {}).get(key))} |")
    for name, ws in sorted((by_class or {}).items()):
        out.append(f"| Runner wait, {name}: median / p90 (min) | {round(median(ws), 1)} / {round(p90(ws), 1)} | |")
    return "\n".join(out) + "\n"


def load_or_write_baseline(path: Path, date: str, m: dict) -> tuple[dict, str]:
    if path.exists():
        d = json.loads(path.read_text())
        return d["metrics"], d["date"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": date, "metrics": m}, indent=1) + "\n")
    return m, date


# ── collection ──

def collect(gh: Gh, cfg: dict, day: dt.date) -> tuple[list[dict], list[dict], float]:
    start = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(days=1)
    wf = {k: os.path.basename(v) for k, v in cfg["workflows"].items()}
    merged = []
    for page in range(1, 6):
        batch = gh.get(f"/repos/{gh.repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page={page}") or []
        merged += [p for p in batch if p.get("merged_at") and start <= parse_time(p["merged_at"]) < end]
        if not batch or (parse_time(batch[-1]["updated_at"]) or end) < start:
            break
    prs = []
    for p in merged[:100]:
        n, ref = p["number"], p["head"]["ref"]
        files = gh.get(f"/repos/{gh.repo}/pulls/{n}/files?per_page=100") or []
        events = gh.get(f"/repos/{gh.repo}/issues/{n}/events?per_page=100") or []
        created, done = parse_time(p["created_at"]), parse_time(p["merged_at"])
        ready = max([parse_time(e["created_at"]) for e in events if e.get("event") == "ready_for_review"], default=created)
        runs = (gh.get(f"/repos/{gh.repo}/actions/workflows/{wf['pr_check']}/runs?branch={ref}&event=pull_request&per_page=100") or {}).get("workflow_runs") or []
        mine = [r for r in runs if created <= parse_time(r["created_at"]) <= done]
        prs.append({"kind": pr_kind(p, cfg), "files": files, "ready_min": (done - ready).total_seconds() / 60,
                    "check_runs": sum(r.get("run_attempt", 1) for r in mine)})
    jobs = []
    for key in ("pr_check", "queue_suite"):
        runs = (gh.get(f"/repos/{gh.repo}/actions/workflows/{wf[key]}/runs?created={day.isoformat()}&per_page=15") or {}).get("workflow_runs") or []
        for r in runs:
            jobs += (gh.get(f"/repos/{gh.repo}/actions/runs/{r['id']}/jobs?per_page=100&filter=latest") or {}).get("jobs") or []
    mh = (gh.get(f"/repos/{gh.repo}/actions/workflows/{wf['master_health']}/runs?branch={cfg['default_branch']}&per_page=100") or {}).get("workflow_runs") or []
    return prs, jobs, red_minutes(mh, start, end)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ops.daily_report")
    ap.add_argument("--date")
    ap.add_argument("--baseline", default="ops/baseline.json")
    ap.add_argument("--out-dir", default="reports")
    ap.add_argument("--max-calls", type=int, default=500)
    a = ap.parse_args(argv)
    cfg = load_settings()
    day = dt.date.fromisoformat(a.date) if a.date else (now_utc() - dt.timedelta(days=1)).date()
    gh = Gh(os.environ["GITHUB_REPOSITORY"], a.max_calls)
    try:
        prs, jobs, red = collect(gh, cfg, day)
    except BudgetExceeded as e:
        print(f"call ceiling reached: {e}")
        return 1
    waits, by_class = wait_minutes(jobs, fleet_classes(cfg))
    m = summarize(prs, waits, red)
    base, base_date = load_or_write_baseline(Path(a.baseline), str(day), m)
    text = render(str(day), m, base, base_date, by_class) + f"\nSampled {len(waits)} jobs; {gh.calls} API calls.\n"
    print(text)
    Path(a.out_dir).mkdir(parents=True, exist_ok=True)
    (Path(a.out_dir) / f"{day}.md").write_text(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
