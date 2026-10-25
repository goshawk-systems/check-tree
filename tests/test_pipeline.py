"""The three pipeline scripts, each run as a command against a throwaway repo."""
import json
import subprocess
import sys

from conftest import PKG, sh

PIPE = PKG / "pipeline"


def run(script, cwd, *args):
    return subprocess.run([sys.executable, str(PIPE / script), *args], cwd=str(cwd), capture_output=True, text=True)


# ── impacted_tests ──

def make_project(tmp_path):
    r = tmp_path / "p"
    (r / "src" / "billing").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "src" / "billing" / "__init__.py").write_text("")
    (r / "src" / "billing" / "charge.py").write_text("def charge(x):\n    return x\n")
    (r / "src" / "billing" / "refund.py").write_text("def refund(x):\n    return -x\n")
    (r / "tests" / "test_charge.py").write_text("from billing.charge import charge\n")
    (r / "tests" / "test_refund.py").write_text("from billing import refund\n")
    (r / "tests" / "test_paths.py").write_text("FILES = ['src/billing/charge.py']\n")
    (r / "tests" / "test_other.py").write_text("import json\n")
    sh(r, "git", "init", "-q", "-b", "main")
    sh(r, "git", "add", "-A")
    sh(r, "git", "commit", "-q", "-m", "start")
    sh(r, "git", "checkout", "-q", "-b", "change")
    return r


def test_impacted_tests_selects_importers_path_pins_and_edited_tests(tmp_path):
    r = make_project(tmp_path)
    (r / "src" / "billing" / "charge.py").write_text("def charge(x):\n    return x * 2\n")
    (r / "tests" / "test_other.py").write_text("import json\nX = 1\n")
    sh(r, "git", "commit", "-q", "-am", "change charge")
    out = run("impacted_tests.py", r, "--base", "main").stdout.split()
    assert out == ["tests/test_charge.py", "tests/test_other.py", "tests/test_paths.py"]


def test_impacted_tests_shards_cover_the_selection_without_overlap(tmp_path):
    r = make_project(tmp_path)
    for f in ("charge.py", "refund.py"):
        (r / "src" / "billing" / f).write_text("# changed\n")
    sh(r, "git", "commit", "-q", "-am", "change both")
    full = run("impacted_tests.py", r, "--base", "main").stdout.split()
    parts = [run("impacted_tests.py", r, "--base", "main", "--shard", f"{i}/2").stdout.split() for i in (1, 2)]
    assert sorted(parts[0] + parts[1]) == sorted(full) and not set(parts[0]) & set(parts[1])


# ── ratchet ──

def ratchet_repo(tmp_path, prints):
    r = tmp_path / "rr"
    (r / "src").mkdir(parents=True)
    (r / "src" / "app.py").write_text("print('x')\n" * prints)
    (r / "ratchet-rules.json").write_text(json.dumps(
        [{"name": "no-print", "pattern": r"^\s*print\(", "include": ["src/*.py"], "why": "use the logger"}]))
    sh(r, "git", "init", "-q", "-b", "main")
    sh(r, "git", "add", "-A")
    sh(r, "git", "commit", "-q", "-m", "start")
    return r


def test_ratchet_seeds_once_fails_on_growth_and_only_lowers(tmp_path):
    r = ratchet_repo(tmp_path, 3)
    assert run("ratchet.py", r, "check").returncode == 1  # no baseline yet
    assert run("ratchet.py", r, "seed", "no-print").returncode == 0
    assert run("ratchet.py", r, "seed", "no-print").returncode != 0  # never re-seeded upward
    assert run("ratchet.py", r, "check").returncode == 0
    (r / "src" / "app.py").write_text("print('x')\n" * 4)
    p = run("ratchet.py", r, "check")
    assert p.returncode == 1 and "4 (baseline 3)" in p.stdout
    (r / "src" / "app.py").write_text("print('x')\n")
    assert run("ratchet.py", r, "lower").returncode == 0
    assert json.loads((r / "ratchet-baseline.json").read_text()) == {"no-print": 1}


# ── scripted_review ──

GOOD = """## What changed
Charge doubles.
## Why
Pricing change.
## How it was proven
The acceptance test passes.
Closes-item: BILL-12
"""


def review_repo(tmp_path):
    r = tmp_path / "rv"
    (r / "tests").mkdir(parents=True)
    (r / "tests" / "test_a.py").write_text("def test_a():\n    assert 1 == 1\n    assert 2 == 2\n")
    sh(r, "git", "init", "-q", "-b", "main")
    sh(r, "git", "add", "-A")
    sh(r, "git", "commit", "-q", "-m", "start")
    sh(r, "git", "checkout", "-q", "-b", "change")
    return r


def test_scripted_review_passes_a_complete_description(tmp_path):
    r = review_repo(tmp_path)
    (r / "body.md").write_text(GOOD)
    p = run("scripted_review.py", r, "--body", "body.md", "--base", "main")
    assert p.returncode == 0, p.stdout


def test_scripted_review_names_missing_sections_and_work_item(tmp_path):
    r = review_repo(tmp_path)
    (r / "body.md").write_text("## What changed\nstuff\n## Why\n\n")
    p = run("scripted_review.py", r, "--body", "body.md", "--base", "main")
    assert p.returncode == 1
    assert "missing section: ## How it was proven" in p.stdout and "empty section: ## Why" in p.stdout
    assert "no work item" in p.stdout


def test_a_removed_assertion_needs_a_reason(tmp_path):
    r = review_repo(tmp_path)
    (r / "tests" / "test_a.py").write_text("def test_a():\n    assert 1 == 1\n")
    sh(r, "git", "commit", "-q", "-am", "drop an assertion")
    (r / "body.md").write_text(GOOD)
    p = run("scripted_review.py", r, "--body", "body.md", "--base", "main")
    assert p.returncode == 1 and "tests/test_a.py (1)" in p.stdout
    (r / "body.md").write_text(GOOD + "## Removed assertions\nThe second assertion duplicated the first.\n")
    assert run("scripted_review.py", r, "--body", "body.md", "--base", "main").returncode == 0
