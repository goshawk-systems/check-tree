"""The ops templates (recovery, stuck-PR alarm, flaky quarantine, daily report) driven with canned GitHub payloads."""
import datetime as dt
import json
import pathlib
import sys
import types

import pytest

PKG = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG / "templates" / "repo"))

from ops import daily_report as rep  # noqa: E402
from ops import quarantine as qr  # noqa: E402
from ops import recover as rc  # noqa: E402
from ops.common import BudgetExceeded, Gh, fleet_classes, load_settings, p90, median  # noqa: E402

R = "o/r"
NOW = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.timezone.utc)
CFG = {"default_branch": "main", "required_statuses": ["pr-check"], "product_prefixes": ["FC-"], "process_prefixes": ["PROC-"],
       "workflows": {"pr_check": "pr-check.yml", "queue_suite": "queue-suite.yml", "master_health": "master-health.yml"},
       "fleet": {"classes": {"spot": ["spot-runner"], "ondemand": {"labels": ["od"]}}}}


def ago(minutes):
    return (NOW - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGh:
    """Routes: exact path, else longest prefix. Records every POST."""
    repo = R

    def __init__(self, routes):
        self.routes, self.posts, self.calls = routes, [], 0

    def get(self, path):
        self.calls += 1
        if path in self.routes:
            return self.routes[path]
        best = max((k for k in self.routes if path.startswith(k)), key=len, default=None)
        if best is None:
            raise RuntimeError(f"unrouted {path}")
        return self.routes[best]

    def post(self, path, body=None):
        self.posts.append((path, body))
        return {}

    gql = ()

    def graphql(self, query, variables=None):
        self.gql = (*self.gql, (query, variables))
        return {}


def job(conclusion, steps=(), **kw):
    return {"id": 1, "name": "tests", "conclusion": conclusion, "steps": [dict(s) for s in steps], **kw}


def step(name, conclusion):
    return {"name": name, "conclusion": conclusion}


# ── one classifier: lost vs real ──

def test_a_job_that_succeeded_is_ok():
    assert rc.classify_job(job("success"))[0] == "ok"


def test_runner_vanished_when_a_step_has_no_conclusion():
    j = job("failure", [step("Set up job", "success"), step("Run tests", None), step("Complete job", "skipped")])
    assert rc.classify_job(j)[0] == "lost"


def test_a_failed_gate_step_is_real_and_needs_no_log():
    called = []
    j = job("failure", [step("Run tests", "failure")])
    assert rc.classify_job(j, lambda: called.append(1) or "")[0] == "real" and not called


def test_only_a_status_post_step_failing_is_lost():
    assert rc.classify_job(job("failure", [step("Run tests", "success"), step("Post pr-check status", "failure")]))[0] == "lost"
    assert rc.classify_job(job("failure", [step("Run tests", "failure"), step("Post pr-check status", "failure")]))[0] == "real"


def test_cancelled_at_the_limit_is_lost_only_when_the_runner_was_lost():
    base = dict(started_at="2026-11-05T10:00:00Z", completed_at="2026-11-05T10:30:00Z")
    limit = "The job has exceeded the maximum execution time of 30m0s"
    assert rc.classify_job(job("cancelled", **base), lambda: limit + " The runner has received a shutdown signal")[0] == "lost"
    assert rc.classify_job(job("cancelled", **base), lambda: limit)[0] == "real"  # a genuine timeout is not re-run
    late = dict(started_at="2026-11-05T10:00:00Z", completed_at="2026-11-05T10:40:00Z")
    assert rc.classify_job(job("cancelled", **late), lambda: limit)[0] == "lost"  # ran 10 min past its limit: never answered the cancel


def test_a_person_or_a_newer_run_cancelling_is_never_lost():
    assert rc.classify_job(job("cancelled"), lambda: "The run was canceled by @sarah.")[0] == "real"
    assert rc.classify_job(job("cancelled"), lambda: "Canceling since a higher priority waiting request for pr-1 exists")[0] == "real"
    assert rc.classify_job(job("cancelled"), lambda: "")[0] == "real"


@pytest.mark.parametrize("text", ["fatal: HTTP 503 from github.com", "Error: Egress is over the account limit.", "Azure ServerBusy: try later",
                                  "The requested URL returned error: 502", "Bad Gateway"])
def test_github_infrastructure_errors_with_no_gate_step_failed_are_lost(text):
    assert rc.classify_job(job("failure", [step("Download artifact", "failure")]), lambda: text)[0] == "lost"


def test_the_same_error_text_with_a_gate_step_failed_is_real():
    assert rc.classify_job(job("failure", [step("Run tests", "failure")]), lambda: "HTTP 503")[0] == "real"
    assert rc.classify_job(job("failure", [step("Set up job", "failure")]), lambda: "AssertionError")[0] == "real"


# ── re-run once ──

def run(**kw):
    return {"id": 10, "conclusion": "failure", "run_attempt": 1, "workflow_id": 7, "head_branch": "b", **kw}


def routes(jobs, newer=()):
    return {f"/repos/{R}/actions/runs/10/jobs": {"jobs": jobs}, f"/repos/{R}/actions/workflows/7/runs": {"workflow_runs": list(newer)}}


LOST = job("failure", [step("Run tests", None)])
REAL = job("failure", [step("Run tests", "failure")])


def test_a_run_whose_failed_jobs_were_all_lost_is_re_run_once():
    gh = FakeGh(routes([LOST, job("success")]))
    r = rc.decide_run(gh, run())
    assert r["action"] == "rerun" and gh.posts == [(f"/repos/{R}/actions/runs/10/rerun-failed-jobs", None)]


def test_a_real_failure_is_never_re_run_even_beside_a_lost_job():
    gh = FakeGh(routes([LOST, REAL]))
    assert rc.decide_run(gh, run())["action"] == "none" and not gh.posts


def test_a_second_attempt_and_a_run_with_a_newer_run_are_left_alone():
    gh = FakeGh(routes([LOST]))
    assert rc.decide_run(gh, run(run_attempt=2))["action"] == "none" and not gh.posts
    gh = FakeGh(routes([LOST], newer=[{"id": 11, "conclusion": None}]))
    assert rc.decide_run(gh, run())["action"] == "none" and not gh.posts


def test_a_green_run_is_not_touched():
    gh = FakeGh({})
    assert rc.decide_run(gh, run(conclusion="success"))["action"] == "none" and gh.calls == 0


# ── the stuck-PR alarm ──

def pr(n=5, draft=False, sha="abc1234567"):
    return {"number": n, "draft": draft, "head": {"sha": sha}}


def pr_routes(status="pending", runs=(), comments=(), sha="abc1234567", commit_age=90):
    st = [] if status is None else [{"context": "pr-check", "state": status}]
    return {f"/repos/{R}/commits/{sha}/status": {"statuses": st},
            f"/repos/{R}/actions/runs?head_sha={sha}": {"workflow_runs": list(runs)},
            f"/repos/{R}/commits/{sha}": {"commit": {"committer": {"date": ago(commit_age)}}},
            f"/repos/{R}/issues/5/comments": list(comments),
            f"/repos/{R}/actions/runs/20/jobs": {"jobs": [job("failure", [step("Post pr-check status", "failure")], id=9)]},
            f"/repos/{R}/check-runs/9/annotations": [],
            f"/repos/{R}/actions/jobs/9/logs": "2026-11-05T11:00:00Z ##[error]Resource not accessible by integration\n"}


def crun(attempt=1, status="completed", conclusion="failure", age=30, rid=20):
    return {"id": rid, "status": status, "conclusion": conclusion, "run_attempt": attempt, "updated_at": ago(age), "created_at": ago(age + 5),
            "path": ".github/workflows/pr-check.yml", "html_url": f"https://github.com/{R}/actions/runs/{rid}"}


def test_a_draft_and_a_pr_with_a_run_in_progress_are_left_alone():
    assert rc.check_pr(FakeGh({}), CFG, pr(draft=True), NOW)["action"] == "none"
    gh = FakeGh(pr_routes(runs=[crun(status="in_progress", conclusion=None)]))
    r = rc.check_pr(gh, CFG, pr(), NOW)
    assert r["action"] == "none" and "in progress" in r["reason"] and not gh.posts
    gh = FakeGh(pr_routes(runs=[crun(status="queued", conclusion=None)]))
    assert rc.check_pr(gh, CFG, pr(), NOW)["action"] == "none" and not gh.posts


def test_green_and_failed_required_statuses_are_not_stuck():
    assert rc.check_pr(FakeGh(pr_routes("success")), CFG, pr(), NOW)["action"] == "none"
    gh = FakeGh(pr_routes("failure"))
    assert "real failure" in rc.check_pr(gh, CFG, pr(), NOW)["reason"] and not gh.posts


def test_a_young_head_waits():
    gh = FakeGh(pr_routes(runs=[crun(age=5)]))
    assert rc.check_pr(gh, CFG, pr(), NOW)["action"] == "wait" and not gh.posts


def test_a_stuck_pending_status_is_re_run_once_then_alarmed_once():
    # first sweep: pending, idle 30 min, first attempt: re-run it
    gh = FakeGh(pr_routes(runs=[crun(attempt=1, age=30)]))
    r = rc.check_pr(gh, CFG, pr(), NOW)
    assert r["action"] == "rerun" and gh.posts == [(f"/repos/{R}/actions/runs/20/rerun-failed-jobs", None)]
    # the re-run finished 25 minutes ago and the status is still pending: ONE comment
    gh = FakeGh(pr_routes(runs=[crun(attempt=2, age=25)]))
    r = rc.check_pr(gh, CFG, pr(), NOW)
    assert r["action"] == "alarm" and len(gh.posts) == 1
    path, body = gh.posts[0]
    text = body["body"]
    assert path == f"/repos/{R}/issues/5/comments" and "`pr-check` (pending)" in text and "/actions/runs/20" in text
    assert "Resource not accessible by integration" in text and rc.alarm_marker("abc1234567") in text
    # the next sweep sees the marker and says nothing more
    gh = FakeGh(pr_routes(runs=[crun(attempt=2, age=25)], comments=[{"body": text}]))
    assert rc.check_pr(gh, CFG, pr(), NOW)["action"] == "none" and not gh.posts


def test_a_success_run_with_a_missing_status_is_re_run_whole():
    gh = FakeGh(pr_routes(status=None, runs=[crun(conclusion="success", age=30)]))
    rc.check_pr(gh, CFG, pr(), NOW)
    assert gh.posts == [(f"/repos/{R}/actions/runs/20/rerun", None)]


def test_no_run_at_all_alarms_only_after_twice_the_wait():
    gh = FakeGh(pr_routes(status=None, runs=[], commit_age=30))
    assert rc.check_pr(gh, CFG, pr(), NOW)["action"] == "wait"
    gh = FakeGh(pr_routes(status=None, runs=[], commit_age=45))
    r = rc.check_pr(gh, CFG, pr(), NOW)
    assert r["action"] == "alarm" and "no run exists" in gh.posts[0][1]["body"] and "(missing)" in gh.posts[0][1]["body"]


def test_the_sweep_skips_drafts_and_stops_at_the_call_ceiling(monkeypatch):
    r = pr_routes(runs=[crun(attempt=2, age=25)])
    r[f"/repos/{R}/pulls?state=open"] = [pr(5), pr(6, draft=True)]
    gh = FakeGh(r)
    out = rc.sweep_stuck(gh, CFG, NOW)
    assert [x["pr"] for x in out] == [5] and out[0]["action"] == "alarm"

    g = Gh(R, max_calls=2)
    monkeypatch.setattr("ops.common.subprocess.run", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="[]", stderr=""))
    g.get("/a"), g.get("/b")
    with pytest.raises(BudgetExceeded):
        g.get("/c")


# ── flaky quarantine ──

def qrun(rid, conclusion, minutes_ago, heads, tree=None):
    title = " ".join(f"pr-{n}@{s}" for n, s in heads)
    return {"id": rid, "conclusion": conclusion, "created_at": ago(minutes_ago), "head_sha": f"sha{rid}", "display_title": title,
            "head_commit": {"tree_id": tree or f"tree{rid}"}}


T = "tests/test_a.py::test_x"
H1, H2 = [(1, "aaaaaaa")], [(1, "aaaaaaa"), (2, "bbbbbbb")]


def test_failed_then_passed_on_identical_heads_is_flaky():
    runs = [qrun(1, "failure", 300, H1), qrun(2, "success", 200, H2)]  # the later draft holds the same heads and more
    out = qr.find_flaky(runs, {1: {T}})
    assert [o["node"] for o in out] == [T] and out[0]["passed_run"]["id"] == 2


def test_one_failure_alone_or_different_heads_or_beyond_24h_is_not_flaky():
    assert qr.find_flaky([qrun(1, "failure", 300, H1)], {1: {T}}) == []
    assert qr.find_flaky([qrun(1, "failure", 300, H2), qrun(2, "success", 200, H1)], {1: {T}}) == []  # the pass lacks PR 2's head
    assert qr.find_flaky([qrun(1, "failure", 2000, H1), qrun(2, "success", 100, H1)], {1: {T}}) == []  # 31 hours apart
    assert qr.find_flaky([qrun(1, "failure", 300, [])], {1: {T}}) == []  # heads unreadable


def test_a_failure_that_reproduces_is_never_quarantined():
    runs = [qrun(1, "failure", 400, H1, tree="same"), qrun(2, "failure", 300, H1, tree="same"), qrun(3, "success", 200, H2)]
    assert qr.find_flaky(runs, {1: {T}, 2: {T}}) == []


def test_a_run_failing_more_tests_than_the_cap_is_an_environment_fault():
    many = {f"tests/t.py::t{i}" for i in range(6)}
    assert qr.find_flaky([qrun(1, "failure", 300, H1), qrun(2, "success", 200, H1)], {1: many}) == []


def test_an_already_quarantined_test_is_not_proposed_again():
    runs = [qrun(1, "failure", 300, H1), qrun(2, "success", 200, H1)]
    assert qr.find_flaky(runs, {1: {T}}, quarantined={T}) == []


def test_failed_tests_are_read_from_the_pytest_summary_in_failed_job_logs():
    log = "2026-11-05T10:00:00Z FAILED tests/test_a.py::test_x - assert 1 == 2\n2026-11-05T10:00:01Z FAILED tests/test_b.py::test_y[1-a]\nPASSED x\n"
    gh = FakeGh({f"/repos/{R}/actions/runs/1/jobs": {"jobs": [{"id": 4, "conclusion": "failure"}, {"id": 5, "conclusion": "success"}]},
                 f"/repos/{R}/actions/jobs/4/logs": log})
    assert qr.failed_tests(gh, {"id": 1}) == {"tests/test_a.py::test_x", "tests/test_b.py::test_y[1-a]"}


def test_propose_writes_entries_that_expire_in_48_hours(tmp_path):
    flaky = qr.find_flaky([qrun(1, "failure", 300, H1), qrun(2, "success", 200, H1)], {1: {T}})
    files, body = qr.propose(flaky, tmp_path / "quarantine", NOW)
    e = json.loads(files[0].read_text())
    assert files[0].name == f"{e['id']}.json" and e["node"] == T
    assert e["added"] == "2026-11-05T12:00:00Z" and e["expires"] == "2026-11-07T12:00:00Z" and "1" in e["evidence"] and "2" in e["evidence"]
    assert T in body


def test_the_cap_of_five_live_entries_is_never_exceeded(tmp_path):
    d = tmp_path / "q"
    d.mkdir()
    for i in range(4):
        (d / f"e{i}.json").write_text(json.dumps({"id": f"e{i}", "node": f"t::{i}", "expires": "2026-11-06T00:00:00Z"}))
    (d / "old.json").write_text(json.dumps({"id": "old", "node": "t::old", "expires": "2026-11-01T00:00:00Z"}))  # expired: no slot
    flaky = [{"node": f"tests/x.py::n{i}", "failed_run": qrun(i, "failure", 9, H1), "passed_run": qrun(i + 50, "success", 5, H1)} for i in range(3)]
    files, _ = qr.propose(flaky, d, NOW)
    assert len(files) == 1  # 4 live + 1 = 5
    files, body = qr.propose(flaky, d, NOW)
    assert files == [] and "full" in body


# ── the daily report ──

def test_file_classes_and_pr_kinds():
    assert [rep.file_class(p) for p in (".github/workflows/a.yml", ".claude/x.md", "ci/run.sh", "ops/a.py", "docs/a.rst", "README.md",
                                        "tests/test_a.py", "src/test_b.py", "src/a_test.go", "src/app.py")] == \
        ["pipeline", "pipeline", "pipeline", "pipeline", "docs", "docs", "tests", "tests", "tests", "product"]
    p = lambda title, ref="x/y", login="u", typ="User": {"title": title, "head": {"ref": ref}, "user": {"login": login, "type": typ}}
    assert rep.pr_kind(p("PROC-12: faster gate"), CFG) == "process"
    assert rep.pr_kind(p("fix", ref="bc/fc944-who"), CFG) == "product"
    assert rep.pr_kind(p("Bump x", login="dependabot[bot]", typ="Bot"), CFG) == "bot"
    assert rep.pr_kind(p("cleanup"), CFG) == "unclassified"


def test_each_report_row_from_synthetic_merges():
    prs = [{"kind": "product", "check_runs": 3, "ready_min": 60, "files": [{"filename": "src/a.py", "additions": 60, "deletions": 0},
                                                                            {"filename": "tests/test_a.py", "additions": 20, "deletions": 0}]},
           {"kind": "process", "check_runs": 5, "ready_min": 120, "files": [{"filename": ".github/w.yml", "additions": 10, "deletions": 0},
                                                                             {"filename": "docs/a.md", "additions": 10, "deletions": 0}]},
           {"kind": "bot", "check_runs": 1, "ready_min": 10, "files": []}]
    m = rep.summarize(prs, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 42.0)
    assert m["merged_prs"] == 3 and m["checks_per_pr"] == 3.0
    assert (m["merged_product"], m["merged_process"], m["merged_bot"], m["merged_unclassified"]) == (1, 1, 1, 0)
    assert (m["share_product"], m["share_tests"], m["share_pipeline"], m["share_docs"]) == (60.0, 20.0, 10.0, 10.0)
    assert (m["ready_median"], m["ready_p90"]) == (60, 120)
    assert m["master_red_min"] == 42.0 and (m["wait_median"], m["wait_p90"]) == (5.5, 9)


def test_an_empty_day_reports_n_a_not_zero():
    m = rep.summarize([], [], 0)
    assert m["checks_per_pr"] is None and m["share_product"] is None and m["ready_median"] is None and m["wait_p90"] is None
    assert "n/a" in rep.render("2026-11-04", m, None, None)


def test_master_red_minutes_run_from_a_failure_to_the_next_success():
    day = dt.datetime(2026, 11, 4, tzinfo=dt.timezone.utc)
    end = day + dt.timedelta(days=1)
    t = lambda h, m=0: (day + dt.timedelta(hours=h, minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = [{"status": "completed", "conclusion": "success", "updated_at": t(-3)},
            {"status": "completed", "conclusion": "failure", "updated_at": t(2)},
            {"status": "completed", "conclusion": "failure", "updated_at": t(3)},
            {"status": "completed", "conclusion": "success", "updated_at": t(4)}]
    assert rep.red_minutes(runs, day, end) == 120
    assert rep.red_minutes(runs[:2], day, end) == 22 * 60  # still red at midnight
    assert rep.red_minutes([{"status": "completed", "conclusion": "failure", "updated_at": t(-2)}], day, end) == 24 * 60  # red since before the day


def test_runner_wait_overall_and_per_fleet_class():
    classes = fleet_classes(CFG)
    assert classes == {"spot": ["spot-runner"], "ondemand": ["od"]}
    jobs = [{"created_at": "2026-11-04T10:00:00Z", "started_at": "2026-11-04T10:02:00Z", "labels": ["spot-runner"]},
            {"created_at": "2026-11-04T10:00:00Z", "started_at": "2026-11-04T10:10:00Z", "labels": ["od"]},
            {"created_at": "2026-11-04T10:00:00Z", "started_at": None, "labels": ["od"]}]
    allw, by = rep.wait_minutes(jobs, classes)
    assert allw == [2.0, 10.0] and by == {"spot": [2.0], "ondemand": [10.0]}
    assert (median(allw), p90(allw)) == (6.0, 10.0)


def test_the_baseline_is_written_by_the_first_run_and_never_rewritten(tmp_path):
    path = tmp_path / "ops" / "baseline.json"
    first = rep.summarize([], [3.0], 5.0)
    base, d = rep.load_or_write_baseline(path, "2026-11-04", first)
    assert d == "2026-11-04" and path.exists()
    later = rep.summarize([], [30.0], 50.0)
    base2, d2 = rep.load_or_write_baseline(path, "2026-11-05", later)
    assert d2 == "2026-11-04" and base2["wait_median"] == 3.0
    text = rep.render("2026-11-05", later, base2, d2)
    assert "| Runner wait, median (min) | 30.0 | 3.0 |" in text and "Baseline (2026-11-04)" in text


def test_collect_reads_one_day_of_merged_prs():
    day = dt.date(2026, 11, 4)
    mk = lambda n, merged, ref, title: {"number": n, "merged_at": merged, "created_at": "2026-11-04T08:00:00Z", "updated_at": merged or "2026-11-04T08:00:00Z",
                                         "head": {"ref": ref}, "title": title, "user": {"login": "u", "type": "User"}}
    r = {f"/repos/{R}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page=2": [],
         f"/repos/{R}/pulls?state=closed": [mk(1, "2026-11-04T10:00:00Z", "b1", "FC-9 x"), mk(2, "2026-11-03T10:00:00Z", "b2", "old"), mk(3, None, "b3", "closed")],
         f"/repos/{R}/pulls/1/files": [{"filename": "src/a.py", "additions": 5, "deletions": 1}],
         f"/repos/{R}/issues/1/events": [{"event": "ready_for_review", "created_at": "2026-11-04T09:00:00Z"}],
         f"/repos/{R}/actions/workflows/pr-check.yml/runs?branch=b1": {"workflow_runs": [
             {"created_at": "2026-11-04T08:30:00Z", "run_attempt": 2}, {"created_at": "2026-11-04T09:30:00Z", "run_attempt": 1},
             {"created_at": "2026-11-03T08:30:00Z", "run_attempt": 4}]},
         f"/repos/{R}/actions/workflows/pr-check.yml/runs?created": {"workflow_runs": []},
         f"/repos/{R}/actions/workflows/queue-suite.yml/runs?created": {"workflow_runs": []},
         f"/repos/{R}/actions/workflows/master-health.yml/runs": {"workflow_runs": []}}
    prs, jobs, red = rep.collect(FakeGh(r), CFG, day)
    assert len(prs) == 1 and prs[0]["kind"] == "product" and prs[0]["check_runs"] == 3 and prs[0]["ready_min"] == 60 and red == 0


def test_settings_default_when_the_file_is_missing(tmp_path):
    cfg = load_settings(tmp_path)
    assert cfg["default_branch"] == "main" and cfg["workflows"]["pr_check"] == "pr-check.yml" and cfg["required_statuses"] == ["pr-check"]



def test_recover_listens_to_the_workflows_by_their_real_names():
    """A workflow_run trigger names workflows by their `name:`; a mismatch means the trigger never fires (found when
    installing into Goshawk, 2026-11-05)."""
    import yaml
    wf = PKG / "templates" / "repo" / ".github" / "workflows"
    names = {yaml.safe_load((wf / f).read_text())["name"] for f in ("pr-check.yml", "queue-suite.yml")}
    rec = yaml.safe_load((wf / "recover.yml").read_text())
    on = rec.get("on") or rec.get(True)
    assert set(on["workflow_run"]["workflows"]) == names, (on["workflow_run"]["workflows"], names)


# ── a ready PR whose pr-check really failed goes back to draft ──

SHA = "abcdef1234567890"
PYTEST_LOG = """2026-11-05T10:00:00.1Z FAILED tests/test_spoof.py::test_in_captured_output - not from the summary
2026-11-05T10:00:01.0Z =========================== short test summary info ============================
2026-11-05T10:00:01.1Z \x1b[31mFAILED\x1b[0m tests/test_a.py::test_x - assert 1 == 2
2026-11-05T10:00:01.2Z FAILED tests/test_b.py::test_y[1-a]
2026-11-05T10:00:01.3Z ERROR tests/test_c.py - ImportError
2026-11-05T10:00:01.4Z ============ 2 failed, 3 passed in 1.2s ============
2026-11-05T10:00:02.0Z FAILED tests/test_after.py::test_late
"""


def prun(**kw):
    return {"id": 10, "conclusion": "failure", "run_attempt": 1, "workflow_id": 7, "head_branch": "feature/x", "head_sha": SHA,
            "event": "pull_request", "path": ".github/workflows/pr-check.yml", "html_url": "https://example.invalid/run/10",
            "pull_requests": [{"number": 5}], **kw}


def ppr(**kw):
    return {"number": 5, "node_id": "PR_node5", "state": "open", "draft": False, "user": {"login": "sarah"},
            "head": {"sha": SHA, "ref": "feature/x"}, **kw}


def draft_routes(jobs, pull=None, comments=(), logs=None):
    r = {f"/repos/{R}/actions/runs/10/jobs": {"jobs": jobs}, f"/repos/{R}/pulls/5": pull or ppr(),
         f"/repos/{R}/issues/5/comments": list(comments), f"/repos/{R}/actions/workflows/7/runs": {"workflow_runs": []}}
    for j in jobs:
        r[f"/repos/{R}/actions/jobs/{j['id']}/logs"] = (logs or {}).get(j["id"], "")
    return r


def gate(jid, name, **kw):
    return {"id": jid, "name": name, "conclusion": "failure", "steps": [step("Run tests", "failure")], **kw}


def test_only_the_pytest_short_summary_names_failed_tests():
    assert rc.short_summary_failures(PYTEST_LOG) == ["tests/test_a.py::test_x", "tests/test_b.py::test_y[1-a]"]
    assert rc.short_summary_failures("FAILED tests/test_a.py::test_x\nno summary here") == []


def test_a_ready_pr_whose_pr_check_failed_for_real_goes_back_to_draft_with_one_comment_naming_jobs_and_tests():
    jobs = [gate(4, "impacted-tests (2)"), gate(5, "lint"), {"id": 6, "name": "pr-body", "conclusion": "success"}]
    gh = FakeGh(draft_routes(jobs, logs={4: PYTEST_LOG, 5: "ruff: 3 errors"}))
    r = rc.return_to_draft(gh, CFG, prun())
    assert r["action"] == "draft", r
    assert gh.gql == ((rc.CONVERT_TO_DRAFT, {"id": "PR_node5"}),)
    (path, body), = gh.posts
    assert path == f"/repos/{R}/issues/5/comments"
    text = body["body"]
    assert "`impacted-tests (2)`" in text and "`lint`" in text and "`pr-body`" not in text
    assert "`tests/test_a.py::test_x`" in text and "`tests/test_b.py::test_y[1-a]`" in text
    assert "test_spoof" not in text and "test_late" not in text and "test_c.py" not in text
    assert "gh workflow run pr-check.yml -f pr=" in text and f"sha={SHA}" in text


def test_the_comment_is_posted_once_per_head_even_when_the_pr_is_sent_back_again():
    jobs = [gate(4, "lint")]
    marker = f"<!-- check-tree-returned-to-draft sha={SHA} -->"
    gh = FakeGh(draft_routes(jobs, comments=[{"body": marker + " earlier"}]))
    assert rc.return_to_draft(gh, CFG, prun())["action"] == "draft"
    assert gh.gql and not gh.posts


def test_a_comment_lists_at_most_twenty_tests_and_cannot_be_broken_out_of_by_a_test_name():
    names = [f"FAILED tests/t.py::test_{i}" for i in range(25)] + ["FAILED tests/t.py::test_`evil`"]
    log = "=== short test summary info ===\n" + "\n".join(names) + "\n=== 26 failed ===\n"
    gh = FakeGh(draft_routes([gate(4, "impacted-tests (1)")], logs={4: log}))
    rc.return_to_draft(gh, CFG, prun())
    text = gh.posts[0][1]["body"]
    assert text.count("\n- `tests/t.py::test_") == 20 and "and 6 more" in text


def test_a_cancelled_run_a_lost_runner_a_timeout_and_a_mixed_failure_never_send_a_pr_back():
    lost = {"id": 4, "name": "impacted-tests (1)", "conclusion": "failure", "steps": [step("Run tests", None)]}
    timed = {"id": 4, "name": "impacted-tests (1)", "conclusion": "cancelled", "started_at": "2026-11-05T10:00:00Z",
             "completed_at": "2026-11-05T10:30:00Z", "steps": [step("Run tests", "cancelled")]}
    for run_, jobs in ((prun(conclusion="cancelled"), [gate(4, "lint")]), (prun(), [lost]), (prun(), [timed]),
                       (prun(), [gate(5, "lint"), lost]), (prun(), [lost, gate(5, "lint")]),
                       (prun(conclusion="timed_out"), [gate(4, "lint")])):
        gh = FakeGh(draft_routes(jobs, logs={4: "The job has exceeded the maximum execution time of 30m0s"}))
        assert rc.return_to_draft(gh, CFG, run_)["action"] == "none" and not gh.gql and not gh.posts, (run_["conclusion"], jobs)


@pytest.mark.parametrize("pull,run_", [
    (ppr(draft=True), {}), (ppr(state="closed"), {}),
    (ppr(head={"sha": "ffff000", "ref": "feature/x"}), {}),                       # the head moved on: an old run
    (ppr(head={"sha": SHA, "ref": "mergify/merge-queue/main/pr-5"}), {}),
    (ppr(user={"login": "dependabot[bot]"}), {}), (ppr(head={"sha": SHA, "ref": "dependabot/pip/x"}), {}),
    (ppr(), {"head_branch": "mergify/merge-queue/main/pr-5"}),
    (ppr(), {"event": "workflow_dispatch"}),                                      # a hand-run dispatch is not a ready PR's push
    (ppr(), {"path": ".github/workflows/queue-suite.yml"}),
])
def test_a_draft_a_moved_head_the_queue_dependabot_and_other_workflows_are_left_alone(pull, run_):
    gh = FakeGh(draft_routes([gate(4, "lint")], pull=pull))
    assert rc.return_to_draft(gh, CFG, prun(**run_))["action"] == "none" and not gh.gql and not gh.posts


def test_a_failed_second_attempt_is_judged_on_its_own_jobs():
    gh = FakeGh(draft_routes([gate(4, "lint")]))
    assert rc.decide_run(gh, prun(run_attempt=2))["action"] == "none"
    assert rc.return_to_draft(gh, CFG, prun(run_attempt=2))["action"] == "draft"


def test_the_fork_pr_is_found_from_the_head_sha_when_the_run_lists_no_pull_request():
    r = draft_routes([gate(4, "lint")])
    r[f"/repos/{R}/commits/{SHA}/pulls"] = [ppr()]
    gh = FakeGh(r)
    assert rc.return_to_draft(gh, CFG, prun(pull_requests=[]))["action"] == "draft"


def test_decide_run_judges_every_failed_job_so_a_real_failure_after_a_lost_one_is_seen():
    lost = {"id": 4, "name": "a", "conclusion": "failure", "steps": [step("Run tests", None)]}
    gh = FakeGh(routes([gate(5, "b"), lost]))
    r = rc.decide_run(gh, run())
    assert r["action"] == "none" and [v["verdict"] for v in r["jobs"]] == ["real", "lost"] and not gh.posts


def test_the_run_event_re_runs_a_lost_failure_and_never_sends_it_to_draft():
    jobs = [{"id": 4, "name": "impacted-tests (1)", "conclusion": "failure", "steps": [step("Run tests", None)]}]
    gh = FakeGh(draft_routes(jobs))
    d = rc.decide_run(gh, prun())
    assert d["action"] == "rerun" and not gh.gql
    assert rc.return_to_draft(gh, CFG, prun(), d["jobs"])["action"] == "none" and not gh.gql


def test_the_sweep_sends_back_a_pr_whose_run_event_was_dropped_and_leaves_a_lost_run_to_the_re_run():
    now = NOW
    gh = FakeGh({**draft_routes([gate(4, "lint")]),
                 f"/repos/{R}/actions/workflows/pr-check.yml/runs": {"workflow_runs": [prun()]},
                 f"/repos/{R}/actions/workflows/queue-suite.yml/runs": {"workflow_runs": []}})
    out = rc.sweep_recent_runs(gh, CFG, now)
    assert any(o["action"] == "draft" for o in out) and len(gh.gql) == 1


def test_the_real_gh_graphql_call_sends_the_query_and_variables_on_stdin_and_raises_on_errors(monkeypatch):
    import subprocess
    seen = {}

    def fake_run(args, input=None, **kw):
        seen["args"], seen["input"] = args, json.loads(input)
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(seen.get("reply", {"data": {"ok": 1}})), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    gh = Gh("o/r", 5)
    assert gh.graphql("mutation{x}", {"id": "A"}) == {"ok": 1}
    assert seen["args"] == ["gh", "api", "graphql", "--input", "-"] and seen["input"] == {"query": "mutation{x}", "variables": {"id": "A"}}
    seen["reply"] = {"errors": [{"message": "no"}]}
    with pytest.raises(RuntimeError):
        gh.graphql("mutation{x}")


def test_recover_may_comment_and_convert_a_pull_request_to_draft():
    import yaml
    wf = PKG / "templates" / "repo" / ".github" / "workflows"
    rec = yaml.safe_load((wf / "recover.yml").read_text())
    assert rec["permissions"]["pull-requests"] == "write" and rec["permissions"]["issues"] == "write"
    on = rec.get("on") or rec.get(True)
    assert "pr-check" in on["workflow_run"]["workflows"] and on["workflow_run"]["types"] == ["completed"]
