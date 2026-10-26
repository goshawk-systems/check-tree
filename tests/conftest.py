"""Test helpers: a throwaway git repo with one approved program, and a way to call hooks as Claude Code does."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

PKG = pathlib.Path(__file__).resolve().parent.parent
HOOKS = PKG / "hooks"
CLI = [sys.executable, str(PKG / "bin" / "check-tree")]

TREE = {
    "program": "demo",
    "objective": "add() returns the sum of two numbers for every caller",
    "areas": ["src/demo/**"],
    "validation": "docs/plans/demo-validation.md",
    "checks": [
        {"id": "obj", "kind": "objective", "title": "the acceptance test passes",
         "run": "python3 -m pytest -q -p no:cacheprovider tests/test_add.py",
         "fails_when": "add() is wrong for a caller", "needs": ["h-test"], "frozen": ["tests/test_add.py"],
         "proven_with": "the acceptance test calling add() as a caller would",
         "appetite_hours": 4, "plan_text": "make add() correct"},
        {"id": "h-test", "kind": "harness", "title": "the acceptance test exists",
         "run": "test -f tests/test_add.py && \"$CHECK_TREE_BIN\" red-at <TESTS-COMMIT> -- python3 -m pytest -q -p no:cacheprovider tests/test_add.py",
         "fails_when": "the test file is missing, or it did not fail for a real reason before the work", "needs": [],
         "frozen": ["tests/test_add.py"], "appetite_hours": 1},
    ],
}


def sh(cwd, *args, check=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    p = subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True, env=env)
    if check and p.returncode != 0:
        raise AssertionError(f"{args} failed: {p.stderr}{p.stdout}")
    return p


def cli(repo, *args, check=False):
    return sh(repo, *CLI, *args, check=check)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src" / "demo").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "src" / "demo" / "__init__.py").write_text("def add(a, b):\n    return a - b\n")
    (r / "tests" / "test_add.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))\n"
        "from demo import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    (r / "README.md").write_text("demo\n")
    (r / "docs" / "plans").mkdir(parents=True)
    (r / "docs" / "plans" / "demo-validation.md").write_text("# Plan validation: demo\n| Step | state |\n")
    sh(r, "git", "init", "-q", "-b", "main")
    (r / ".gitignore").write_text(".check-tree/state/\n.check-tree/worktrees/\n")
    sh(r, "git", "add", "-A")
    sh(r, "git", "commit", "-q", "-m", "code and the defining test")
    tests_commit = sh(r, "git", "rev-parse", "HEAD").stdout.strip()
    (r / ".check-tree" / "programs" / "demo").mkdir(parents=True)
    (r / ".check-tree" / "config.json").write_text(json.dumps({"base_ref": "main", "remote": None}))
    (r / ".check-tree" / "programs" / "demo" / "checks.json").write_text(
        json.dumps(TREE, indent=2).replace("<TESTS-COMMIT>", tests_commit))
    sh(r, "git", "add", "-A")
    sh(r, "git", "commit", "-q", "-m", "the tree")
    return r


@pytest.fixture
def approved(repo):
    p = cli(repo, "approve", "demo", "--by", "owner", "--words", "yes, build this")
    assert p.returncode == 0, p.stderr
    sh(repo, "git", "commit", "-q", "-am", "owner approved the demo tree")
    return repo


def hook(name, payload, cwd):
    p = subprocess.run([sys.executable, str(HOOKS / name)], input=json.dumps(payload), capture_output=True,
                       text=True, cwd=str(cwd))
    assert p.returncode == 0, p.stderr
    assert "hook error" not in p.stderr, p.stderr
    return json.loads(p.stdout) if p.stdout.strip() else None


def decision(out):
    return (out or {}).get("hookSpecificOutput", {}).get("permissionDecision")


def write_transcript(path, prompt, reads=()):
    rows = [{"type": "user", "message": {"role": "user", "content": prompt}}]
    for f in reads:
        rows.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": str(f)}}]}})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(path)
