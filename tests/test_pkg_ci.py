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
                       "event": {"pull_request": {"draft": draft, "number": 7}}}}


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
    assert jobs["all-green"]["name"] == "all-green"
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
