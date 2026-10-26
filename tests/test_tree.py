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


def test_red_at_passes_only_when_the_test_failed_for_a_real_reason_before_the_work(repo):
    first = sh(repo, "git", "rev-list", "--max-parents=0", "HEAD").stdout.strip()
    ok, why = core.red_at(repo, first, "python3 -m pytest -q -p no:cacheprovider tests/test_add.py")
    assert ok, why
    ok, why = core.red_at(repo, first, "python3 -m pytest -q -p no:cacheprovider tests/test_missing.py")
    assert not ok and "cannot run" in why
    ok, why = core.red_at(repo, first, "python3 -c 'pass'")
    assert not ok and "PASSES" in why


def test_the_harness_stays_green_after_the_work_is_done(approved):
    (approved / "src" / "demo" / "__init__.py").write_text("def add(a, b):\n    return a + b\n")
    sh(approved, "git", "commit", "-q", "-am", "fix add")
    status = {r["check"]: r["status"] for r in core.run_checks(approved, "demo") if "check" in r}
    assert status == {"h-test": "pass", "obj": "pass"}


def test_a_placeholder_commit_and_a_missing_proven_with_are_refused(repo):
    t = core.load_tree(repo, "demo")
    t.data["checks"][1]["run"] = "check-tree red-at <TESTS-COMMIT> -- pytest"
    del t.data["checks"][0]["proven_with"]
    problems = core.validate(t, repo)
    assert any("<TESTS-COMMIT>" in p for p in problems) and any("proven_with" in p for p in problems)


def test_the_owner_sheet_has_plain_words_only(approved):
    out = cli(approved, "table", "demo", "--owner").stdout
    assert "Done means: the acceptance test passes" in out and "Proven with:" in out and "Your answer" in out
    for jargon in ("h-test", "harness", "frozen", "sha256", "tests/test_add.py"):
        assert jargon not in out, jargon


def test_init_writes_a_full_config_and_a_usable_test_name(tmp_path):
    r = tmp_path / "fresh"
    r.mkdir()
    sh(r, "git", "init", "-q", "-b", "main")
    (r / "x").write_text("x")
    sh(r, "git", "add", "x")
    sh(r, "git", "commit", "-q", "-m", "x")
    assert cli(r, "init", "invoice-payments").returncode == 0
    cfg = json.loads((r / ".check-tree" / "config.json").read_text())
    assert cfg["remote"] is None and "guards" in cfg
    tree = (r / core.tree_path("invoice-payments")).read_text()
    assert "test_invoice_payments.py" in tree and "test_invoice-payments" not in tree


def test_the_card_is_shareable_and_shows_progress(approved):
    import datetime as _dt
    core.run_checks(approved, "demo")
    core.score(approved, "demo")  # the scoreboard records history each time it is read
    hist = approved / ".check-tree" / "state" / "demo" / "history.jsonl"
    old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=8)).isoformat(timespec="seconds")
    hist.write_text(json.dumps({"at": old, "commit": "x", "passing": 0, "total": 2}) + "\n" + hist.read_text())
    (approved / "src" / "demo" / "__init__.py").write_text("def add(a, b):\n    return a + b\n")
    sh(approved, "git", "commit", "-q", "-am", "fix add")
    core.run_checks(approved, "demo")
    out = cli(approved, "card", "demo").stdout
    assert "demo: 0 -> 2 of 2 checks passing in the last 7 days" in out, out
    assert "Goal reached." in out and "- the acceptance test passes" in out
    for internal in ("h-test", "frozen", "/Users/", ".check-tree"):
        assert internal not in out, internal


def test_red_at_can_pin_the_reason(repo):
    first = sh(repo, "git", "rev-list", "--max-parents=0", "HEAD").stdout.strip()
    cmd = "python3 -m pytest -q -p no:cacheprovider tests/test_add.py"
    ok, why = core.red_at(repo, first, cmd, expect=r"assert -1 == 5")
    assert ok, why
    ok, why = core.red_at(repo, first, cmd, expect=r"ConnectionError")
    assert not ok and "no error line matches" in why


def test_areas_must_own_real_files_and_the_objective_must_be_real(repo):
    t = core.load_tree(repo, "demo")
    t.data["areas"] = ["src/payments/**"]
    t.data["objective"] = "One sentence, in the owner's words: what is true when this program is done."
    problems = core.validate(t, repo)
    assert any("matches no file" in p for p in problems)
    assert any("starter text" in p for p in problems)


def test_open_questions_need_the_owners_answers(repo):
    path = repo / core.tree_path("demo")
    data = json.loads(path.read_text())
    data["open_for_the_owner"] = ["When do we plan the real email service?"]
    path.write_text(json.dumps(data))
    p = cli(repo, "approve", "demo", "--by", "owner", "--words", "yes")
    assert p.returncode != 0 and "--answer" in (p.stderr + p.stdout)
    p = cli(repo, "approve", "demo", "--by", "owner", "--words", "yes", "--answer", "In December, after launch.")
    assert p.returncode == 0, p.stderr
    rec = json.loads(path.read_text())["approved"]
    assert rec["answers"] == [{"question": "When do we plan the real email service?",
                               "answer": "In December, after launch."}]
    sheet = cli(repo, "table", "demo", "--owner").stdout
    assert "Before building:" in sheet and "an estimate" in sheet


def test_red_at_command_line_reads_expect_before_the_separator(repo):
    first = sh(repo, "git", "rev-list", "--max-parents=0", "HEAD").stdout.strip()
    test = ["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_add.py"]
    ok = cli(repo, "red-at", first, "--expect", "assert -1 == 5", "--", *test)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    wrong = cli(repo, "red-at", first, "--expect", "ConnectionError", "--", *test)
    assert wrong.returncode == 1 and "no error line matches" in wrong.stdout


def test_a_shell_usage_error_is_never_a_real_failure(repo):
    first = sh(repo, "git", "rev-list", "--max-parents=0", "HEAD").stdout.strip()
    ok, why = core.red_at(repo, first, "--expect MISSING -- python3 -c 'pass'")
    assert not ok and "cannot run" in why, why


def test_a_pin_that_only_matches_echoed_source_does_not_count(tmp_path):
    r = tmp_path / "pin"
    (r / "tests").mkdir(parents=True)
    # The test passes its MISSING guard and then fails on a wrong value. pytest echoes the source, which
    # contains the word MISSING; a pin on that echo must not pass.
    (r / "tests" / "test_x.py").write_text(
        "def test_x():\n    assert True, \"MISSING: thing\"\n    assert 1 + 1 == 3\n")
    sh(r, "git", "init", "-q", "-b", "main")
    sh(r, "git", "add", "tests/test_x.py")
    sh(r, "git", "commit", "-q", "-m", "t")
    cmd = "python3 -m pytest -q -p no:cacheprovider tests/test_x.py"
    ok, why = core.red_at(r, "HEAD", cmd, expect="MISSING")
    assert not ok, why
    ok, why = core.red_at(r, "HEAD", cmd, expect="AssertionError: MISSING")
    assert not ok, why
    ok, why = core.red_at(r, "HEAD", cmd, expect=r"assert \(1 \+ 1\) == 3")
    assert ok and "assert (1 + 1) == 3" in why, why


def test_the_owner_sheet_says_how_it_was_put_together(approved):
    sheet = cli(approved, "table", "demo", "--owner").stdout
    assert "How this was put together: not stated" in sheet


def test_nothing_counts_while_the_tree_is_not_approved(approved):
    core.run_checks(approved, "demo")
    assert core.score(approved, "demo").passing == 1
    (approved / "tests" / "test_add.py").write_text("def test_add():\n    assert True\n")
    sh(approved, "git", "commit", "-q", "-am", "weaken the test")
    core.run_checks(approved, "demo")
    s = core.score(approved, "demo")
    assert s.passing == 0 and not s.objective_passes and not s.approved
    assert "Nothing counts" in core.render_score(s, core.load_tree(approved, "demo", "main"))


def test_a_new_test_side_file_on_main_cancels_the_approval(approved):
    core.run_checks(approved, "demo")
    assert core.score(approved, "demo").passing == 1
    (approved / "tests" / "conftest.py").write_text("# changes what the tests run against\n")
    sh(approved, "git", "add", "tests/conftest.py")
    sh(approved, "git", "commit", "-q", "-m", "a helper straight onto main")
    core.run_checks(approved, "demo")
    s = core.score(approved, "demo")
    assert not s.approved and s.passing == 0
    assert any("tests/conftest.py" in w for w in s.unapproved_why)
    assert "uncounted" in core.render_score(s, core.load_tree(approved, "demo", "main"))


def test_discard_removes_a_rejected_builders_work(approved, tmp_path):
    from test_loop_hooks import bind
    wt, _ = bind(approved, tmp_path)
    (wt / "notes.txt").write_text("rejected work\n")
    sh(wt, "git", "add", "notes.txt")
    sh(wt, "git", "commit", "-q", "-m", "rejected")
    out = cli(approved, "discard", "demo", "obj").stdout
    assert "discarded" in out and not wt.exists()
