"""check-tree core: the tree, its approval, running checks, and the scoreboard.

A program's plan is a tree of checks. Each check is a shell command that passes (exit 0) or fails.
The owner approves the tree once; the approval pins the tree's hash and the contents of its frozen
files (the tests that define the checks). Editing either unapproves it. A check counts only when it
passes at the current head of the branch where passes count, run fresh by the runner. Nobody writes
the scoreboard by hand.

Standard library only. Python 3.9+.
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

KINDS = ("objective", "component", "harness")
STATE_DIR = ".check-tree/state"
WORKTREES_DIR = ".check-tree/worktrees"
PROGRAMS_DIR = ".check-tree/programs"
CONFIG_PATH = ".check-tree/config.json"

DEFAULT_CONFIG = {
    # The ref whose copy of a tree binds, and where passes count unless a tree names `counts_on`.
    "base_ref": "main",
    # Fetch this remote before reading the binding tree (set to null for a repo with no remote).
    "remote": "origin",
    # How many times a builder is sent back on a failing check before it may return as failed.
    "max_send_backs": 2,
    # Seconds a single check command may run.
    "check_timeout_seconds": 1800,
    # A program whose pass count has not moved for this many days shows STALLED.
    "stalled_after_days": 3,
    # Edits to a program's files outside a builder's worktree: "enforce", "warn" or "off".
    "scope": "enforce",
}

# A command that cannot fail is not a check.
TRIVIAL_RUN = re.compile(r"^\s*(true|:|exit\s+0|echo\b[^|;&]*|/bin/true)\s*$")

# Variables never handed to a check command: credentials a check has no business reading.
SECRET_ENV = re.compile(
    r"^(AWS_|AZURE_|GOOGLE_APPLICATION_CREDENTIALS|GCP_|GH_TOKEN$|GITHUB_TOKEN$|ANTHROPIC_API_KEY$|"
    r"OPENAI_API_KEY$|DATABASE_URL|DB_URL|PG[A-Z]*$)|(_TOKEN|_SECRET|_PASSWORD|_API_KEY)$"
)


# ───────────────────────────── git and paths ─────────────────────────────

def git(cwd: pathlib.Path, *args: str, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout if p.returncode == 0 else p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def repo_root(start: Optional[pathlib.Path] = None) -> pathlib.Path:
    """The main checkout's root, even when called from inside a builder's worktree."""
    start = pathlib.Path(start or os.getcwd())
    code, common = git(start, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if code == 0 and common:
        return pathlib.Path(common).parent
    code, top = git(start, "rev-parse", "--show-toplevel")
    if code == 0:
        return pathlib.Path(top)
    raise SystemExit(f"check-tree: {start} is not inside a git repository")


def load_config(root: pathlib.Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    path = root / CONFIG_PATH
    if path.is_file():
        cfg.update(json.loads(path.read_text()))
    return cfg


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def fetch(root: pathlib.Path, cfg: dict) -> Optional[str]:
    """Fetch the remote so hooks read today's binding tree, not a stale checkout. Returns a warning or None."""
    remote = cfg.get("remote")
    if not remote:
        return None
    code, _ = git(root, "remote", "get-url", remote)
    if code != 0:
        return None
    code, out = git(root, "fetch", "-q", remote, timeout=30)
    return None if code == 0 else f"could not fetch {remote}; read the last fetched copy ({out[:120]})"


def binding_ref(root: pathlib.Path, cfg: dict) -> str:
    """The ref whose copy of a tree binds: <remote>/<base_ref> when it exists, else <base_ref>."""
    base = cfg["base_ref"]
    remote = cfg.get("remote")
    if remote and git(root, "rev-parse", "--verify", "-q", f"{remote}/{base}")[0] == 0:
        return f"{remote}/{base}"
    return base


def read_at(root: pathlib.Path, ref: str, rel: str) -> Optional[bytes]:
    try:
        p = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=str(root), capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def head_of(root: pathlib.Path, ref: str) -> Optional[str]:
    code, out = git(root, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
    return out if code == 0 else None


# ───────────────────────────── the tree ─────────────────────────────

@dataclass
class Tree:
    program: str
    data: dict
    source: str  # "ref:<ref>" or "working copy"
    problems: list = field(default_factory=list)

    @property
    def checks(self) -> list[dict]:
        return self.data.get("checks", [])

    def check(self, cid: str) -> Optional[dict]:
        return next((c for c in self.checks if c.get("id") == cid), None)

    @property
    def objective(self) -> str:
        return self.data.get("objective", "")

    @property
    def areas(self) -> list[str]:
        return self.data.get("areas", [])

    @property
    def integration_branch(self) -> str:
        return self.data.get("integration_branch") or f"check-tree/{self.program}"

    def counts_on(self, cfg: dict) -> str:
        return self.data.get("counts_on") or cfg["base_ref"]


def tree_path(program: str) -> str:
    return f"{PROGRAMS_DIR}/{program}/checks.json"


def list_programs(root: pathlib.Path, ref: Optional[str] = None) -> list[str]:
    names = set()
    d = root / PROGRAMS_DIR
    if d.is_dir():
        names.update(p.name for p in d.iterdir() if (p / "checks.json").is_file())
    if ref:
        code, out = git(root, "ls-tree", "--name-only", f"{ref}:{PROGRAMS_DIR}")
        if code == 0:
            names.update(n for n in out.splitlines() if read_at(root, ref, tree_path(n)) is not None)
    return sorted(names)


def load_tree(root: pathlib.Path, program: str, ref: Optional[str] = None) -> Optional[Tree]:
    """The tree at `ref` (the binding copy), or the working copy when ref is None."""
    if ref:
        raw = read_at(root, ref, tree_path(program))
        if raw is None:
            return None
        return Tree(program, json.loads(raw), f"ref:{ref}")
    p = root / tree_path(program)
    if not p.is_file():
        return None
    return Tree(program, json.loads(p.read_text()), "working copy")


def tree_hash(data: dict) -> str:
    body = {k: v for k, v in data.items() if k != "approved"}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()


def frozen_files(tree: Tree) -> list[str]:
    out: list[str] = []
    for c in tree.checks:
        for f in c.get("frozen", []):
            if f not in out:
                out.append(f)
    return out


def file_hash_at(root: pathlib.Path, ref: Optional[str], rel: str) -> Optional[str]:
    raw = read_at(root, ref, rel) if ref else ((root / rel).read_bytes() if (root / rel).is_file() else None)
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def validate(tree: Tree, root: pathlib.Path, ref: Optional[str] = None) -> list[str]:
    """Every reason the tree cannot be approved or run. Empty means valid."""
    p: list[str] = []
    d = tree.data
    if d.get("program") != tree.program:
        p.append(f"`program` is {d.get('program')!r}, expected {tree.program!r}")
    if not str(d.get("objective", "")).strip():
        p.append("no `objective`: one sentence, in the owner's words")
    if not d.get("areas"):
        p.append("no `areas`: the file patterns this program owns")
    ids = [c.get("id") for c in tree.checks]
    if not ids:
        p.append("no checks")
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        p.append(f"duplicate check ids: {sorted(dupes)}")
    objectives = [c for c in tree.checks if c.get("kind") == "objective"]
    if len(objectives) != 1:
        p.append(f"exactly one `objective` check is required, found {len(objectives)}")
    known = set(ids)
    for c in tree.checks:
        cid = c.get("id", "?")
        if c.get("kind") not in KINDS:
            p.append(f"{cid}: `kind` must be one of {KINDS}")
        run = str(c.get("run", ""))
        if not run.strip():
            p.append(f"{cid}: no `run` command")
        elif TRIVIAL_RUN.match(run):
            p.append(f"{cid}: `run` cannot fail ({run!r}); a check must be able to fail")
        if not str(c.get("fails_when", "")).strip():
            p.append(f"{cid}: no `fails_when`: say what a failure means")
        if not isinstance(c.get("appetite_hours"), (int, float)) or c["appetite_hours"] <= 0:
            p.append(f"{cid}: `appetite_hours` must be a positive number: the time the owner bets on it")
        for n in c.get("needs", []):
            if n not in known:
                p.append(f"{cid}: needs unknown check {n!r}")
        for f in c.get("frozen", []):
            if file_hash_at(root, ref, f) is None:
                p.append(f"{cid}: frozen file {f} does not exist" + (f" at {ref}" if ref else ""))
    # needs must not loop
    graph = {c.get("id"): list(c.get("needs", [])) for c in tree.checks}
    seen: dict[str, int] = {}

    def visit(n: str, stack: list[str]) -> None:
        if seen.get(n) == 2:
            return
        if seen.get(n) == 1:
            p.append(f"`needs` loop: {' -> '.join(stack + [n])}")
            return
        seen[n] = 1
        for m in graph.get(n, []):
            if m in graph:
                visit(m, stack + [n])
        seen[n] = 2

    for n in graph:
        visit(n, [])
    return p


def approval_state(tree: Tree, root: pathlib.Path, ref: Optional[str]) -> tuple[bool, list[str]]:
    """(approved, reasons it is not). Approval = the tree's hash and every frozen file's hash, as recorded."""
    a = tree.data.get("approved") or {}
    why: list[str] = []
    if not a:
        return False, ["never approved by the owner"]
    if a.get("tree_sha256") != tree_hash(tree.data):
        why.append("the tree changed since the owner approved it")
    pinned = a.get("frozen", {})
    for f in frozen_files(tree):
        h = file_hash_at(root, ref, f)
        if f not in pinned:
            why.append(f"frozen file {f} was added after approval")
        elif pinned[f] != h:
            why.append(f"frozen file {f} changed since approval")
    return (not why), why


def approve(root: pathlib.Path, program: str, by: str, words: str) -> dict:
    """Record the owner's approval in the working copy. It binds once committed to the base ref."""
    tree = load_tree(root, program)
    if tree is None:
        raise SystemExit(f"no tree at {tree_path(program)}")
    problems = validate(tree, root)
    if problems:
        raise SystemExit("cannot approve an invalid tree:\n  " + "\n  ".join(problems))
    if not words.strip():
        raise SystemExit("--words is required: the owner's own words approving this tree")
    tree.data["approved"] = {
        "by": by,
        "words": words,
        "at": now(),
        "tree_sha256": tree_hash(tree.data),
        "frozen": {f: file_hash_at(root, None, f) for f in frozen_files(tree)},
    }
    (root / tree_path(program)).write_text(json.dumps(tree.data, indent=2, ensure_ascii=False) + "\n")
    return tree.data["approved"]


# ───────────────────────────── state (local, not committed) ─────────────────────────────

def state_dir(root: pathlib.Path, program: str) -> pathlib.Path:
    d = root / STATE_DIR / program
    d.mkdir(parents=True, exist_ok=True)
    return d


def append_jsonl(path: pathlib.Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def progress(root: pathlib.Path, program: str, line: str, **extra) -> None:
    append_jsonl(state_dir(root, program) / "progress.jsonl", {"at": now(), "line": line, **extra})


# ───────────────────────────── running checks ─────────────────────────────

def check_env(root: pathlib.Path, program: str, workdir: pathlib.Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    env["CHECK_TREE_PROGRAM"] = program
    env["CHECK_TREE_PROGRAM_DIR"] = str(workdir / PROGRAMS_DIR / program)
    env["CHECK_TREE_ROOT"] = str(workdir)
    return env


def run_command(cmd: str, workdir: pathlib.Path, env: dict, timeout: int) -> tuple[bool, str, float]:
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, shell=True, cwd=str(workdir), env=env, capture_output=True, text=True,
                           timeout=timeout)
        out = (p.stdout + ("\n" + p.stderr if p.stderr else "")).strip()
        return p.returncode == 0, out, time.monotonic() - t0
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s", time.monotonic() - t0


def tail(text: str, lines: int = 40) -> str:
    rows = text.splitlines()
    return "\n".join(rows[-lines:])


def order(tree: Tree) -> list[dict]:
    """Checks in dependency order (needs first)."""
    done: list[str] = []
    by_id = {c["id"]: c for c in tree.checks}

    def add(cid: str) -> None:
        if cid in done or cid not in by_id:
            return
        for n in by_id[cid].get("needs", []):
            add(n)
        done.append(cid)

    for c in tree.checks:
        add(c["id"])
    return [by_id[i] for i in done]


def run_checks(root: pathlib.Path, program: str, only: Optional[list[str]] = None) -> list[dict]:
    """Run the binding tree's checks in a fresh worktree at the head of the branch where passes count."""
    cfg = load_config(root)
    warn = fetch(root, cfg)
    ref = binding_ref(root, cfg)
    tree = load_tree(root, program, ref) or load_tree(root, program)
    if tree is None:
        raise SystemExit(f"no tree for {program!r}")
    counts_on = tree.counts_on(cfg)
    if cfg.get("remote") and head_of(root, f"{cfg['remote']}/{counts_on}"):
        counts_on = f"{cfg['remote']}/{counts_on}"
    sha = head_of(root, counts_on)
    if not sha:
        raise SystemExit(f"{counts_on} does not exist")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix=f"check-tree-{program}-"))
    work = tmp / "head"
    code, out = git(root, "worktree", "add", "--detach", "-q", str(work), sha)
    if code != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit(f"could not make a fresh worktree at {sha[:10]}: {out}")
    results: list[dict] = []
    try:
        env = check_env(root, program, work)
        status: dict[str, str] = {}
        for c in order(tree):
            if only and c["id"] not in only:
                continue
            blocked = [n for n in c.get("needs", []) if status.get(n) not in (None, "pass")]
            if blocked:
                row = {"check": c["id"], "status": "blocked", "by": blocked}
            else:
                ok, output, secs = run_command(c["run"], work, env, int(cfg["check_timeout_seconds"]))
                row = {"check": c["id"], "status": "pass" if ok else "fail", "seconds": round(secs, 1),
                       "output_tail": tail(output, 20)}
            row.update({"at": now(), "commit": sha, "ref": counts_on})
            status[c["id"]] = row["status"]
            results.append(row)
            append_jsonl(state_dir(root, program) / "results.jsonl", row)
    finally:
        git(root, "worktree", "remove", "--force", str(work))
        shutil.rmtree(tmp, ignore_errors=True)
    if warn:
        results.append({"warning": warn})
    return results


# ───────────────────────────── the scoreboard ─────────────────────────────

@dataclass
class Score:
    program: str
    commit: Optional[str]
    ref: str
    approved: bool
    unapproved_why: list
    status: dict          # check id -> pass | fail | blocked | not run
    passing: int
    total: int
    ready: list           # check ids whose needs all pass and which do not pass yet
    over_appetite: list
    stalled: bool
    objective_passes: bool


def latest_results(root: pathlib.Path, program: str, commit: Optional[str]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for row in read_jsonl(root / STATE_DIR / program / "results.jsonl"):
        if row.get("commit") == commit and "check" in row:
            latest[row["check"]] = row
    return latest


def dispatch_hours(root: pathlib.Path, program: str) -> dict[str, float]:
    """Hours since each check was first dispatched to a builder."""
    first: dict[str, str] = {}
    for row in read_jsonl(root / STATE_DIR / program / "dispatches.jsonl"):
        first.setdefault(row.get("check", ""), row.get("at", ""))
    out = {}
    for cid, at in first.items():
        try:
            t = dt.datetime.fromisoformat(at)
            out[cid] = (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600
        except ValueError:
            continue
    return out


def score(root: pathlib.Path, program: str, do_fetch: bool = True) -> Score:
    cfg = load_config(root)
    if do_fetch:
        fetch(root, cfg)
    ref = binding_ref(root, cfg)
    tree = load_tree(root, program, ref) or load_tree(root, program)
    if tree is None:
        raise SystemExit(f"no tree for {program!r}")
    ok, why = approval_state(tree, root, ref if tree.source.startswith("ref:") else None)
    counts_on = tree.counts_on(cfg)
    if cfg.get("remote") and head_of(root, f"{cfg['remote']}/{counts_on}"):
        counts_on = f"{cfg['remote']}/{counts_on}"
    sha = head_of(root, counts_on)
    latest = latest_results(root, program, sha)
    status = {c["id"]: latest.get(c["id"], {}).get("status", "not run") for c in tree.checks}
    passing = sum(1 for s in status.values() if s == "pass")
    ready = [c["id"] for c in tree.checks
             if status[c["id"]] != "pass" and all(status.get(n) == "pass" for n in c.get("needs", []))]
    hours = dispatch_hours(root, program)
    over = [c["id"] for c in tree.checks
            if status[c["id"]] != "pass" and hours.get(c["id"], 0) > float(c.get("appetite_hours", 0) or 0)]
    hist_path = state_dir(root, program) / "history.jsonl"
    hist = read_jsonl(hist_path)
    if not hist or hist[-1].get("passing") != passing or hist[-1].get("commit") != sha:
        append_jsonl(hist_path, {"at": now(), "commit": sha, "passing": passing, "total": len(tree.checks)})
        hist = read_jsonl(hist_path)
    stalled = False
    days = float(cfg["stalled_after_days"])
    changed_at = None
    for row in reversed(hist):
        if row.get("passing") != passing:
            break
        changed_at = row.get("at")
    if changed_at and passing < len(tree.checks):
        try:
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(changed_at)
            stalled = age.total_seconds() > days * 86400
        except ValueError:
            pass
    obj = next((c["id"] for c in tree.checks if c.get("kind") == "objective"), None)
    return Score(program, sha, counts_on, ok, why, status, passing, len(tree.checks), ready, over, stalled,
                 bool(obj and status.get(obj) == "pass"))


def render_score(s: Score, tree: Tree) -> str:
    mark = {"pass": "PASS", "fail": "FAIL", "blocked": "blocked", "not run": "not run"}
    lines = [f"{s.program}: {s.passing}/{s.total} checks pass at {s.ref} {str(s.commit)[:10]}"
             + ("  STALLED" if s.stalled else "")
             + ("" if s.approved else "  (tree NOT approved: " + "; ".join(s.unapproved_why) + ")"),
             f"objective: {'PASSES' if s.objective_passes else 'not yet'} - {tree.objective}"]
    for c in order(tree):
        flag = " (ready)" if c["id"] in s.ready else ""
        flag += " (OVER APPETITE)" if c["id"] in s.over_appetite else ""
        lines.append(f"  [{mark[s.status[c['id']]]:>7}] {c['kind']:<9} {c['id']}{flag}: {c.get('title', '')}")
    return "\n".join(lines)


# ───────────────────────────── builders: worktrees and briefs ─────────────────────────────

def ensure_integration_branch(root: pathlib.Path, tree: Tree, cfg: dict) -> str:
    """The build's one integration branch, created from the base ref when absent. Returns its name."""
    name = tree.integration_branch
    if head_of(root, name):
        return name
    remote = cfg.get("remote")
    if remote and head_of(root, f"{remote}/{name}"):
        git(root, "branch", name, f"{remote}/{name}")
        return name
    base = binding_ref(root, cfg)
    git(root, "branch", name, base)
    return name


def make_worktree(root: pathlib.Path, tree: Tree, check_id: str, cfg: dict) -> tuple[pathlib.Path, str]:
    integ = ensure_integration_branch(root, tree, cfg)
    suffix = hashlib.sha1(f"{check_id}{time.time()}".encode()).hexdigest()[:6]
    branch = f"ct/{tree.program}/{check_id}-{suffix}"
    path = root / WORKTREES_DIR / f"{tree.program}__{check_id}__{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    code, out = git(root, "worktree", "add", "-q", "-b", branch, str(path), integ)
    if code != 0:
        raise RuntimeError(f"could not create the builder's worktree: {out}")
    return path, branch


def owner_of(root: pathlib.Path, rel_path: str, trees: list[Tree]) -> Optional[Tree]:
    rel = rel_path.lstrip("./")
    for t in trees:
        for pattern in t.areas:
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, pattern.rstrip("/") + "/*"):
                return t
    return None


def brief(tree: Tree, check: dict, s: Score, worktree: Optional[pathlib.Path], branch: Optional[str],
          integ: str) -> str:
    frozen = check.get("frozen", [])
    lines = [
        "",
        "--- check-tree brief (appended by the dispatch hook; it replaces any reading list) ---",
        f"Program: {tree.program}   Objective: {tree.objective}",
        f"Scoreboard: {s.passing}/{s.total} pass. Your ONE check: {check['id']} ({check['kind']}).",
        f"  What it proves: {check.get('title', '')}",
    ]
    if check.get("plan_text"):
        lines.append(f"  The plan's own words for this step: {check['plan_text']}")
    lines += [
        f"  Command: {check['run']}",
        f"  It fails when: {check.get('fails_when', '')}",
        f"  Time budget: {check.get('appetite_hours')} hours.",
    ]
    if frozen:
        lines.append("  Frozen (you may NOT edit; they define the check): " + ", ".join(frozen))
    if worktree:
        lines += [
            f"Worktree: {worktree}",
            f"Branch: {branch} (made from the build's integration branch {integ}).",
            "Work ONLY in that worktree. Commit everything there.",
        ]
    lines += [
        "When you stop, the stop hook runs the check in your worktree. You will be sent back if it fails,",
        "if anything is uncommitted, or if a frozen file changed. Passing the check is the only way to be done.",
        "If the check itself is wrong, say so on a line starting `Check wrong:` with your evidence; do not",
        "work around it. Anything else you notice, on a line starting `Noticed:`.",
        "--- end of brief ---",
    ]
    return "\n".join(lines)


CHECK_LINE = re.compile(r"(?m)^\s*Check:\s*([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\s*$")
WORKTREE_LINE = re.compile(r"(?m)^Worktree:\s*(\S.*?)\s*$")


def parse_binding(text: str) -> Optional[tuple[str, str]]:
    m = CHECK_LINE.search(text or "")
    return (m.group(1), m.group(2)) if m else None
