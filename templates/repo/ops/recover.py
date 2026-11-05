"""One recovery rule, the return of a failed ready PR to draft, and the stuck-PR alarm.

RECOVERY. One classifier (`classify_job`) decides whether a job was LOST (its runner vanished, a runner lost it at a time
limit, its only failed step posts a status, or GitHub itself failed under it) or really FAILED. A run whose failing jobs
were all lost is re-run ONCE (`rerun-failed-jobs`, first attempt only). A real failure is never re-run: re-running one
hides a bug and spends the shared runner pool. A person's cancel and a newer run's cancel are never lost.

RETURN TO DRAFT. A ready PR (not a draft, not the merge queue's, not Dependabot's) whose `pr-check` run FAILED on its current head, with
every failed job a real failure (the same `classify_job`: no lost runner, no cancel, no timeout), goes back to draft (GraphQL
`convertPullRequestToDraft`) with ONE comment per head naming the failed jobs and the test node ids from pytest's `short test summary
info` section only. Ready means passed: the PR is marked ready again only after `gh workflow run pr-check.yml -f pr=<n>` is green on
the new head (hooks/pr_open_rules.py). A cancelled run, a lost runner, a timeout, an old head and a PR that is already a draft are left alone.

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
from .quarantine import FAILED_LINE

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
QUEUE_PREFIX = "mergify/merge-queue/"
CONVERT_TO_DRAFT = "mutation($id:ID!){convertPullRequestToDraft(input:{pullRequestId:$id}){pullRequest{isDraft}}}"
LOG_TIME = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s?")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
SUMMARY_HEADER = re.compile(r"^=+ short test summary info =+$")
SECTION_RULE = re.compile(r"^=+ .* =+$")
MAX_LISTED = 20


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


def judge_jobs(gh: Gh, run: dict) -> list[dict]:
    """Every failed, cancelled or timed-out job of a run, classified: [{job, id, conclusion, verdict (lost|real), why}]."""
    out = []
    for job in _jobs(gh, run["id"]):
        if job.get("conclusion") not in BAD:
            continue
        v, why = classify_job(job, lambda j=job: _job_text(gh, j))
        out.append({"job": job.get("name"), "id": job.get("id"), "conclusion": job.get("conclusion"), "verdict": v, "why": why})
    return out


def decide_run(gh: Gh, run: dict) -> dict:
    """Classify a completed run's jobs and re-run once when every failed job was lost. Returns {action, reason, jobs}."""
    if run.get("conclusion") not in BAD:
        return {"action": "none", "reason": f"concluded {run.get('conclusion')}"}
    if run.get("run_attempt", 1) != 1:
        return {"action": "none", "reason": f"attempt {run.get('run_attempt')}: re-run once only"}
    verdicts = judge_jobs(gh, run)
    real = next((v for v in verdicts if v["verdict"] == "real"), None)
    if real:
        return {"action": "none", "reason": f"real failure: {real['job']}: {real['why']}", "jobs": verdicts}
    if not verdicts:
        return {"action": "none", "reason": "no failed job"}
    newer = (gh.get(f"/repos/{gh.repo}/actions/workflows/{run['workflow_id']}/runs?branch={run.get('head_branch')}&per_page=10") or {}).get("workflow_runs") or []
    if any(r["id"] > run["id"] and r.get("conclusion") != "skipped" for r in newer):
        return {"action": "none", "reason": "a newer run of this workflow exists on the branch", "jobs": verdicts}
    gh.post(f"/repos/{gh.repo}/actions/runs/{run['id']}/rerun-failed-jobs")
    return {"action": "rerun", "reason": "every failed job was lost", "jobs": verdicts, "run": run["id"]}


# ── a ready PR whose pr-check really failed goes back to draft ──

def short_summary_failures(log: str) -> list[str]:
    """Test node ids from the `short test summary info` section(s) of a pytest log, and nothing else in the log."""
    found: list[str] = []
    inside = False
    for raw in log.splitlines():
        line = ANSI.sub("", LOG_TIME.sub("", raw)).strip()
        if SUMMARY_HEADER.match(line):
            inside = True
        elif inside and SECTION_RULE.match(line):
            inside = False
        elif inside:
            m = FAILED_LINE.match(line)
            if m and m.group(1) not in found:
                found.append(m.group(1))
    return found


def _pr_of_run(gh: Gh, run: dict) -> dict | None:
    for p in run.get("pull_requests") or []:
        if p.get("number"):
            return gh.get(f"/repos/{gh.repo}/pulls/{p['number']}")
    for p in gh.get(f"/repos/{gh.repo}/commits/{run.get('head_sha')}/pulls") or []:  # a fork's run lists no pull request
        if p.get("state") == "open" and (p.get("head") or {}).get("sha") == run.get("head_sha"):
            return p
    return None


def _draft_comment(sha: str, run: dict, verdicts: list[dict], tests: list[str]) -> str:
    jobs = ", ".join(f"`{v['job']}`" for v in verdicts)
    lines = [f"<!-- check-tree-returned-to-draft sha={sha} -->",
             f"**Returned to draft: `pr-check` failed on head `{sha[:7]}`.** Failed: {jobs}."]
    if tests:
        shown = [t.replace("`", "") for t in tests[:MAX_LISTED]]
        lines += ["", "Failing tests (pytest short summary):", *[f"- `{t}`" for t in shown]]
        if len(tests) > MAX_LISTED:
            lines.append(f"- and {len(tests) - MAX_LISTED} more")
    lines += ["", f"Run: {run.get('html_url', '')}",
              "Fix it on the draft, then run `gh workflow run pr-check.yml -f pr=<this number>`; mark it ready once `all-green` is green on the new head."]
    return "\n".join(lines)


def return_to_draft(gh: Gh, cfg: dict, run: dict, verdicts: list[dict] | None = None) -> dict:
    """Send the run's PR back to draft when pr-check really failed on its current head. Returns {action: none|draft, reason}."""
    def none(why: str) -> dict:
        return {"action": "none", "reason": why, "run": run.get("id")}
    check_file = os.path.basename(cfg["workflows"]["pr_check"])
    if os.path.basename(str(run.get("path", ""))).split("@")[0] != check_file:
        return none("not the pull-request check")
    if run.get("event") != "pull_request" or str(run.get("head_branch", "")).startswith(QUEUE_PREFIX):
        return none("not a run on a pull request's own head")
    if run.get("conclusion") != "failure":
        return none(f"concluded {run.get('conclusion')}: only a failure is a real failure")
    pr = _pr_of_run(gh, run)
    if not pr:
        return none("no open pull request for this head")
    login, ref = (pr.get("user") or {}).get("login", ""), (pr.get("head") or {}).get("ref", "")
    if pr.get("state") != "open" or pr.get("draft"):
        return none("the pull request is closed or already a draft")
    if (pr.get("head") or {}).get("sha") != run.get("head_sha"):
        return none("the head moved on: this run is for an old head")
    if ref.startswith(QUEUE_PREFIX) or login == "dependabot[bot]" or ref.startswith("dependabot/"):
        return none("the merge queue's or Dependabot's pull request")
    verdicts = verdicts if verdicts is not None else judge_jobs(gh, run)
    if not verdicts or any(v["verdict"] != "real" or v["conclusion"] != "failure" for v in verdicts):
        return none("not every failed job was a real failure (a lost runner, a cancel or a timeout is never this)")
    tests: list[str] = []
    for v in verdicts:
        try:
            log = gh.get(f"/repos/{gh.repo}/actions/jobs/{v['id']}/logs")
        except BudgetExceeded:
            raise
        except Exception:
            continue
        tests += [t for t in short_summary_failures(log) if t not in tests] if isinstance(log, str) else []
    gh.graphql(CONVERT_TO_DRAFT, {"id": pr["node_id"]})
    marker = f"check-tree-returned-to-draft sha={run['head_sha']}"
    if not any(marker in (c.get("body") or "") for c in gh.get(f"/repos/{gh.repo}/issues/{pr['number']}/comments?per_page=100") or []):
        gh.post(f"/repos/{gh.repo}/issues/{pr['number']}/comments", {"body": _draft_comment(run["head_sha"], run, verdicts, tests)})
    return {"action": "draft", "reason": f"pr-check failed for real: {', '.join(v['job'] for v in verdicts)}", "run": run["id"], "pr": pr["number"]}


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
            decided = {"action": "none", "reason": "not a first attempt"}
            if r.get("conclusion") in BAD and r.get("run_attempt", 1) == 1:
                decided = decide_run(gh, r)
                out.append({"run": r["id"], **decided})
            if key == "pr_check" and r.get("conclusion") == "failure" and decided["action"] != "rerun":
                d = return_to_draft(gh, cfg, r, decided.get("jobs"))
                if d["action"] == "draft":
                    out.append({"run": r["id"], **d})
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
            if r["action"] != "rerun":
                d = return_to_draft(gh, cfg, ev["workflow_run"], r.get("jobs"))
                lines.append(f"run {ev['workflow_run']['id']}: {d['action']} ({d['reason']})")
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
