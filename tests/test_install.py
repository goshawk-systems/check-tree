"""check-tree init-repo and workspace new / sweep: shown installing, skipping, forcing, fencing and refusing to lose work."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

from conftest import CLI, PKG, sh

TEMPLATE = PKG / "templates" / "repo"
PLACEHOLDERS = ("repo_name", "default_branch", "product_prefixes", "test_command", "lint_command")
REPO_JSON_KEYS = ["repo_name", "default_branch", "product_prefixes", "process_prefixes", "freeze_allowed_items",
                  "max_pr_files", "test_command", "lint_command", "prod", "shared_checkouts", "fleet",
                  "work_item_pattern"]


def template_rel_files():
    return sorted(p.relative_to(TEMPLATE).as_posix() for p in TEMPLATE.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc"))


def init(target, *args):
    return sh(PKG, *CLI, "init-repo", "--target", str(target), *args, check=True)


def test_every_template_file_lands_with_placeholders_filled(tmp_path):
    target = tmp_path / "shop"
    target.mkdir()
    (target / ".check-tree").mkdir()
    (target / ".check-tree" / "repo.json").write_text(json.dumps({
        "repo_name": "shop", "default_branch": "trunk", "product_prefixes": ["SHP", "WEB"],
        "test_command": "make test", "lint_command": "make lint"}))
    out = init(target).stdout
    for rel in template_rel_files():
        assert (target / rel).is_file(), f"{rel} did not land"
    assert (target / ".githooks" / "pre-push").stat().st_mode & 0o111, "pre-push lost its executable bit"
    for rel in template_rel_files():
        text = (target / rel).read_text(errors="ignore")
        for name in PLACEHOLDERS:
            assert "{{" + name + "}}" not in text, f"{rel} still has {{{{{name}}}}}"
    assert "# Working in shop" in (target / "CLAUDE.md").read_text()
    assert "make lint" in (target / ".githooks" / "pre-push").read_text()
    assert "trunk" in (target / ".githooks" / "pre-push").read_text()
    # the checklist of what only the owner can do
    for phrase in ("Mergify", "all-green", "infrastructure module"):
        assert phrase in out


def test_a_missing_repo_json_is_created_with_the_agreed_keys_and_defaults(tmp_path):
    target = tmp_path / "newrepo"
    target.mkdir()
    init(target)
    data = json.loads((target / ".check-tree" / "repo.json").read_text())
    assert list(data) == REPO_JSON_KEYS
    assert data["repo_name"] == "newrepo"
    assert data["default_branch"] == "main" and data["max_pr_files"] == 50
    assert data["process_prefixes"] == ["PROC"] and data["product_prefixes"] == []
    assert data["test_command"] == "python3 -m pytest -q" and data["lint_command"] == "ruff check ."
    assert data["prod"] == {"deploy_workflow_patterns": ["deploy-prod*", "*production*"], "db_patterns": [],
                            "aws_profiles": [], "aws_accounts": []}
    assert data["fleet"] == {"monthly_budget_usd": 0, "classes": {}}
    assert data["work_item_pattern"] == "[A-Z]{2,6}-\\d+[a-z]?"


def test_a_second_run_skips_everything_and_changes_nothing(tmp_path):
    target = tmp_path / "r"
    target.mkdir()
    init(target)
    before = {rel: (target / rel).read_bytes() for rel in template_rel_files()}
    out = init(target).stdout
    assert "0 created, 0 overwritten" in out
    for rel in template_rel_files():
        assert (target / rel).read_bytes() == before[rel]
        if rel != ".check-tree/repo.json":
            assert f"skipped (already exists; use --force to overwrite): {rel}" in out


def test_an_existing_file_is_never_overwritten_without_force(tmp_path):
    target = tmp_path / "r"
    target.mkdir()
    (target / "CLAUDE.md").write_text("mine\n")
    out = init(target).stdout
    assert (target / "CLAUDE.md").read_text() == "mine\n"
    assert "skipped (already exists; use --force to overwrite): CLAUDE.md" in out


def test_force_overwrites_files_but_never_the_settings(tmp_path):
    target = tmp_path / "r"
    target.mkdir()
    init(target)
    settings = target / ".check-tree" / "repo.json"
    data = json.loads(settings.read_text())
    data["repo_name"] = "edited"
    settings.write_text(json.dumps(data))
    (target / "CLAUDE.md").write_text("mine\n")
    out = init(target, "--force").stdout
    assert "# Working in edited" in (target / "CLAUDE.md").read_text()
    assert json.loads(settings.read_text())["repo_name"] == "edited"
    assert "overwritten (--force): CLAUDE.md" in out


def test_the_template_claude_md_obeys_its_own_size_test(tmp_path):
    target = tmp_path / "r"
    target.mkdir()
    init(target)
    assert (target / "CLAUDE.md").read_text().count("\n") + 1 <= 100
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_claude_md_size.py"],
                       cwd=str(target), capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr


# ── the test fence ──

def run_pytest(target, *args, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "ALLOW_"))}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
                          cwd=str(target), capture_output=True, text=True, env=env)


FENCE_TESTS = '''
import os, socket, pytest

def test_a_socket_connect_to_a_real_address_is_refused():
    s = socket.socket()
    try:
        with pytest.raises(OSError, match="test fence"):
            s.connect(("203.0.113.1", 80))
    finally:
        s.close()

def test_a_name_lookup_is_refused():
    with pytest.raises(OSError, match="test fence"):
        socket.getaddrinfo("example.com", 80)

def test_cloud_credentials_are_not_visible():
    os.environ.get("AWS_ACCESS_KEY_ID") is None or pytest.fail("credential leaked")
    assert os.environ["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"

def test_loopback_still_works():
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    c = socket.socket(); c.connect(srv.getsockname()); c.close(); srv.close()

@pytest.mark.live
def test_live_is_skipped_without_the_variable():
    raise AssertionError("must not run")
'''


def test_the_fence_refuses_a_socket_connect_and_hides_credentials(tmp_path):
    target = tmp_path / "r"
    target.mkdir()
    init(target)
    (target / "tests").mkdir(exist_ok=True)
    (target / "tests" / "test_fence_demo.py").write_text(FENCE_TESTS)
    p = run_pytest(target, "tests/test_fence_demo.py", env_extra={"AWS_ACCESS_KEY_ID": "AKIAEXAMPLE"})
    assert p.returncode == 0, p.stdout + p.stderr
    assert "4 passed, 1 skipped" in p.stdout
    assert "test fence: refused real-system access" in p.stdout


def test_the_fence_refuses_a_database_connection_to_a_real_host(tmp_path):
    target = tmp_path / "r"
    target.mkdir()
    init(target)
    stub = target / "psycopg2.py"
    stub.write_text("def connect(*a, **k):\n    return 'connected'\n")
    (target / "tests" / "test_db.py").write_text(
        "import psycopg2, pytest\n\n"
        "def test_real_host_refused():\n"
        "    with pytest.raises(OSError, match='test fence'):\n"
        "        psycopg2.connect(host='db.example.com', dbname='x')\n\n"
        "def test_local_allowed():\n"
        "    assert psycopg2.connect('host=localhost port=55432 dbname=t') == 'connected'\n")
    p = run_pytest(target, "tests/test_db.py", env_extra={"PYTHONPATH": str(target)})
    assert p.returncode == 0, p.stdout + p.stderr


def test_a_live_test_runs_only_when_the_person_opts_in(tmp_path):
    target = tmp_path / "r"
    target.mkdir()
    init(target)
    (target / "tests" / "test_live.py").write_text(
        "import pytest\n\n@pytest.mark.live\ndef test_reaches_out():\n    assert True\n")
    assert "1 skipped" in run_pytest(target, "tests/test_live.py").stdout
    assert "1 passed" in run_pytest(target, "tests/test_live.py", env_extra={"ALLOW_LIVE_TESTS": "1"}).stdout


# ── workspaces ──

def make_origin(tmp_path):
    """A bare origin with one commit on main, and a working repo (cwd for the commands) cloned from it."""
    bare = tmp_path / "origin.git"
    sh(tmp_path, "git", "init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    sh(tmp_path, "git", "clone", "-q", str(bare), str(seed))
    (seed / "README.md").write_text("hi\n")
    sh(seed, "git", "add", "README.md")
    sh(seed, "git", "commit", "-q", "-m", "first")
    sh(seed, "git", "branch", "-M", "main")
    sh(seed, "git", "push", "-q", "origin", "main")
    return bare, seed


def ws(seed, root, *args):
    return sh(seed, *CLI, "workspace", *args, "--root", str(root))


def test_workspace_new_clones_origin_and_turns_on_the_hooks(tmp_path):
    bare, seed = make_origin(tmp_path)
    root = tmp_path / "bench"
    p = ws(seed, root, "new", "alpha", "--branch", "feat/alpha")
    dest = root / "ws-alpha"
    assert dest.is_dir() and (dest / "README.md").is_file()
    assert sh(dest, "git", "config", "core.hooksPath").stdout.strip() == ".githooks"
    assert sh(dest, "git", "branch", "--show-current").stdout.strip() == "feat/alpha"
    assert sh(dest, "git", "remote", "get-url", "origin").stdout.strip() == str(bare)
    assert "ws-alpha" in p.stdout
    again = sh(seed, *CLI, "workspace", "new", "alpha", "--root", str(root), check=False)
    assert again.returncode != 0 and "already exists" in again.stderr + again.stdout


def test_workspace_new_refuses_an_unsafe_name(tmp_path):
    _, seed = make_origin(tmp_path)
    p = sh(seed, *CLI, "workspace", "new", "../evil", "--root", str(tmp_path / "bench"), check=False)
    assert p.returncode != 0 and not (tmp_path / "evil").exists()


def test_sweep_is_a_dry_run_and_removes_only_merged_pushed_clean_clones(tmp_path):
    _, seed = make_origin(tmp_path)
    root = tmp_path / "bench"
    for name in ("clean", "dirty", "unpushed", "stashed", "unmerged"):
        ws(seed, root, "new", name, "--branch", f"feat/{name}")
    # clean: its branch is at origin/main already, nothing else
    (root / "ws-dirty" / "scratch.txt").write_text("uncommitted\n")
    (root / "ws-unpushed" / "work.txt").write_text("w\n")
    sh(root / "ws-unpushed", "git", "add", "work.txt")
    sh(root / "ws-unpushed", "git", "commit", "-q", "-m", "local only")
    sh(root / "ws-unpushed", "git", "checkout", "-q", "-b", "feat/other", "origin/main")  # the commit is on a no-remote branch now
    (root / "ws-stashed" / "README.md").write_text("changed\n")
    sh(root / "ws-stashed", "git", "stash", "-q")
    (root / "ws-unmerged" / "pushed.txt").write_text("p\n")
    sh(root / "ws-unmerged", "git", "add", "pushed.txt")
    sh(root / "ws-unmerged", "git", "commit", "-q", "-m", "pushed, not merged")
    sh(root / "ws-unmerged", "git", "push", "-q", "origin", "feat/unmerged")

    dry = ws(seed, root, "sweep").stdout
    assert "would remove" in dry and "ws-clean" in dry
    for name in ("dirty", "unpushed", "stashed", "unmerged"):
        assert f"keeping {root}/ws-{name}" in dry
    assert all((root / f"ws-{n}").exists() for n in ("clean", "dirty", "unpushed", "stashed", "unmerged"))

    applied = ws(seed, root, "sweep", "--apply").stdout
    assert f"removed {root}/ws-clean" in applied
    assert not (root / "ws-clean").exists()
    for name in ("dirty", "unpushed", "stashed", "unmerged"):
        assert (root / f"ws-{name}").exists(), f"ws-{name} was removed"
    assert (root / "ws-unpushed" / "work.txt").is_file() is False  # that branch is elsewhere; the commit survives:
    assert "local only" in sh(root / "ws-unpushed", "git", "log", "--all", "--format=%s").stdout


def test_sweep_never_removes_a_clone_with_unpushed_work_on_its_current_branch(tmp_path):
    _, seed = make_origin(tmp_path)
    root = tmp_path / "bench"
    ws(seed, root, "new", "busy", "--branch", "feat/busy")
    clone = root / "ws-busy"
    (clone / "a.txt").write_text("a\n")
    sh(clone, "git", "add", "a.txt")
    sh(clone, "git", "commit", "-q", "-m", "not pushed")
    out = ws(seed, root, "sweep", "--apply").stdout
    assert clone.exists()
    assert "unpushed" in out and "removed" not in out.replace("would remove", "")


def test_sweep_leaves_a_clone_of_another_repository_alone(tmp_path):
    _, seed = make_origin(tmp_path)
    root = tmp_path / "bench"
    other = tmp_path / "other.git"
    sh(tmp_path, "git", "init", "--bare", "-b", "main", str(other))
    root.mkdir()
    sh(tmp_path, "git", "clone", "-q", str(other), str(root / "ws-foreign"))
    out = ws(seed, root, "sweep", "--apply").stdout
    assert (root / "ws-foreign").exists() and "different repository" in out
