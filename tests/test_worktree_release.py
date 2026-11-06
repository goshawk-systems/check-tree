"""Builder worktrees: where they live and when one may be released (PROC-159, the pipeline lead's rules of 2026-11-06).

  * a worktree is a SIBLING of the clone (`<clone>.wt/<program>__<check>__<sfx>`), never inside it;
  * `clean` and `discard` release a worktree ONLY when `git status --porcelain` is empty and its HEAD equals
    `origin/<branch>` (that ref fetched first); never `git worktree remove --force`, never `branch -D`;
  * a worktree that is kept is reported by name;
  * the package never pushes;
  * a dispatch to a read-only agent type gets no brief and no worktree.
"""
import importlib.util
import pathlib
import re
import sys

import pytest

from checktree import core
from conftest import HOOKS, PKG, cli, decision, hook, sh
from test_loop_hooks import bind, dispatch


def add_origin(repo, tmp_path):
    """A bare `origin` with main on it (the clone's config says `remote: null`; the release rule still asks origin)."""
    origin = tmp_path / "origin.git"
    sh(tmp_path, "git", "init", "-q", "--bare", str(origin))
    sh(repo, "git", "remote", "add", "origin", str(origin))
    sh(repo, "git", "push", "-q", "origin", "main")
    return origin


def push_by_ref(repo, branch):
    """The one way work reaches origin: from the base clone, by ref."""
    sh(repo, "git", "-C", str(repo), "push", "-q", "origin", branch)


def branch_of(wt):
    return sh(wt, "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def branch_exists(repo, branch):
    return sh(repo, "git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).returncode == 0


def commit_in(wt, name="notes.txt", text="work\n"):
    (wt / name).write_text(text)
    sh(wt, "git", "add", name)
    sh(wt, "git", "commit", "-q", "-m", f"add {name}")


def clean(repo):
    return cli(repo, "clean", "demo", "--hours", "0").stdout


# ── where a worktree lives ──

def test_a_worktree_is_a_sibling_of_the_clone_never_inside_it(approved, tmp_path):
    wt, _ = bind(approved, tmp_path)
    root = approved.resolve()
    assert not wt.resolve().is_relative_to(root), f"{wt} is inside the clone {root}"
    assert wt.resolve().parent == root.parent / f"{root.name}.wt"
    assert re.fullmatch(r"demo__obj__[0-9a-f]{6}", wt.name)
    assert not (approved / ".check-tree" / "worktrees").exists()
    # a whole-tree scanner over the clone sees none of the builder's copy
    assert not [p for p in approved.rglob("__init__.py") if ".wt" in p.parts]
    assert sh(approved, "git", "status", "--porcelain").stdout.strip() == ""


def test_the_scope_guard_still_recognises_a_sibling_worktree(approved, tmp_path):
    wt, _ = bind(approved, tmp_path)
    assert core.worktrees_dir(approved).resolve() == wt.resolve().parent
    assert hook("check_scope.py", {"hook_event_name": "PreToolUse", "tool_name": "Edit", "cwd": str(approved),
                                   "tool_input": {"file_path": str(wt / "src" / "demo" / "__init__.py")}},
                approved) is None


# ── the release rule ──

def test_a_worktree_with_an_uncommitted_file_is_kept(approved, tmp_path):
    add_origin(approved, tmp_path)
    wt, _ = bind(approved, tmp_path)
    branch = branch_of(wt)
    push_by_ref(approved, branch)  # at origin: only the uncommitted file stands in the way
    (wt / "scratch.txt").write_text("not committed\n")
    report = clean(approved)
    assert f"kept {wt.name}" in report and "uncommitted" in report
    assert wt.is_dir() and (wt / "scratch.txt").is_file() and branch_exists(approved, branch)


def test_a_worktree_with_a_local_commit_not_on_origin_is_kept(approved, tmp_path):
    add_origin(approved, tmp_path)
    wt, _ = bind(approved, tmp_path)
    branch = branch_of(wt)
    push_by_ref(approved, branch)
    commit_in(wt)  # committed, clean, but one commit ahead of origin/<branch>
    sh(approved, "git", "branch", "-f", "check-tree/demo", branch)  # even landed locally
    report = clean(approved)
    assert f"kept {wt.name}" in report
    assert wt.is_dir() and branch_exists(approved, branch)


def test_a_landed_worktree_whose_branch_never_reached_origin_is_kept(approved, tmp_path):
    """Landed on the local integration branch is not on origin: the old rule force-removed it and deleted the branch."""
    add_origin(approved, tmp_path)
    wt, _ = bind(approved, tmp_path)
    branch = branch_of(wt)
    (wt / "src" / "demo" / "__init__.py").write_text("def add(a, b):\n    return a + b\n")
    sh(wt, "git", "commit", "-q", "-am", "fix add")
    sh(approved, "git", "branch", "-f", "check-tree/demo", branch)  # the integration branch now holds the work
    report = clean(approved)
    assert f"kept {wt.name}" in report and "origin" in report
    assert wt.is_dir() and branch_exists(approved, branch)
    assert sh(wt, "git", "log", "-1", "--format=%s").stdout.strip() == "fix add"


def test_an_idle_worktree_that_was_never_pushed_is_kept(approved, tmp_path):
    add_origin(approved, tmp_path)
    wt, _ = bind(approved, tmp_path)
    report = clean(approved)
    assert f"kept {wt.name}" in report and wt.is_dir()


def test_with_no_origin_at_all_nothing_is_released(approved, tmp_path):
    wt, _ = bind(approved, tmp_path)
    report = clean(approved)
    assert f"kept {wt.name}" in report and wt.is_dir() and branch_exists(approved, branch_of(wt))


def test_a_clean_worktree_at_origin_is_released(approved, tmp_path):
    add_origin(approved, tmp_path)
    wt, _ = bind(approved, tmp_path)
    branch = branch_of(wt)
    commit_in(wt)
    push_by_ref(approved, branch)
    sh(approved, "git", "branch", "-f", "check-tree/demo", branch)  # the work landed: nothing else holds it
    report = clean(approved)
    assert f"removed {wt.name}" in report
    assert not wt.exists() and not branch_exists(approved, branch)
    assert sh(approved, "git", "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}").returncode == 0
    assert not wt.parent.exists()  # the emptied `<clone>.wt` folder goes too


def test_the_branch_is_fetched_before_it_is_compared(approved, tmp_path):
    """The work reached origin by a path that left the base's remote-tracking ref missing: release asks origin."""
    origin = add_origin(approved, tmp_path)
    wt, _ = bind(approved, tmp_path)
    branch = branch_of(wt)
    commit_in(wt)
    sh(approved, "git", "-C", str(approved), "push", "-q", str(origin), branch)  # by URL
    # Some git versions still write the tracking ref for a URL that matches a configured remote (CI's
    # did, 2026-11-06), so the missing ref is FORCED rather than assumed: the premise holds on every git.
    sh(approved, "git", "update-ref", "-d", f"refs/remotes/origin/{branch}", check=False)
    assert sh(approved, "git", "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}",
              check=False).returncode != 0
    sh(approved, "git", "branch", "-f", "check-tree/demo", branch)
    assert f"removed {wt.name}" in clean(approved)
    assert not wt.exists()


def test_clean_and_discard_never_force_and_never_delete_a_branch_with_dash_D(approved, tmp_path, monkeypatch):
    add_origin(approved, tmp_path)
    kept, _ = bind(approved, tmp_path)  # unpushed
    released, _ = bind(approved, tmp_path)
    rb = branch_of(released)
    commit_in(released)
    push_by_ref(approved, rb)
    sh(approved, "git", "branch", "-f", "check-tree/demo", rb)
    calls = []
    real = core.git

    def spy(cwd, *args, **kw):
        calls.append(args)
        return real(cwd, *args, **kw)

    monkeypatch.setattr(core, "git", spy)
    core.clean_worktrees(approved, "demo", 0)
    core.discard_worktrees(approved, "demo", "obj")
    assert any(a[:2] == ("worktree", "remove") for a in calls)  # the released one really went through git
    for a in calls:
        assert "--force" not in a and "-D" not in a, a
    assert kept.is_dir()  # the unpushed one survived both


# ── discard follows the same rule ──

def test_discard_keeps_a_worktree_whose_work_is_not_on_origin_and_names_it(approved, tmp_path):
    add_origin(approved, tmp_path)
    wt, _ = bind(approved, tmp_path)
    branch = branch_of(wt)
    commit_in(wt, text="rejected, but never pushed\n")
    out = cli(approved, "discard", "demo", "obj").stdout
    assert f"kept {wt.name}" in out and "discarded" not in out
    assert wt.is_dir() and branch_exists(approved, branch)


def test_discard_keeps_uncommitted_work_even_at_origin(approved, tmp_path):
    add_origin(approved, tmp_path)
    wt, _ = bind(approved, tmp_path)
    push_by_ref(approved, branch_of(wt))
    (wt / "scratch.txt").write_text("x\n")
    out = cli(approved, "discard", "demo", "obj").stdout
    assert f"kept {wt.name}" in out and wt.is_dir()


def test_discard_releases_a_worktree_that_is_at_origin(approved, tmp_path):
    add_origin(approved, tmp_path)
    wt, _ = bind(approved, tmp_path)
    branch = branch_of(wt)
    commit_in(wt, text="rejected\n")
    push_by_ref(approved, branch)
    out = cli(approved, "discard", "demo", "obj").stdout
    assert f"discarded {wt.name}" in out and not wt.exists() and not branch_exists(approved, branch)


# ── the package never pushes ──

def test_nothing_in_the_package_pushes(approved, tmp_path, monkeypatch):
    """The package moves a builder's work by merge and update-ref on local branches, and only reads origin. The push is
    the lead's, by ref from the base clone (`git -C <base> push origin <branch>`); a builder never pushes from a worktree."""
    for src in list((PKG / "checktree").glob("*.py")) + list(HOOKS.glob("*.py")):
        assert not re.search(r"""["']push["']""", src.read_text()), f"{src} runs a git push"
    add_origin(approved, tmp_path)
    calls = []
    real = core.git

    def spy(cwd, *args, **kw):
        calls.append(args)
        return real(cwd, *args, **kw)

    monkeypatch.setattr(core, "git", spy)
    wt, _ = bind(approved, tmp_path)
    commit_in(wt)
    core.clean_worktrees(approved, "demo", 0)
    core.discard_worktrees(approved, "demo", "obj")
    assert calls and not [a for a in calls if "push" in a]


def test_the_brief_tells_a_builder_to_push_by_ref_from_the_base_clone(approved, tmp_path):
    core.run_checks(approved, "demo")
    out = dispatch(approved, "Again.\nCheck: demo/obj")
    prompt = out["hookSpecificOutput"]["updatedInput"]["prompt"]
    branch = re.search(r"(?m)^Branch: (\S+)", prompt).group(1)
    assert f"git -C {approved} push origin {branch}" in prompt
    assert "never from inside the worktree" in prompt


# ── read-only agent types ──

@pytest.mark.parametrize("agent", ["loop-evaluator", "check-evaluator", "Explore", "pr-antagonist", "Plan"])
def test_a_read_only_agent_gets_no_brief_and_no_worktree(approved, agent):
    core.run_checks(approved, "demo")
    prompt = "Attack the plan.\nCheck: demo/obj"
    out = dispatch(approved, prompt, tool_input={"prompt": prompt, "subagent_type": agent})
    assert decision(out) != "deny", out
    assert (out or {}).get("hookSpecificOutput", {}).get("updatedInput") is None
    assert not core.worktrees_dir(approved).exists()
    assert not (core.state_dir(approved, "demo") / "dispatches.jsonl").exists()
    assert sh(approved, "git", "rev-parse", "--verify", "-q", "check-tree/demo", check=False).returncode != 0


def test_a_builder_type_is_still_bound(approved):
    core.run_checks(approved, "demo")
    prompt = "Fix it.\nCheck: demo/obj"
    for agent in ("builder", "general-purpose", "test-author"):
        out = dispatch(approved, prompt, tool_input={"prompt": prompt, "subagent_type": agent})
        assert decision(out) == "allow", (agent, out)
        assert pathlib.Path(re.search(r"(?m)^Worktree: (.+)$",
                                      out["hookSpecificOutput"]["updatedInput"]["prompt"]).group(1)).is_dir()


def test_the_package_read_only_set_covers_the_loops_set():
    loop = PKG.parent / "loop" / "looplib.py"
    if not loop.is_file():
        pytest.skip("no execution loop beside the package")
    spec = importlib.util.spec_from_file_location("looplib_for_test", loop)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.path.insert(0, str(HOOKS))
    try:
        import _common as C
    finally:
        sys.path.remove(str(HOOKS))
    assert set(mod.READ_ONLY_TYPES) <= set(C.READ_ONLY_TYPES)
    assert {"check-evaluator", "loop-evaluator", "Explore"} <= set(C.READ_ONLY_TYPES)
