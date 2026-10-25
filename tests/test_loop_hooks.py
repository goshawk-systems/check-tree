"""The three loop hooks, driven the way Claude Code drives them: JSON on stdin, a decision on stdout."""
import pathlib
import re

from checktree import core
from conftest import cli, decision, hook, sh, write_transcript


def dispatch(repo, prompt, **extra):
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": str(repo), "session_id": "s1",
               "tool_input": {"prompt": prompt, "description": "build", "subagent_type": "general-purpose"}}
    payload.update(extra)
    return hook("check_brief.py", payload, repo)


# ── check_brief ──

def test_a_dispatch_without_a_check_line_is_left_alone(approved):
    assert dispatch(approved, "please tidy the README") is None


def test_an_unapproved_tree_cannot_be_dispatched(repo):
    out = dispatch(repo, "Check: demo/obj")
    assert decision(out) == "deny" and "not approved" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_check_that_waits_on_another_is_refused(approved):
    core.run_checks(approved, "demo", ["h-test"])  # only score the harness... obj not ready until it passes
    (approved / "tests" / "test_add.py").unlink()
    sh(approved, "git", "commit", "-q", "-am", "drop the test")
    core.run_checks(approved, "demo")
    out = dispatch(approved, "Check: demo/obj")
    assert decision(out) == "deny"


def test_a_ready_check_gets_its_brief_and_its_own_worktree(approved):
    core.run_checks(approved, "demo")
    out = dispatch(approved, "Fix the adder.\nCheck: demo/obj")
    assert decision(out) == "allow"
    new = out["hookSpecificOutput"]["updatedInput"]
    assert new["description"] == "build" and new["subagent_type"] == "general-purpose"
    prompt = new["prompt"]
    assert prompt.startswith("Fix the adder.") and "Your ONE check: obj" in prompt
    assert "Frozen (you may NOT edit" in prompt and "make add() correct" in prompt
    wt = pathlib.Path(re.search(r"(?m)^Worktree: (.+)$", prompt).group(1))
    assert wt.is_dir() and (wt / "src" / "demo" / "__init__.py").is_file()
    assert sh(approved, "git", "rev-parse", "--verify", "check-tree/demo").returncode == 0


def test_a_passing_check_is_not_dispatched_again(approved):
    core.run_checks(approved, "demo")
    out = dispatch(approved, "Check: demo/h-test")
    assert decision(out) == "deny" and "already passes" in out["hookSpecificOutput"]["permissionDecisionReason"]


# ── check_stop ──

def bind(approved, tmp_path):
    core.run_checks(approved, "demo")
    out = dispatch(approved, "Fix the adder.\nCheck: demo/obj")
    prompt = out["hookSpecificOutput"]["updatedInput"]["prompt"]
    wt = pathlib.Path(re.search(r"(?m)^Worktree: (.+)$", prompt).group(1))
    transcript = write_transcript(tmp_path / "agent-a1.jsonl", prompt)
    return wt, transcript


def stop(approved, transcript, agent="a1"):
    return hook("check_stop.py", {"hook_event_name": "SubagentStop", "agent_id": agent,
                                  "agent_transcript_path": transcript, "cwd": str(approved),
                                  "stop_hook_active": False}, approved)


def test_a_builder_cannot_stop_while_its_check_fails(approved, tmp_path):
    wt, transcript = bind(approved, tmp_path)
    out = stop(approved, transcript)
    assert out["decision"] == "block" and "still fails (attempt 1 of 2)" in out["reason"]
    out = stop(approved, transcript)
    assert out["decision"] == "block" and "attempt 2 of 2" in out["reason"]
    assert stop(approved, transcript) is None  # third failing stop may return; it is logged as failed
    log = (approved / ".check-tree/state/demo/progress.jsonl").read_text()
    assert "returned FAILING" in log


def test_uncommitted_work_is_sent_back(approved, tmp_path):
    wt, transcript = bind(approved, tmp_path)
    (wt / "src" / "demo" / "__init__.py").write_text("def add(a, b):\n    return a + b\n")
    out = stop(approved, transcript)
    assert out["decision"] == "block" and "uncommitted" in out["reason"]


def test_changing_a_frozen_test_is_sent_back(approved, tmp_path):
    wt, transcript = bind(approved, tmp_path)
    (wt / "tests" / "test_add.py").write_text("def test_add():\n    assert True\n")
    sh(wt, "git", "commit", "-q", "-am", "make it pass the easy way")
    out = stop(approved, transcript)
    assert out["decision"] == "block" and "frozen" in out["reason"]


def test_a_passing_builder_may_stop_and_is_logged_ready_to_land(approved, tmp_path):
    wt, transcript = bind(approved, tmp_path)
    (wt / "src" / "demo" / "__init__.py").write_text("def add(a, b):\n    return a + b\n")
    sh(wt, "git", "commit", "-q", "-am", "fix add")
    assert stop(approved, transcript) is None
    assert "ready to land" in (approved / ".check-tree/state/demo/progress.jsonl").read_text()
    # land it, then a fresh run at the integration branch... and on main after the milestone merges
    p = cli(approved, "land", "demo", "obj")
    assert p.returncode == 0, p.stderr + p.stdout
    sh(approved, "git", "merge", "-q", "--no-edit", "check-tree/demo")
    core.run_checks(approved, "demo")
    s = core.score(approved, "demo")
    assert s.objective_passes and s.passing == 2


def test_a_stop_without_a_binding_is_ignored(approved, tmp_path):
    t = write_transcript(tmp_path / "agent-x.jsonl", "just look around")
    assert stop(approved, t, "x") is None


# ── check_scope ──

def edit(repo, path):
    return hook("check_scope.py", {"hook_event_name": "PreToolUse", "tool_name": "Edit", "cwd": str(repo),
                                   "tool_input": {"file_path": str(path)}}, repo)


def test_a_programs_file_cannot_be_edited_in_the_main_checkout(approved):
    out = edit(approved, approved / "src" / "demo" / "__init__.py")
    assert decision(out) == "deny" and "Check: demo/<id>" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_builder_edits_its_programs_files_in_its_worktree(approved, tmp_path):
    wt, _ = bind(approved, tmp_path)
    assert edit(approved, wt / "src" / "demo" / "__init__.py") is None


def test_files_no_program_owns_are_untouched(approved):
    assert edit(approved, approved / "README.md") is None


def test_an_unapproved_programs_files_are_not_locked(repo):
    assert edit(repo, repo / "src" / "demo" / "__init__.py") is None
