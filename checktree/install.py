"""Set a repository up the check-tree way, and manage per-workstream clones.

    check-tree init-repo [--target DIR] [--force]
    check-tree workspace new <name> [--root DIR] [--branch NAME]
    check-tree workspace sweep [--root DIR] [--apply]

`init-repo` copies templates/repo/ into the target repository, filling {{placeholders}} from
`.check-tree/repo.json` (the one per-repo settings file every part of the package reads). It never overwrites an
existing file unless --force is given, and running it twice changes nothing. Standard library only.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
from typing import Optional

PKG = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE_DIR = PKG / "templates" / "repo"
SETTINGS_REL = ".check-tree/repo.json"
PLACEHOLDERS = ("repo_name", "default_branch", "product_prefixes", "test_command", "lint_command")
SKIP_PARTS = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIXES = (".pyc",)

CHECKLIST = """\
What only you can do (check-tree cannot, and does not try):
  [ ] Review and edit .check-tree/repo.json (repo name, product prefixes, test and lint commands, prod patterns).
      It is never overwritten. Files already rendered from it keep their old values: delete one (or pass --force) and rerun.
  [ ] Run: git config core.hooksPath .githooks   (turns on the pre-push hook; `check-tree workspace new` does this itself)
  [ ] Install the Mergify GitHub app on the repository and commit .mergify.yml (the merge queue).
  [ ] Branch protection on {default_branch}: require ONE status check, `all-green`, and nothing else; require the merge queue.
  [ ] Apply the infrastructure module (ci/infra, if your template set includes it) with your cloud credentials: that is a production change and is yours to run.
  [ ] Fill in docs/ARCHITECTURE-RULES.md (layers, the one approved way) and add the first real row to docs/RULINGS.md.
  [ ] Run the CLAUDE.md size test once: python3 -m pytest -q tests/test_claude_md_size.py
"""


# ── settings ──

def _template_settings() -> dict:
    return json.loads((TEMPLATE_DIR / SETTINGS_REL).read_text())


def load_settings(target: pathlib.Path) -> dict:
    """The target's repo.json over the template's defaults (a missing key takes its default)."""
    s = _template_settings()
    p = target / SETTINGS_REL
    if p.is_file():
        s.update(json.loads(p.read_text()))
    return s


def ensure_settings(target: pathlib.Path) -> bool:
    """Create .check-tree/repo.json from the template if missing. True if it was created."""
    p = target / SETTINGS_REL
    if p.is_file():
        return False
    s = _template_settings()
    s["repo_name"] = target.resolve().name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s, indent=2) + "\n")
    return True


def placeholder_values(settings: dict) -> dict[str, str]:
    prefixes = settings.get("product_prefixes") or []
    return {
        "repo_name": str(settings.get("repo_name", "")),
        "default_branch": str(settings.get("default_branch", "main")),
        # joined with | so it drops straight into a regular expression: (?:{{product_prefixes}})-\d+
        "product_prefixes": "|".join(prefixes) if isinstance(prefixes, list) else str(prefixes),
        "test_command": str(settings.get("test_command", "")),
        "lint_command": str(settings.get("lint_command", "")),
    }


def render(text: str, values: dict[str, str]) -> str:
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


# ── init-repo ──

def template_files() -> list[pathlib.Path]:
    out = []
    for p in sorted(TEMPLATE_DIR.rglob("*")):
        rel = p.relative_to(TEMPLATE_DIR)
        if p.is_file() and not (set(rel.parts) & SKIP_PARTS) and not p.name.endswith(SKIP_SUFFIXES):
            out.append(rel)
    return out


def init_repo(target: pathlib.Path, force: bool = False) -> dict[str, list[str]]:
    """Copy the template tree into target. Returns {"created": [...], "overwritten": [...], "skipped": [...],
    "kept": [...]} of relative paths. repo.json is settings, not template: never overwritten, even with force."""
    target = pathlib.Path(target)
    target.mkdir(parents=True, exist_ok=True)
    result: dict[str, list[str]] = {"created": [], "overwritten": [], "skipped": [], "kept": []}
    if ensure_settings(target):
        result["created"].append(SETTINGS_REL)
    values = placeholder_values(load_settings(target))
    for rel in template_files():
        rels = rel.as_posix()
        dest = target / rel
        if rels == SETTINGS_REL:
            if rels not in result["created"]:
                result["kept"].append(rels)
            continue
        existed = dest.exists() or dest.is_symlink()
        if existed and not force:
            result["skipped"].append(rels)
            continue
        src = TEMPLATE_DIR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if existed:
            dest.unlink()
        raw = src.read_bytes()
        try:
            dest.write_text(render(raw.decode("utf-8"), values))
        except UnicodeDecodeError:
            dest.write_bytes(raw)
        shutil.copymode(src, dest)
        result["overwritten" if existed else "created"].append(rels)
    return result


def report_init(target: pathlib.Path, result: dict[str, list[str]]) -> str:
    lines = [f"check-tree init-repo: {target}"]
    for key, verb in (("created", "created"), ("overwritten", "overwritten (--force)"),
                      ("kept", "kept (settings, never overwritten)"),
                      ("skipped", "skipped (already exists; use --force to overwrite)")):
        for rel in result[key]:
            lines.append(f"  {verb}: {rel}")
    lines.append(f"{len(result['created'])} created, {len(result['overwritten'])} overwritten, "
                 f"{len(result['skipped'])} skipped.")
    branch = load_settings(target).get("default_branch", "main")
    lines.append("")
    lines.append(CHECKLIST.format(default_branch=branch))
    return "\n".join(lines)


# ── workspaces (the ws-new / ws-sweep equivalents) ──

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _git(cwd, *args, timeout: int = 600) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout if p.returncode == 0 else p.stderr + p.stdout).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def workspace_root(root: Optional[str] = None) -> pathlib.Path:
    """--root, else $CHECK_TREE_WORKSPACE_ROOT, else ~/workbench."""
    return pathlib.Path(root or os.environ.get("CHECK_TREE_WORKSPACE_ROOT") or "~/workbench").expanduser()


def origin_of(repo: pathlib.Path) -> Optional[str]:
    code, out = _git(repo, "remote", "get-url", "origin")
    return out if code == 0 and out else None


def default_branch_of(repo: pathlib.Path) -> str:
    p = repo / SETTINGS_REL
    if p.is_file():
        try:
            return json.loads(p.read_text()).get("default_branch") or "main"
        except ValueError:
            pass
    return "main"


def workspace_new(name: str, origin_repo: pathlib.Path, root: Optional[str] = None,
                  branch: Optional[str] = None) -> pathlib.Path:
    """A fresh clone of origin_repo's origin at <root>/ws-<name>, with core.hooksPath set to .githooks."""
    if not NAME_RE.match(name):
        raise SystemExit("check-tree: a workspace name must match [a-z0-9][a-z0-9._-]*")
    url = origin_of(origin_repo)
    if not url:
        raise SystemExit(f"check-tree: {origin_repo} has no 'origin' remote to clone")
    default = default_branch_of(origin_repo)
    dest = workspace_root(root) / f"ws-{name}"
    if dest.exists():
        raise SystemExit(f"check-tree: {dest} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    code, out = _git(dest.parent, "clone", "--quiet", url, str(dest))
    if code != 0:
        raise SystemExit(f"check-tree: clone failed: {out}")
    _git(dest, "config", "core.hooksPath", ".githooks")
    if branch:
        code, out = _git(dest, "checkout", "-b", branch, f"origin/{default}", "--quiet")
        if code != 0:
            raise SystemExit(f"check-tree: could not create branch {branch} off origin/{default}: {out}")
    return dest


def clone_verdict(clone: pathlib.Path) -> tuple[bool, str]:
    """(removable, reason). Removable only when nothing in the clone can be lost: clean tree (untracked files
    count), no stash, no local commit that is on no remote branch, and HEAD already in origin's default branch.
    Reads the remote refs the clone already has: it never fetches, so a stale view errs toward keeping."""
    code, out = _git(clone, "rev-parse", "--is-inside-work-tree")
    if code != 0 or out != "true":
        return False, "not a git clone"
    code, out = _git(clone, "status", "--porcelain=v1", "--untracked-files=all")
    if code != 0:
        return False, "git status failed"
    if out:
        return False, "uncommitted or untracked work"
    if _git(clone, "rev-parse", "-q", "--verify", "refs/stash")[0] == 0:
        return False, "has a stash"
    code, out = _git(clone, "log", "--branches", "--not", "--remotes", "--oneline", "-1")
    if code != 0:
        return False, "could not list unpushed commits"
    if out:
        return False, "unpushed commits (on a branch that is on no remote)"
    code, head = _git(clone, "symbolic-ref", "-q", "--short", "HEAD")
    if code != 0:
        return False, "detached HEAD"
    default = default_branch_of(clone)
    if _git(clone, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{default}")[0] != 0:
        return False, f"no origin/{default} to compare with"
    if _git(clone, "merge-base", "--is-ancestor", "HEAD", f"origin/{default}")[0] == 0:
        return True, f"{head} is merged into origin/{default}, nothing unpushed"
    code, cherry = _git(clone, "cherry", f"origin/{default}", "HEAD")
    if code == 0 and not any(line.startswith("+") for line in cherry.splitlines()):
        return True, f"{head}'s commits are all in origin/{default}, nothing unpushed"
    return False, f"{head} is not merged into origin/{default}"


def workspace_sweep(root: Optional[str] = None, apply: bool = False,
                    only_origin: Optional[str] = None) -> list[tuple[pathlib.Path, bool, str, bool]]:
    """Each ws-* clone under root: (path, removable, reason, removed). Dry run unless apply."""
    base = workspace_root(root)
    out = []
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        if not d.name.startswith("ws-") or d.is_symlink() or not d.is_dir():
            continue
        if only_origin and origin_of(d) != only_origin:
            out.append((d, False, "a clone of a different repository", False))
            continue
        ok, why = clone_verdict(d)
        removed = False
        if ok and apply:
            ok2, why2 = clone_verdict(d)  # re-check immediately before removing
            if ok2:
                shutil.rmtree(d)
                removed = True
            else:
                ok, why = False, why2
        out.append((d, ok, why, removed))
    return out
