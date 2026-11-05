"""One recovery rule, and the stuck-PR alarm.

RECOVERY. One classifier (`classify_job`) decides whether a job was LOST (its runner vanished, a runner lost it at a time
limit, its only failed step posts a status, or GitHub itself failed under it) or really FAILED. A run whose failing jobs
were all lost is re-run ONCE (`rerun-failed-jobs`, first attempt only). A real failure is never re-run: re-running one
hides a bug and spends the shared runner pool. A person's cancel and a newer run's cancel are never lost.

THE STUCK-PR ALARM. An open, non-draft PR whose required status is pending or missing on its head, with no run queued
or in progress and nothing happening for 20 minutes, gets its latest run re-run once. If it is still not green 20
minutes after that, ONE comment names the stuck status, the run and the cause line from the log. At most one alarm per
head (the comment carries a marker for the head sha). A required status that FAILED is a real failure and is left alone.

BOUNDED. Every sweep stops at a call ceiling (`--max-calls`, default 150) because the API limit is shared.

Run: `python3 -m ops.recover event` (workflow_run event) or `python3 -m ops.recover sweep` (schedule)."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys

from .common import BudgetExceeded, Gh, load_settings, now_utc, parse_time

RUNNER_LOST = re.compile(r"(?i)lost communication with the server|received a shutdown signal|runner (?:has )?(?:been )?(?:decommissioned|removed)")
TIME_LIMIT = re.compile(r"(?i)exceeded the maximum execution time(?: of (?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?)?")
USER_CANCEL = re.compile(r"(?i)the run was canceled by @")
SUPERSEDED = re.compile(r"(?i)canceling since a higher priority waiting request")
INFRA_LOG = re.compile(
    r"(?i)egress is over the account limit|ServerBusy"
    r"|(?:HTTP[/ \d.]*|status(?: code)?:? |returned error:? |error:? )5\d\d\b"
    r"|\b(?:Bad Gateway|Service Unavailable|Gateway Time-?out|Internal Server Error)\b")
INFRA_STEP = re.compile(r"(?i)^(set up job|initialize containers|post |complete job|checkout|set ?up |upload |download |cache |"
                        r"run actions/(?:checkout|setup-|cache|upload-artifact|download-artifact))")
STATUS_STEP = re.compile(r"(?i)^(?:post|publish|report|set|update|write)\b.*\b(?:commit )?status(?:es)?\b")
FORCE_CANCEL_GRACE_SECONDS = 240  # a job still running this long past its stated limit was on a runner that never answered
BAD = ("failure", "cancelled", "timed_out")
ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")
STUCK_MINUTES = 20


def _limit_seconds(text: str) -> int | None:
    m = TIME_LIMIT.search(text)
    if not m or not any(m.groups()):
        return None
    h, mi, s = (int(g or 0) for g in m.groups())
    return h * 3600 + mi * 60 + s


def classify_job(job: dict, get_text=lambda: "") -> tuple[str, str]:
    """(`ok` | `lost` | `real`, why). `get_text()` supplies annotations plus the job log, and is called only when the steps cannot decide."""
    conclusion = job.get("conclusion")
    if conclusion not in BAD:
        return "ok", ""
    steps = job.get("steps") or []
    failed = [s for s in steps if s.get("conclusion") == "failure"]
    if conclusion in ("cancelled", "timed_out"):
        text = get_text()
        if USER_CANCEL.search(text):
            return "real", "a person cancelled the run"
        if SUPERSEDED.search(text):
            return "real", "cancelled by a newer run"
        if TIME_LIMIT.search(text) or conclusion == "timed_out":
            limit, start, end = _limit_seconds(text), parse_time(job.get("started_at")), parse_time(job.get("completed_at"))
            past = (end - start).total_seconds() - limit if (limit and start and end) else None
            if RUNNER_LOST.search(text) or (past is not None and past >= FORCE_CANCEL_GRACE_SECONDS):
                return "lost", "cancelled at the time limit by a lost runner"
            return "real", "outlasted its time limit; a re-run would too"
        if RUNNER_LOST.search(text):
            return "lost", "runner lost"
        return "real", "cancelled with no stated cause"
    # concluded failure
    if not failed and not any(s.get("conclusion") == "cancelled" for s in steps) and any(s.get("conclusion") is None for s in steps):
        return "lost", "runner vanished: a step has no conclusion"
    if failed and all(STATUS_STEP.search(s.get("name", "")) for s in failed):
        return "lost", "only the status-post step failed"
    if any(not INFRA_STEP.search(s.get("name", "")) for s in failed):
        return "real", f"gate step failed: {next(s['name'] for s in failed if not INFRA_STEP.search(s.get('name', '')))}"
    text = get_text()
    if RUNNER_LOST.search(text):
        return "lost", "runner lost"
    if INFRA_LOG.search(text):
        return "lost", "GitHub infrastructure error in the log, no gate step failed"
    return "real", "failed with no infrastructure cause"


def _job_text(gh: Gh, job: dict) -> str:
    parts = []
    for path in (f"/repos/{gh.repo}/check-runs/{job['id']}/annotations", f"/repos/{gh.repo}/actions/jobs/{job['id']}/logs"):
        try:
            body = gh.get(path)
        except BudgetExceeded:
            raise
        except Exception:
            continue
        parts.append(json.dumps(body) if not isinstance(body, str) else body)
    return "\n".join(parts)


def _jobs(gh: Gh, run_id) -> list[dict]:
    return (gh.get(f"/repos/{gh.repo}/actions/runs/{run_id}/jobs?per_page=100&filter=latest") or {}).get("jobs") or []


def decide_run(gh: Gh, run: dict) -> dict:
    """Classify a completed run's jobs and re-run once when every failed job was lost. Returns {action, reason, jobs}."""
    if run.get("conclusion") not in BAD:
        return {"action": "none", "reason": f"concluded {run.get('conclusion')}"}
    if run.get("run_attempt", 1) != 1:
        return {"action": "none", "reason": f"attempt {run.get('run_attempt')}: re-run once only"}
    verdicts = []
    for job in _jobs(gh, run["id"]):
        if job.get("conclusion") not in BAD:
            continue
        v, why = classify_job(job, lambda j=job: _job_text(gh, j))
        verdicts.append({"job": job.get("name"), "verdict": v, "why": why})
        if v == "real":
            return {"action": "none", "reason": f"real failure: {job.get('name')}: {why}", "jobs": verdicts}
    if not verdicts:
        return {"action": "none", "reason": "no failed job"}
    newer = (gh.get(f"/repos/{gh.repo}/actions/workflows/{run['workflow_id']}/runs?branch={run.get('head_branch')}&per_page=10") or {}).get("workflow_runs") or []
    if any(r["id"] > run["id"] and r.get("conclusion") != "skipped" for r in newer):
        return {"action": "none", "reason": "a newer run of this workflow exists on the branch", "jobs": verdicts}
    gh.post(f"/repos/{gh.repo}/actions/runs/{run['id']}/rerun-failed-jobs")
    return {"action": "rerun", "reason": "every failed job was lost", "jobs": verdicts, "run": run["id"]}


# ── the stuck-PR alarm ──

def _cause_line(gh: Gh, run: dict | None) -> str:
    if not run:
        return "no run exists for this head"
    for job in _jobs(gh, run["id"]):
        if job.get("conclusion") in BAD:
            text = _job_text(gh, job)
            for line in text.splitlines():
                if "##[error]" in line or RUNNER_LOST.search(line) or INFRA_LOG.search(line):
                    return re.sub(r"\x1b\[[0-9;]*m", "", line.split("##[error]")[-1]).strip()[:200]
            return f"job '{job.get('name')}' {job.get('conclusion')}; no cause line in its log"
    return "every job succeeded but the status was never posted"


def alarm_marker(sha: str) -> str:
    return f"<!-- check-tree-stuck sha={sha} -->"


def check_pr(gh: Gh, cfg: dict, pr: dict, now: dt.datetime, minutes: int = STUCK_MINUTES) -> dict:
    """One open PR. Returns {pr, action: none|wait|rerun|alarm, reason}. Failed statuses and drafts are never this alarm's."""
    n, sha = pr["number"], pr["head"]["sha"]
    out = {"pr": n, "sha": sha}
    if pr.get("draft"):
        return {**out, "action": "none", "reason": "draft"}
    required = cfg.get("required_statuses") or []
    states = {s["context"]: s["state"] for s in (gh.get(f"/repos/{gh.repo}/commits/{sha}/status") or {}).get("statuses") or []}
    # `all-green` is a check run, not a commit status: read both, so a required name is found whichever it is.
    for cr in (gh.get(f"/repos/{gh.repo}/commits/{sha}/check-runs?per_page=100") or {}).get("check_runs") or []:
        if cr.get("status") == "completed" and cr.get("conclusion") != "skipped" and cr.get("name") not in states:
            states[cr["name"]] = "success" if cr.get("conclusion") in ("success", "neutral") else "failure"
    if any(states.get(c) in ("failure", "error") for c in required):
        return {**out, "action": "none", "reason": "a required status failed: a real failure"}
    stuck = [c for c in required if states.get(c) not in ("success",)]
    if not stuck:
        return {**out, "action": "none", "reason": "required statuses are green"}
    runs = (gh.get(f"/repos/{gh.repo}/actions/runs?head_sha={sha}&per_page=30") or {}).get("workflow_runs") or []
    if any(r.get("status") in ACTIVE for r in runs):
        return {**out, "action": "none", "reason": "a run is queued or in progress"}
    idle_since = max([t for t in (parse_time(r.get("updated_at")) for r in runs) if t], default=None)
    if idle_since is None:
        c = (gh.get(f"/repos/{gh.repo}/commits/{sha}") or {}).get("commit", {}).get("committer", {}).get("date")
        idle_since = parse_time(c)
    if idle_since is None:
        return {**out, "action": "none", "reason": "head age unknown"}
    idle = (now - idle_since).total_seconds() / 60
    check_file = os.path.basename(cfg["workflows"]["pr_check"])
    checks = sorted((r for r in runs if os.path.basename(r.get("path", "")) == check_file), key=lambda r: r.get("created_at", ""))
    rerun_done = any(r.get("run_attempt", 1) >= 2 for r in checks)
    need = minutes if (checks or rerun_done) else 2 * minutes
    if idle < need:
        return {**out, "action": "wait", "reason": f"idle {idle:.0f} min, under {need}"}
    if checks and not rerun_done:
        target = checks[-1]
        gh.post(f"/repos/{gh.repo}/actions/runs/{target['id']}/" + ("rerun-failed-jobs" if target.get("conclusion") in BAD else "rerun"))
        return {**out, "action": "rerun", "reason": f"{', '.join(stuck)} not green and idle {idle:.0f} min", "run": target["id"]}
    marker = alarm_marker(sha)
    comments = gh.get(f"/repos/{gh.repo}/issues/{n}/comments?per_page=100") or []
    if any(marker in (c.get("body") or "") for c in comments):
        return {**out, "action": "none", "reason": "already alarmed on this head"}
    run = checks[-1] if checks else (runs[-1] if runs else None)
    stuck_text = ", ".join(f"`{c}` ({states.get(c, 'missing')})" for c in stuck)
    body = (f"{marker}\n**Stuck: nothing has moved on this head for {idle:.0f} minutes.** Required status {stuck_text} is not green, "
            f"and the run was already re-run once.\n- Run: {run.get('html_url') if run else 'none'}\n- Cause: {_cause_line(gh, run)}\n"
            "This is the only alarm for this head; pushing a new commit starts over.")
    gh.post(f"/repos/{gh.repo}/issues/{n}/comments", {"body": body})
    return {**out, "action": "alarm", "reason": f"{', '.join(stuck)} not green after a re-run"}


def sweep_stuck(gh: Gh, cfg: dict, now: dt.datetime | None = None, max_prs: int = 40) -> list[dict]:
    now = now or now_utc()
    prs = gh.get(f"/repos/{gh.repo}/pulls?state=open&per_page=100") or []
    results = []
    for pr in [p for p in prs if not p.get("draft")][:max_prs]:
        try:
            results.append(check_pr(gh, cfg, pr, now))
        except BudgetExceeded as e:
            results.append({"pr": pr["number"], "action": "none", "reason": f"call ceiling: {e}"})
            break
    return results


def sweep_recent_runs(gh: Gh, cfg: dict, now: dt.datetime, hours: int = 2, per_workflow: int = 10) -> list[dict]:
    """Insurance for a dropped `workflow_run` event: recent failed first-attempt runs of the two workflows."""
    since = (now - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for key in ("pr_check", "queue_suite"):
        wf = os.path.basename(cfg["workflows"][key])
        runs = (gh.get(f"/repos/{gh.repo}/actions/workflows/{wf}/runs?status=completed&created=%3E{since}&per_page={per_workflow}") or {}).get("workflow_runs") or []
        for r in runs:
            if r.get("conclusion") in BAD and r.get("run_attempt", 1) == 1:
                out.append({"run": r["id"], **decide_run(gh, r)})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ops.recover")
    ap.add_argument("mode", choices=["event", "sweep"])
    ap.add_argument("--max-calls", type=int, default=150)
    a = ap.parse_args(argv)
    cfg = load_settings()
    gh = Gh(os.environ["GITHUB_REPOSITORY"], a.max_calls)
    lines: list[str] = []
    try:
        if a.mode == "event":
            ev = json.load(open(os.environ["GITHUB_EVENT_PATH"]))
            r = decide_run(gh, ev["workflow_run"])
            lines.append(f"run {ev['workflow_run']['id']}: {r['action']} ({r['reason']})")
        else:
            now = now_utc()
            for r in sweep_recent_runs(gh, cfg, now):
                lines.append(f"run {r['run']}: {r['action']} ({r['reason']})")
            for r in sweep_stuck(gh, cfg, now):
                lines.append(f"PR #{r['pr']}: {r['action']} ({r['reason']})")
    except BudgetExceeded as e:
        lines.append(f"call ceiling reached: {e}")
    lines.append(f"API calls: {gh.calls}")
    text = "\n".join(lines)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("### Recovery\n```\n" + text + "\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
