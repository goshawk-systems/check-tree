"""The deployable CI package (templates/repo): the PR check, the merge queue, master health, and the scripts they call."""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import shutil
import subprocess
import sys

import pytest
import yaml

from conftest import PKG, sh

REPO_T = PKG / "templates" / "repo"
CI = REPO_T / "ci"
WF = REPO_T / ".github" / "workflows"
QUEUE = "mergify/merge-queue/"


def load_mod(name):
    spec = importlib.util.spec_from_file_location(f"pkgci_{name}", CI / f"{name}.py")
    sys.path.insert(0, str(CI))
    try:
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    finally:
        sys.path.remove(str(CI))
    return m


def wf(name):
    return yaml.safe_load((WF / name).read_text())


def triggers(doc):
    return doc.get("on", doc.get(True))


# ── workflow YAML ──

@pytest.mark.parametrize("name", ["pr-check.yml", "queue-suite.yml", "master-health.yml"])
def test_workflows_parse_and_have_jobs(name):
    doc = wf(name)
    assert doc["jobs"] and triggers(doc)


def test_pr_check_triggers_and_cancels_per_pr_but_never_a_queue_draft():
    doc = wf("pr-check.yml")
    t = triggers(doc)
    assert set(t["pull_request"]["types"]) == {"opened", "synchronize", "reopened", "ready_for_review"}
    c = doc["concurrency"]
    assert "pull_request.number" in c["group"] and "merge-queue" in str(c["cancel-in-progress"])
    # a dispatch for a pull request shares that pull request's group, so a newer push cancels a stale dispatch
    assert "inputs.pr" in c["group"]
    assert t["workflow_dispatch"]["inputs"]["pr"]["required"] is True


def test_queue_suite_triggers_and_posts_queue_suite():
    doc = wf("queue-suite.yml")
    t = triggers(doc)
    assert t["push"]["branches"] == [QUEUE + "**"] and "workflow_dispatch" in t and "workflow_call" in t
    assert doc["jobs"]["queue-suite"]["name"] == "queue-suite"
    assert set(doc["jobs"]["queue-suite"]["needs"]) == {"plan", "suite"}
    assert "fromJSON(needs.plan.outputs.matrix)" in doc["jobs"]["suite"]["strategy"]["matrix"]


def test_master_health_runs_the_suite_on_the_default_branch_only_and_posts_the_verdict():
    doc = wf("master-health.yml")
    assert "branches-ignore" in triggers(doc)["push"] or "branches" in triggers(doc)["push"]
    for job in doc["jobs"].values():
        assert "default_branch" in job["if"]
    assert doc["jobs"]["suite"]["uses"] == "./.github/workflows/queue-suite.yml"
    run = doc["jobs"]["verdict"]["steps"][-1]["run"]
    assert "master_health.py post" in run and "master_health.py broadcast" in run
    assert doc["jobs"]["verdict"]["permissions"]["statuses"] == "write"


# ── the `if:` expressions, evaluated on synthetic events ──

def gh_value(expr, ctx):
    """A GitHub Actions expression, by walking its syntax tree: &&, ||, !, ==, !=, null, strings, dotted reads,
    startsWith(a, b), always()."""
    src = re.sub(r"\bnull\b", "None", " ".join(expr.split()).replace("&&", " and ").replace("||", " or "))
    src = re.sub(r"!(?!=)", " not ", src).strip()

    def dotted(n):
        return n.id if isinstance(n, ast.Name) else f"{dotted(n.value)}.{n.attr}"

    def read(n):
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, (ast.Attribute, ast.Name)):
            cur = ctx
            for part in dotted(n).split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            return cur
        if isinstance(n, ast.BoolOp):
            val = None
            for o in n.values:
                val = read(o)
                if bool(val) == isinstance(n.op, ast.Or):
                    return val
            return val
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            return not read(n.operand)
        if isinstance(n, ast.Compare) and len(n.ops) == 1:
            l, r = read(n.left), read(n.comparators[0])
            return l == r if isinstance(n.ops[0], ast.Eq) else l != r
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            args = [read(a) for a in n.args]
            if n.func.id == "always":
                return True
            if n.func.id == "startsWith":
                return str(args[0] or "").lower().startswith(str(args[1] or "").lower())
        raise AssertionError(f"unsupported expression {ast.dump(n)} in {expr!r}")

    return read(ast.parse(src, mode="eval").body)


def if_of(job):
    cond = str(job["if"]).strip()
    m = re.fullmatch(r"\$\{\{(.*)\}\}", cond, re.S)
    return m.group(1) if m else cond


def event(draft, head):
    return {"github": {"event_name": "pull_request", "head_ref": head,
                       "event": {"pull_request": {"draft": draft, "number": 7}}},
            "needs": {"plan": {"outputs": {"run": "true", "reuse": "false"}}}}


PR_CHECK_JOBS = wf("pr-check.yml")["jobs"]


@pytest.mark.parametrize("job_id", sorted(PR_CHECK_JOBS))
def test_every_pr_check_job_skips_a_draft_and_runs_for_a_ready_pr_and_for_a_queue_draft(job_id):
    expr = if_of(PR_CHECK_JOBS[job_id])
    assert not gh_value(expr, event(True, "feature/x")), f"{job_id} runs for a draft PR"
    assert gh_value(expr, event(False, "feature/x")), f"{job_id} skips a ready PR"
    assert gh_value(expr, event(True, QUEUE + "main/pr-1+pr-2")), f"{job_id} skips the queue's draft"
    assert gh_value(expr, event(False, QUEUE + "main/pr-1")), f"{job_id} skips a non-draft queue PR"


def test_the_expression_evaluator_is_not_vacuous():
    never = "github.event.pull_request.draft"
    assert gh_value(never, event(True, "a")) and not gh_value(never, event(False, "a"))
    assert not gh_value("!startsWith(github.head_ref, 'mergify/merge-queue/')", event(True, QUEUE + "x"))


def test_all_green_needs_every_other_job_and_requires_success_of_each():
    jobs = PR_CHECK_JOBS
    assert set(jobs["all-green"]["needs"]) == set(jobs) - {"all-green"}
    assert gh_value(if_of({"if": jobs["all-green"]["name"]}), event(False, "feature/x")) == "all-green"
    assert "always()" in jobs["all-green"]["if"]
    script = jobs["all-green"]["steps"][-1]["run"]
    assert '.result == "success"' in script          # a skipped or cancelled job is not a pass


def test_pr_check_expected_jobs_exist():
    assert {"lint", "secret-scan", "impacted-tests", "pr-body", "mergify-config"} <= set(PR_CHECK_JOBS)


def test_no_workflow_interpolates_pr_text_into_a_shell_command():
    for name in ("pr-check.yml", "queue-suite.yml", "master-health.yml"):
        for job in wf(name)["jobs"].values():
            for step in job.get("steps", []):
                run = step.get("run", "")
                assert not re.search(r"\$\{\{\s*github\.(event\.pull_request\.(body|title)|head_ref)", run), (name, run)


@pytest.mark.skipif(not shutil.which("actionlint"), reason="actionlint not installed")
def test_actionlint_accepts_the_workflows():
    p = subprocess.run(["actionlint", *map(str, WF.glob("*.yml"))], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout


# ── PR body check ──

GOOD = """Closes-item: BILL-12

## What changed (required)

| file | why |
|---|---|
| src/billing/charge.py | doubles the charge |
| tests/test_charge.py | pins it |

Test edits: tests/test_charge.py — the old expectation was the bug; the assertion still pins the charge

## Consumer proof (required)

The invoice endpoint returned total 20 for a 10 item; reproduced with curl against the dev server.
"""
FILES = ["src/billing/charge.py", "tests/test_charge.py"]
PATTERN = "[A-Z][A-Z0-9]*-[0-9]+"


def body_problems(body, changed=FILES, edited=("tests/test_charge.py",)):
    return load_mod("check_pr_body").check(body, list(changed), list(edited), PATTERN)


def test_a_complete_body_passes():
    assert body_problems(GOOD) == []


def test_each_missing_required_item_fails_by_name():
    assert any("no work item" in p for p in body_problems(GOOD.replace("Closes-item: BILL-12", "Closes-item: <ID>")))
    assert any("no work item" in p for p in body_problems(GOOD.replace("BILL-12", "nope")))
    assert any("not named" in p and "src/billing/charge.py" in p for p in body_problems(GOOD.replace("src/billing/charge.py", "x")))
    assert any("Test edits" in p for p in body_problems(re.sub(r"(?m)^Test edits:.*$", "", GOOD)))
    assert any("Test edits" in p for p in body_problems(re.sub(r"(?m)^(Test edits:.*?—).*$", r"\1", GOOD)))   # no reason
    assert any("Consumer proof" in p for p in body_problems(GOOD.split("## Consumer proof")[0]))
    empty_proof = GOOD.split("The invoice")[0] + "<!-- what you reproduced -->\n"
    assert any("empty" in p for p in body_problems(empty_proof))
    assert any("empty" in p for p in body_problems(GOOD.split("The invoice")[0] + "n/a\n"))


def test_a_new_test_file_needs_no_test_edits_line_and_comments_never_count():
    body = re.sub(r"(?m)^Test edits:.*$", "", GOOD)
    assert body_problems(body, edited=()) == []
    commented = GOOD.replace("Closes-item: BILL-12", "<!-- Closes-item: BILL-12 -->")
    assert any("no work item" in p for p in body_problems(commented))


def test_size_limit_and_its_owner_override():
    m = load_mod("check_pr_body")
    many = [f"f{i}.txt" for i in range(51)]
    assert m.size_problem(many, "", 50) and not m.size_problem(many[:50], "", 50)
    assert not m.size_problem(many, "Big-PR-approved-by: Sarah", 50)


def make_repo(tmp_path):
    r = tmp_path / "r"
    (r / "src").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "src" / "a.py").write_text("x = 1\n")
    (r / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    sh(r, "git", "init", "-q", "-b", "main")
    sh(r, "git", "add", "-A")
    sh(r, "git", "commit", "-q", "-m", "start")
    sh(r, "git", "checkout", "-q", "-b", "change")
    return r


def test_the_cli_finds_an_edited_existing_test_from_git_and_a_new_one_is_exempt(tmp_path):
    r = make_repo(tmp_path)
    (r / "tests" / "test_a.py").write_text("def test_a():\n    assert 1 == 1\n")
    (r / "tests" / "test_new.py").write_text("def test_n():\n    assert True\n")
    sh(r, "git", "add", "-A")
    sh(r, "git", "commit", "-q", "-m", "change")
    body = ("Refs-item: AB-1\n## What changed\ntests/test_a.py tests/test_new.py\n## Consumer proof\nran it\n")
    (r / "body.md").write_text(body)
    cmd = [sys.executable, str(CI / "check_pr_body.py"), "--body", "body.md", "--base", "main"]
    p = subprocess.run(cmd, cwd=r, capture_output=True, text=True)
    assert p.returncode == 1 and "tests/test_a.py" in p.stdout and "test_new.py" not in p.stdout.split("Test edits")[-1]
    (r / "body.md").write_text(body + "Test edits: tests/test_a.py -- same check, written with ==\n")
    assert subprocess.run(cmd, cwd=r, capture_output=True, text=True).returncode == 0


# ── the merge queue ──

SETTINGS = {"default_branch": "main", "product_prefixes": ["FC", "SIM"], "process_prefixes": ["PROC"]}


def rendered(settings=SETTINGS):
    m = load_mod("render_mergify")
    return yaml.safe_load(m.render((REPO_T / ".mergify.template.yml").read_text(), {**load_mod("settings").DEFAULTS, **settings}))


def test_queue_rules_match_the_design():
    q = rendered()["queue_rules"][0]
    assert q["batch_size"] == {"min": 4, "max": 8} and q["max_checks_retries"] == 0
    flat = json.dumps(q["queue_conditions"])
    assert "check-success=all-green" in flat and "-draft" in q["queue_conditions"] and "check-success=master-health" in flat
    assert "Fixes-breakage" in flat                      # the way a fix gets in while master is red
    assert q["merge_conditions"] == ["check-success=queue-suite"]


def test_the_default_branch_comes_from_the_settings():
    q = rendered({"default_branch": "trunk"})
    assert "base=trunk" in q["queue_rules"][0]["queue_conditions"] and "base=trunk" in q["pull_request_rules"][0]["conditions"]
    assert "main" not in json.dumps(q["queue_rules"])


COND = re.compile(r"^(?P<neg>-)?(?P<attr>[a-z-]+)(?P<op>~=|=)(?P<arg>.*)$", re.S)


def holds(cond, pr):
    m = COND.match(cond)
    attr, arg, neg = m["attr"], m["arg"], bool(m["neg"])
    val = pr[attr]
    values = val if isinstance(val, list) else [val]
    if attr == "body":
        values = [re.sub(r"<!--.*?-->", "", v, flags=re.S) for v in values]
    hit = any((re.search(arg, v) if m["op"] == "~=" else v == arg) for v in values)
    return not hit if neg else hit


def priority_of(doc, **kw):
    pr = {"title": "", "body": "", "author": "dev", "head": "feature/x", "files": ["src/a.py"], **kw}
    for r in doc["priority_rules"]:
        if all(holds(c, pr) for c in r["conditions"]):
            return {"low": 1000, "medium": 2000, "high": 3000}.get(r["priority"], r["priority"])
    return 2000


def test_product_ranks_above_process_and_a_fix_for_a_red_master_ranks_first():
    d = rendered()
    product = priority_of(d, title="FC-12: scheduling fix")
    process = priority_of(d, title="PROC-5: tidy the pipeline")
    assert product > priority_of(d, title="a plain change") > process
    assert priority_of(d, title="x", body="Closes-item: SIM-9\n") == product
    assert priority_of(d, title="x", body="Refs-item: proc-9\n") == process
    assert priority_of(d, title="x", body="<!-- Closes-item: FC-1 -->\n") == 2000     # a comment never counts
    assert priority_of(d, title="PROC-5", body="Fixes-breakage: PROC-5\n") == 4000 > product
    assert priority_of(d, author="dependabot[bot]", title="Bump x") == 1000
    assert priority_of(d, title="Notes", files=["docs/a.md", "README.md"]) == 1000
    assert priority_of(d, title="Notes", files=["docs/a.md", "src/a.py"]) == 2000


def test_no_product_prefixes_means_no_product_rule():
    d = rendered({"product_prefixes": []})
    assert all("(FC" not in json.dumps(r) for r in d["priority_rules"])
    assert priority_of(d, title="PROC-5") == 1000


def test_a_prefix_is_matched_as_text_not_as_a_pattern():
    d = rendered({"product_prefixes": ["A.B"]})
    assert priority_of(d, title="A.B-1 x") == 3000 and priority_of(d, title="AxB-1 x") == 2000


def test_the_shipped_mergify_yml_is_what_the_template_renders_to_and_drift_is_caught(tmp_path):
    cmd = [sys.executable, str(CI / "render_mergify.py"), "--template", str(REPO_T / ".mergify.template.yml"),
           "--settings", str(tmp_path / "none.json")]
    assert subprocess.run([*cmd, "--check", "--out", str(REPO_T / ".mergify.yml")], capture_output=True, text=True).returncode == 0
    drifted = tmp_path / ".mergify.yml"
    drifted.write_text((REPO_T / ".mergify.yml").read_text().replace("max: 8", "max: 9"))
    p = subprocess.run([*cmd, "--check", "--out", str(drifted)], capture_output=True, text=True)
    assert p.returncode == 1 and "max: 9" in p.stdout
    s = tmp_path / "repo.json"
    s.write_text(json.dumps({"product_prefixes": ["ZZ"]}))
    cmd[-1] = str(s)
    assert subprocess.run([*cmd, "--check", "--out", str(REPO_T / ".mergify.yml")], capture_output=True, text=True).returncode == 1


# ── master health ──

def test_master_health_copies_only_a_settled_verdict_and_skips_queue_drafts():
    m = load_mod("master_health")
    pend = {"context": "master-health", "state": "pending", "description": "running"}
    red = {"context": "master-health", "state": "failure", "description": "RED"}
    green = {"context": "master-health", "state": "success", "description": "ok"}
    other = {"context": "ci", "state": "failure"}
    assert m.settled([pend, other, red, green]) == red       # newest first; pending is passed over
    assert m.settled([pend, other]) is None
    assert m.verdict_to_copy(None)[0] == "success"           # a repo with no verdict yet is not blocked
    assert m.verdict_to_copy(red) == ("failure", "RED")
    prs = [{"head": {"sha": "a1", "ref": "feat"}}, {"head": {"sha": "b2", "ref": QUEUE + "x"}}]
    assert m.broadcast_targets(prs) == ["a1"]


# ── sharding, settings, impacted tests ──

def test_settings_defaults_and_overrides(tmp_path):
    s = load_mod("settings")
    assert s.load(tmp_path / "missing.json")["max_pr_files"] == 50
    f = tmp_path / "repo.json"
    f.write_text(json.dumps({"max_pr_files": 7, "test_command": "make test"}))
    got = s.load(f)
    assert got["max_pr_files"] == 7 and got["test_command"] == "make test" and got["default_branch"] == "main"


def test_shards_follow_the_durations_file_and_cover_every_file_once():
    m = load_mod("shards")
    files = [f"tests/test_{i}.py" for i in range(10)]
    known = {f: 100.0 for f in files}
    assert m.shard_count(files, known, 300, 16) == 4          # 1000 s / 300 s
    assert m.shard_count(files, known, 300, 2) == 2           # capped
    assert m.shard_count(files, {}, 300, 16) == 4             # no file: min(4, files)
    assert m.shard_count(files[:2], {}, 300, 16) == 2
    parts = m.partition(files, known, 4)
    assert sorted(sum(parts, [])) == sorted(files) and max(map(len, parts)) - min(map(len, parts)) <= 1


def test_run_tests_runs_the_settings_command_with_the_files(tmp_path):
    (tmp_path / ".check-tree").mkdir()
    (tmp_path / ".check-tree" / "repo.json").write_text(json.dumps({"test_command": "echo ran"}))
    p = subprocess.run(["bash", str(CI / "run_tests.sh"), "a.py", "b.py"], cwd=tmp_path, capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip() == "ran a.py b.py"


def plant(tmp_path):
    r = tmp_path / "p"
    for d in ("src/app", "tests", "tools"):
        (r / d).mkdir(parents=True)
    (r / "src/app/__init__.py").write_text("")
    (r / "src/app/api.py").write_text("from app.service import run\n")
    (r / "src/app/service.py").write_text("from . import db\n\ndef run():\n    return db.q()\n")
    (r / "src/app/db.py").write_text("def q():\n    return 1\n")
    (r / "src/app/unrelated.py").write_text("X = 1\n")
    (r / "tools/helper.py").write_text("def h():\n    return 2\n")
    (r / "tests/test_api.py").write_text("from app.api import run\n")
    (r / "tests/test_unrelated.py").write_text("import app.unrelated\n")
    (r / "tests/test_bare.py").write_text("import sys\nsys.path.insert(0, 'tools')\nimport helper\n")
    (r / "tests/test_scan.py").write_text(
        "import pathlib\nROOT = pathlib.Path(__file__).parent.parent\nFILES = list(ROOT.rglob('*.md'))\n")
    sh(r, "git", "init", "-q", "-b", "main")
    sh(r, "git", "add", "-A")
    sh(r, "git", "commit", "-q", "-m", "start")
    sh(r, "git", "checkout", "-q", "-b", "change")
    return r


def impacted(r, *args):
    p = subprocess.run([sys.executable, str(PKG / "pipeline" / "impacted_tests.py"), "--base", "main", *args],
                       cwd=r, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.split()


def test_the_import_graph_reaches_a_test_through_two_hops_and_not_an_unrelated_one(tmp_path):
    r = plant(tmp_path)
    (r / "src/app/db.py").write_text("def q():\n    return 3\n")
    sh(r, "git", "commit", "-q", "-am", "change db")
    got = impacted(r, "--no-scanners")
    assert got == ["tests/test_api.py"]          # test_api -> api -> service -> db


def test_a_bare_name_import_after_a_sys_path_insert_is_followed(tmp_path):
    r = plant(tmp_path)
    (r / "tools/helper.py").write_text("def h():\n    return 5\n")
    sh(r, "git", "commit", "-q", "-am", "change helper")
    assert impacted(r, "--no-scanners") == ["tests/test_bare.py"]


def test_whole_tree_scanners_are_always_added_up_to_a_time_bound(tmp_path):
    r = plant(tmp_path)
    (r / "src/app/unrelated.py").write_text("X = 2\n")
    sh(r, "git", "commit", "-q", "-am", "change")
    assert impacted(r) == ["tests/test_scan.py", "tests/test_unrelated.py"]
    (r / "d.json").write_text(json.dumps({"tests/test_scan.py": 30.0}))
    assert impacted(r, "--durations", "d.json", "--scanner-max-seconds", "5") == ["tests/test_unrelated.py"]


def test_a_conftest_edit_selects_the_tests_under_it(tmp_path):
    r = plant(tmp_path)
    (r / "tests/conftest.py").write_text("X = 1\n")
    sh(r, "git", "add", "-A")
    sh(r, "git", "commit", "-q", "-m", "conftest")
    assert {"tests/test_api.py", "tests/test_bare.py"} <= set(impacted(r, "--no-scanners"))


def test_the_installed_copy_of_impacted_tests_is_the_pipeline_script():
    assert (CI / "impacted_tests.py").read_bytes() == (PKG / "pipeline" / "impacted_tests.py").read_bytes()


def test_the_pr_template_marks_the_four_required_items():
    t = (REPO_T / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text()
    for needle in ("Closes-item:", "## What changed (required)", "Test edits:", "## Consumer proof (required)"):
        assert needle in t
    assert t.count("(optional)") >= 3


def test_a_template_file_is_never_selected_as_a_test():
    """Files under templates/ are data the installer copies into a repo; collected inside an adopting repo they fail
    for want of the repo they describe (found installing into Goshawk, 2026-11-05)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("impacted_tests_tpl", PKG / "pipeline" / "impacted_tests.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert not mod.is_test(".claude/check-tree/templates/test_claude_md_size.py")
    assert not mod.is_test("templates/repo/tests/test_claude_md_size.py")
    assert mod.is_test("tests/test_claude_md_size.py") and mod.is_test("engine/talon/test_x.py")


def test_test_roots_keeps_another_projects_tests_out_of_the_selection():
    """A repo can hold other projects' tests (Goshawk's sample targets under rehearsal/); `test_roots` in the settings limits
    the selection to the repo's own (found on Goshawk #140, 2026-11-05: a sample target's test needed a module Goshawk never installs)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("impacted_tests_roots", PKG / "pipeline" / "impacted_tests.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.is_test("rehearsal/hostile-estate/tests/test_jobs.py")
    mod.TEST_ROOTS = ("engine", "tests", ".claude")
    assert not mod.is_test("rehearsal/hostile-estate/tests/test_jobs.py")
    assert mod.is_test("engine/talon/test_x.py") and mod.is_test("tests/test_claude_md_size.py")
    assert not mod.is_test("engineering/test_x.py"), "a root is a folder, not a name prefix"


def test_an_empty_shard_prints_nothing_and_the_workflows_drop_blank_lines(tmp_path):
    """A shard with no test must print nothing: `mapfile` reads a lone blank line as one empty file name, and pytest given ""
    collects the whole repository (found on Goshawk #140, 2026-11-05: shards 3 and 4 of a one-test change ran everything)."""
    r = plant(tmp_path)
    (r / "src/app/db.py").write_text("def q():\n    return 3\n")
    sh(r, "git", "commit", "-q", "-am", "change db")
    p = subprocess.run([sys.executable, str(PKG / "pipeline" / "impacted_tests.py"), "--base", "main", "--no-scanners",
                        "--shard", "2/4"], cwd=r, capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout == ""
    shell = subprocess.run(["bash", "-c", 'mapfile -t files < <(printf "\\n" | sed "/^$/d"); echo "${#files[@]}"'],
                           capture_output=True, text=True)
    assert shell.stdout.strip() == "0"
    for name, script in (("pr-check.yml", "impacted_tests.py"), ("queue-suite.yml", "shards.py")):
        lines = [ln for ln in (WF / name).read_text().splitlines() if "mapfile -t files" in ln and script in ln]
        assert lines and all(ln.rstrip().endswith('| sed "/^$/d")') for ln in lines), name
    src = (CI / "shards.py").read_text()
    assert "if parts[i - 1]:" in src


def test_a_description_edit_re_runs_the_description_jobs_one_attempt_at_a_time():
    """Re-running a job starts a new attempt of the run and GitHub refuses another re-run until it ends; the re-check must wait between
    the two jobs and read each id from the latest attempt (found on Goshawk #140, 2026-11-05: the failed pr-body was refused)."""
    doc = wf("pr-body-recheck.yml")
    assert triggers(doc) == {"pull_request": {"types": ["edited"]}}
    assert "all-green" not in doc["jobs"]
    script = doc["jobs"]["body-recheck"]["steps"][0]["run"]
    loop = script[script.index("for name in pr-size pr-body"):]
    assert loop.index('settle "$id"') < loop.index("/rerun")
    assert "$api/runs/$id/jobs" in loop, "each job id is read again from the latest attempt"
    p = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


# ── a ready PR must have passed pr-check on its current head (PROC-159) ──

def scenario(kind):
    """The context of one kind of pr-check run. A draft push runs nothing: `plan` is skipped, so it has no outputs."""
    pr = {"number": 7, "draft": kind in ("draft push", "queue draft")}
    head = QUEUE + "main/pr-7" if kind == "queue draft" else "feature/x"
    ctx = {"github": {"event_name": "pull_request", "head_ref": head, "event": {"action": "opened", "pull_request": pr}},
           "inputs": {}, "needs": {"plan": {"outputs": {"run": "true", "reuse": "false"}}}}
    if kind == "draft push":
        ctx["needs"]["plan"]["outputs"] = {}
    if kind == "ready_for_review on a green head":
        ctx["github"]["event"]["action"] = "ready_for_review"
        ctx["needs"]["plan"]["outputs"] = {"run": "false", "reuse": "true"}
    if kind == "dispatch":
        ctx = {"github": {"event_name": "workflow_dispatch", "head_ref": "", "event": {}}, "inputs": {"pr": "7"},
               "needs": {"plan": {"outputs": {"run": "true", "reuse": "false"}}}}
    return ctx


KINDS = ["draft push", "ready PR", "queue draft", "ready_for_review on a green head", "dispatch"]


def runs(job, ctx):
    return bool(gh_value(if_of(job), ctx))


def shown_name(job_id, job, ctx):
    name = str(job.get("name", job_id))
    return gh_value(if_of({"if": name}), ctx) if "${{" in name else name


@pytest.mark.parametrize("kind", KINDS)
def test_no_job_is_named_all_green_unless_it_runs_and_none_is_when_it_sits_on_another_commit(kind):
    """GitHub counts a skipped required check as success, which could mask a failing one (see pr-body-recheck.yml): a job named
    `all-green` that is skipped, or that sits on the default branch's commit for a dispatch, would do exactly that."""
    ctx = scenario(kind)
    for job_id, job in PR_CHECK_JOBS.items():
        name = shown_name(job_id, job, ctx)
        if name == "all-green":
            assert runs(job, ctx), f"{job_id} is skipped on a {kind} but would be named all-green"
            assert kind != "dispatch", "a dispatch's own jobs sit on the default branch's commit, not the PR's"


def test_the_aggregate_is_all_green_on_a_ready_pr_a_queue_draft_and_a_green_head_marked_ready():
    for kind in ("ready PR", "queue draft", "ready_for_review on a green head"):
        ctx = scenario(kind)
        job = PR_CHECK_JOBS["all-green"]
        assert shown_name("all-green", job, ctx) == "all-green" and runs(job, ctx), kind
    assert shown_name("all-green", PR_CHECK_JOBS["all-green"], scenario("dispatch")) == "all-green (dispatch)"
    assert shown_name("all-green", PR_CHECK_JOBS["all-green"], scenario("draft push")) != "all-green"


def test_a_dispatch_runs_every_job_on_the_named_pr_as_if_it_were_ready():
    ctx = scenario("dispatch")
    assert all(runs(job, ctx) for job in PR_CHECK_JOBS.values())


def test_a_ready_for_review_on_a_green_head_runs_no_test_job_again_but_still_posts_all_green():
    ctx = scenario("ready_for_review on a green head")
    ran = {j for j, job in PR_CHECK_JOBS.items() if runs(job, ctx)}
    assert ran == {"plan", "master-health-onto-pr", "all-green"}, ran


def test_a_draft_push_runs_nothing_and_costs_no_runner():
    ctx = scenario("draft push")
    assert [j for j, job in PR_CHECK_JOBS.items() if runs(job, ctx)] == []


def test_every_job_that_reads_the_pull_request_takes_it_from_plan_not_from_the_event():
    """A dispatch has no pull_request payload: a job reading github.base_ref or the event's head/body would be empty there."""
    text = (WF / "pr-check.yml").read_text()
    body = text[text.index("\njobs:"):]
    for needle in ("github.base_ref", "pull_request.head.sha", "pull_request.base.ref", "pull_request.body"):
        offenders = [ln for ln in body.splitlines() if needle in ln and "EVENT_" not in ln]
        assert not offenders, (needle, offenders)


# the scripts of the plan job and the aggregate, run for real against a fake `gh`

FAKE_GH = r"""#!%(py)s
import json, os, subprocess, sys
a = sys.argv[1:]
routes = json.load(open(os.environ["FAKE_ROUTES"]))
if "-X" in a or "--method" in a:
    open(os.environ["FAKE_POSTS"], "a").write(json.dumps(a) + "\n")
    sys.exit(1 if os.environ.get("FAKE_POST_FAILS") else 0)
path = a[1]
body = routes.get(path.split("?")[0] + ("?" + path.split("?")[1] if "?" in path else ""))
if body is None:
    body = routes.get(path.split("?")[0])
if body is None:
    sys.stderr.write("unrouted " + path + "\n"); sys.exit(1)
if "--jq" in a:
    sys.stdout.write(subprocess.run(["jq", "-r", a[a.index("--jq") + 1]], input=json.dumps(body), capture_output=True, text=True).stdout)
else:
    sys.stdout.write(json.dumps(body))
"""


@pytest.fixture
def fake_gh(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    g = b / "gh"
    g.write_text(FAKE_GH % {"py": sys.executable})
    g.chmod(0o755)
    routes, posts, out = tmp_path / "routes.json", tmp_path / "posts.log", tmp_path / "out.txt"
    routes.write_text("{}")
    posts.write_text("")
    out.write_text("")

    def run(script, env, routes_json=None):
        routes.write_text(json.dumps(routes_json or {}))
        posts.write_text("")
        out.write_text("")
        e = {"PATH": f"{b}:/usr/bin:/bin:/opt/homebrew/bin", "FAKE_ROUTES": str(routes), "FAKE_POSTS": str(posts),
             "GITHUB_OUTPUT": str(out), "GITHUB_REPOSITORY": "o/r", "RUNNER_TEMP": str(tmp_path), **env}
        p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e)
        outputs = dict(ln.split("=", 1) for ln in out.read_text().splitlines() if "=" in ln)
        return p, outputs, [json.loads(ln) for ln in posts.read_text().splitlines()]

    return run


def step_script(job, step_id=None):
    for st in PR_CHECK_JOBS[job]["steps"]:
        if "run" in st and (step_id is None or st.get("id") == step_id):
            return st["run"]
    raise AssertionError(job)


PLAN = step_script("plan", "plan")
CHECKS = "repos/o/r/commits/h1/check-runs?check_name=all-green&per_page=100"


def plan_env(event="pull_request", action="opened", pr="7", head_ref="feature/x"):
    return {"REPO": "o/r", "EVENT": event, "ACTION": action, "PR_NUMBER": pr, "EVENT_HEAD": "h1", "EVENT_BASE": "main",
            "HEAD_REF": head_ref, "GH_TOKEN": "x"}


def green(conclusion, rid=1):
    return {CHECKS: {"check_runs": [{"id": rid, "conclusion": conclusion}]}}


def test_marking_ready_a_head_that_already_has_a_successful_all_green_does_not_run_the_jobs_again(fake_gh):
    p, out, _ = fake_gh(PLAN, plan_env(action="ready_for_review"), green("success"))
    assert p.returncode == 0, p.stderr
    assert (out["run"], out["reuse"], out["head"], out["base"], out["ref"]) == ("false", "true", "h1", "main", "")


@pytest.mark.parametrize("routes", [{}, green("failure"), green("skipped"), {CHECKS: {"check_runs": []}},
                                    {CHECKS: {"check_runs": [{"id": 1, "conclusion": "success"}, {"id": 2, "conclusion": "failure"}]}}])
def test_marking_ready_a_head_without_a_successful_latest_all_green_runs_every_job(fake_gh, routes):
    p, out, _ = fake_gh(PLAN, plan_env(action="ready_for_review"), routes)
    assert p.returncode == 0, p.stderr
    assert (out["run"], out["reuse"]) == ("true", "false")


def test_only_ready_for_review_reuses_a_green_head_and_never_the_queues_own_pull_request(fake_gh):
    for env in (plan_env(action="synchronize"), plan_env(action="opened"),
                plan_env(action="ready_for_review", head_ref="mergify/merge-queue/main/pr-7")):
        p, out, _ = fake_gh(PLAN, env, green("success"))
        assert p.returncode == 0 and (out["run"], out["reuse"]) == ("true", "false"), env


PULL = {"state": "open", "mergeable": True, "head": {"sha": "h1", "ref": "feature/x"}, "base": {"ref": "develop"}}


def test_a_dispatch_reads_the_pull_request_and_checks_out_its_merge_ref(fake_gh):
    p, out, _ = fake_gh(PLAN, plan_env(event="workflow_dispatch", pr="7", head_ref=""), {"repos/o/r/pulls/7": PULL})
    assert p.returncode == 0, p.stderr
    assert (out["pr"], out["head"], out["base"], out["ref"], out["run"], out["reuse"]) == (
        "7", "h1", "develop", "refs/pull/7/merge", "true", "false")


@pytest.mark.parametrize("pr,pull", [("7; rm -rf /", PULL), ("", PULL), ("7", {**PULL, "state": "closed"}),
                                     ("7", {**PULL, "mergeable": False}),
                                     ("7", {**PULL, "head": {"sha": "h1", "ref": "mergify/merge-queue/main/pr-7"}})])
def test_a_dispatch_for_something_that_is_not_an_open_mergeable_pull_request_fails_with_nothing_run(fake_gh, pr, pull):
    p, out, _ = fake_gh(PLAN, plan_env(event="workflow_dispatch", pr=pr, head_ref=""), {"repos/o/r/pulls/7": pull})
    assert p.returncode != 0 and "run" not in out


ALL_GREEN = step_script("all-green")
NEEDS_OK = {n: {"result": "success"} for n in ("plan", "lint", "secret-scan", "impacted-tests", "pr-body", "pr-size", "mergify-config",
                                                  "master-health-onto-pr")}


def agg_env(needs, event="pull_request", reuse="false", head="h1"):
    return {"NEEDS": json.dumps(needs), "EVENT": event, "REUSE": reuse, "PR_NUMBER": "7", "HEAD_SHA": head, "GH_TOKEN": "x",
            "RUN_URL": "https://example.invalid/run/1"}


def test_the_aggregate_passes_only_when_every_job_succeeded_and_a_pull_request_event_posts_nothing(fake_gh):
    p, _, posts = fake_gh(ALL_GREEN, agg_env(NEEDS_OK))
    assert p.returncode == 0 and posts == []
    for bad in ("failure", "cancelled", "skipped"):
        p, _, posts = fake_gh(ALL_GREEN, agg_env({**NEEDS_OK, "lint": {"result": bad}}))
        assert p.returncode != 0 and posts == [], bad


def test_a_green_head_marked_ready_passes_with_its_jobs_skipped_but_never_over_a_failed_one(fake_gh):
    skipped = {**NEEDS_OK, **{n: {"result": "skipped"} for n in ("lint", "secret-scan", "impacted-tests", "pr-body", "pr-size", "mergify-config")}}
    assert fake_gh(ALL_GREEN, agg_env(skipped, reuse="true"))[0].returncode == 0
    assert fake_gh(ALL_GREEN, agg_env(skipped, reuse="false"))[0].returncode != 0           # skipped is no pass without a reuse
    assert fake_gh(ALL_GREEN, agg_env({**skipped, "master-health-onto-pr": {"result": "failure"}}, reuse="true"))[0].returncode != 0
    assert fake_gh(ALL_GREEN, agg_env({**skipped, "plan": {"result": "failure"}}, reuse="true"))[0].returncode != 0


def test_a_dispatch_posts_all_green_on_the_pr_head_with_the_real_conclusion(fake_gh):
    cur = {"repos/o/r/pulls/7": {"head": {"sha": "h1"}}}
    p, _, posts = fake_gh(ALL_GREEN, agg_env(NEEDS_OK, event="workflow_dispatch"), cur)
    assert p.returncode == 0, p.stderr
    (post,) = posts
    assert post[post.index("-X") + 1] == "POST" and "repos/o/r/check-runs" in post
    assert "name=all-green" in post and "head_sha=h1" in post and "conclusion=success" in post and "status=completed" in post
    p, _, posts = fake_gh(ALL_GREEN, agg_env({**NEEDS_OK, "impacted-tests": {"result": "failure"}}, event="workflow_dispatch"), cur)
    assert p.returncode != 0 and "conclusion=failure" in posts[0]


def test_a_dispatch_posts_nothing_when_the_head_moved_or_the_plan_failed(fake_gh):
    moved = {"repos/o/r/pulls/7": {"head": {"sha": "h2"}}}
    p, _, posts = fake_gh(ALL_GREEN, agg_env(NEEDS_OK, event="workflow_dispatch"), moved)
    assert p.returncode != 0 and posts == []
    p, _, posts = fake_gh(ALL_GREEN, agg_env({**NEEDS_OK, "plan": {"result": "failure"}}, event="workflow_dispatch", head=""), moved)
    assert p.returncode != 0 and posts == []


def test_a_failed_post_fails_the_dispatch_run(fake_gh):
    p, _, _ = fake_gh(ALL_GREEN, {**agg_env(NEEDS_OK, event="workflow_dispatch"), "FAKE_POST_FAILS": "1"},
                      {"repos/o/r/pulls/7": {"head": {"sha": "h1"}}})
    assert p.returncode != 0
