"""The tree: validation, approval pinning, running, and the computed scoreboard."""
import json

from checktree import core
from conftest import TREE, cli, sh


def test_a_valid_tree_validates(repo):
    assert core.validate(core.load_tree(repo, "demo"), repo) == []


def test_a_check_that_cannot_fail_is_refused(repo):
    t = core.load_tree(repo, "demo")
    t.data["checks"][1]["run"] = "true"
    assert any("cannot fail" in p for p in core.validate(t, repo))


def test_exactly_one_objective_and_no_needs_loop(repo):
    t = core.load_tree(repo, "demo")
    t.data["checks"][1]["kind"] = "objective"
    t.data["checks"][1]["needs"] = ["obj"]
    problems = core.validate(t, repo)
    assert any("exactly one `objective`" in p for p in problems)
    assert any("loop" in p for p in problems)


def test_missing_frozen_file_and_appetite_are_reported(repo):
    t = core.load_tree(repo, "demo")
    t.data["checks"][0]["frozen"] = ["tests/nope.py"]
    t.data["checks"][0]["appetite_hours"] = 0
    problems = core.validate(t, repo)
    assert any("tests/nope.py" in p for p in problems)
    assert any("appetite_hours" in p for p in problems)


def test_an_unapproved_tree_says_so(repo):
    ok, why = core.approval_state(core.load_tree(repo, "demo", "main"), repo, "main")
    assert not ok and "never approved" in why[0]


def test_approval_pins_the_tree_and_its_frozen_files(approved):
    t = core.load_tree(approved, "demo", "main")
    assert core.approval_state(t, approved, "main") == (True, [])
    # a changed frozen file unapproves
    (approved / "tests" / "test_add.py").write_text("def test_add():\n    assert True\n")
    sh(approved, "git", "commit", "-q", "-am", "weaken the test")
    ok, why = core.approval_state(core.load_tree(approved, "demo", "main"), approved, "main")
    assert not ok and any("tests/test_add.py changed" in w for w in why)


def test_editing_the_tree_unapproves_it(approved):
    path = approved / core.tree_path("demo")
    data = json.loads(path.read_text())
    data["checks"][0]["appetite_hours"] = 400
    path.write_text(json.dumps(data))
    sh(approved, "git", "commit", "-q", "-am", "quietly extend the budget")
    ok, why = core.approval_state(core.load_tree(approved, "demo", "main"), approved, "main")
    assert not ok and "tree changed" in why[0]


def test_approve_refuses_without_the_owners_words(repo):
    p = cli(repo, "approve", "demo", "--by", "owner", "--words", "  ")
    assert p.returncode != 0 and "owner's own words" in (p.stderr + p.stdout)


def test_run_scores_at_the_head_with_needs_order(approved):
    results = core.run_checks(approved, "demo")
    status = {r["check"]: r["status"] for r in results if "check" in r}
    assert status == {"h-test": "pass", "obj": "fail"}
    s = core.score(approved, "demo")
    assert (s.passing, s.total) == (1, 2)
    assert s.ready == ["obj"] and not s.objective_passes
    head = sh(approved, "git", "rev-parse", "HEAD").stdout.strip()
    assert s.commit == head


def test_a_new_commit_makes_old_results_stale(approved):
    core.run_checks(approved, "demo")
    (approved / "README.md").write_text("changed\n")
    sh(approved, "git", "commit", "-q", "-am", "unrelated")
    s = core.score(approved, "demo")
    assert s.passing == 0 and set(s.status.values()) == {"not run"}


def test_a_blocked_check_is_not_run(approved):
    (approved / "tests" / "test_add.py").unlink()
    sh(approved, "git", "commit", "-q", "-am", "drop the test")
    results = {r["check"]: r["status"] for r in core.run_checks(approved, "demo") if "check" in r}
    assert results == {"h-test": "fail", "obj": "blocked"}


def test_check_commands_do_not_see_credentials(approved, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("MY_SERVICE_PASSWORD", "x")
    env = core.check_env(approved, "demo", approved)
    assert not {"AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "MY_SERVICE_PASSWORD"} & set(env)
    assert env["CHECK_TREE_PROGRAM"] == "demo"


def test_tree_constant_is_the_fixture():
    assert TREE["program"] == "demo"


def test_approval_needs_a_validated_plan_or_says_it_skipped_one(repo):
    import json as _j
    path = repo / core.tree_path("demo")
    data = _j.loads(path.read_text())
    del data["validation"]
    path.write_text(_j.dumps(data))
    p = cli(repo, "approve", "demo", "--by", "owner", "--words", "yes")
    assert p.returncode != 0 and "Validate the plan first" in (p.stderr + p.stdout)
    assert cli(repo, "approve", "demo", "--by", "owner", "--words", "yes", "--without-validation").returncode == 0
    sh(repo, "git", "commit", "-q", "-am", "approved without validation")
    t = core.load_tree(repo, "demo", "main")
    assert "approved WITHOUT a validated plan" in core.render_score(core.score(repo, "demo"), t)


def test_a_named_validation_file_must_exist(repo):
    t = core.load_tree(repo, "demo")
    t.data["validation"] = "docs/plans/missing.md"
    assert any("missing.md" in p for p in core.validate(t, repo))
