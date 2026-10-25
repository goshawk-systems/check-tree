#!/usr/bin/env python3
"""Which tests does this change reach? Run them on the pull request, before the change joins a merge batch.

Selected: test files the change adds or edits; test files that import a module the change touched (by name);
and test files that mention a changed file's path. It reads text, not an import graph, so it misses modules
reached only through another module; the full suite in the merge queue still catches those.

    impacted_tests.py --base origin/main [--tests tests] [--src src] [--shard 1/4] [--max-files 400]

Prints one test file per line. With --shard I/N, prints only the I-th of N balanced slices, so a large
selection runs as parallel jobs instead of one job that times out.
Standard library only.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys


def changed_files(base: str) -> list[str]:
    mb = subprocess.run(["git", "merge-base", base, "HEAD"], capture_output=True, text=True)
    ref = mb.stdout.strip() or base
    out = subprocess.run(["git", "diff", "--name-only", ref], capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line.strip()]


def module_names(path: str, src_roots: list[str]) -> set[str]:
    """`src/billing/charge.py` -> {'billing.charge', 'charge'} (and the package for __init__.py)."""
    if not path.endswith(".py"):
        return set()
    p = pathlib.PurePosixPath(path)
    parts = list(p.with_suffix("").parts)
    for root in src_roots:
        rp = pathlib.PurePosixPath(root).parts
        if tuple(parts[:len(rp)]) == rp:
            parts = parts[len(rp):]
            break
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return set()
    return {".".join(parts), parts[-1]}


def is_test(path: str, tests_dir: str) -> bool:
    name = pathlib.PurePosixPath(path).name
    return path.startswith(tests_dir.rstrip("/") + "/") and (name.startswith("test_") or name.endswith("_test.py"))


def select(changed: list[str], tests_dir: str, src_roots: list[str], repo: pathlib.Path) -> list[str]:
    tests = sorted(str(p.relative_to(repo)) for p in (repo / tests_dir).rglob("*.py")
                   if is_test(str(p.relative_to(repo)), tests_dir))
    picked = {c for c in changed if is_test(c, tests_dir) and (repo / c).is_file()}
    mods = set()
    for c in changed:
        mods |= module_names(c, src_roots)
    patterns = [re.compile(rf"^\s*(from\s+{re.escape(m)}(\s|\.)|import\s+([\w.]+\s*,\s*)*{re.escape(m)}\b)", re.M)
                for m in mods]
    paths = [c for c in changed if not is_test(c, tests_dir)]
    for t in tests:
        text = (repo / t).read_text(errors="replace")
        if any(p.search(text) for p in patterns) or any(c in text for c in paths):
            picked.add(t)
    return sorted(picked)


def shard(files: list[str], spec: str) -> list[str]:
    i, n = (int(x) for x in spec.split("/"))
    if not 1 <= i <= n:
        raise SystemExit(f"--shard {spec}: expected I/N with 1 <= I <= N")
    return [f for k, f in enumerate(files) if k % n == i - 1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--tests", default="tests")
    ap.add_argument("--src", action="append", default=None, help="source root(s) to strip, e.g. src")
    ap.add_argument("--shard")
    ap.add_argument("--max-files", type=int, default=400)
    a = ap.parse_args(argv)
    repo = pathlib.Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                                       text=True).stdout.strip() or ".")
    chosen = select(changed_files(a.base), a.tests, a.src or ["src"], repo)
    if len(chosen) > a.max_files:
        print(f"# {len(chosen)} files selected, over --max-files {a.max_files}; running only tests the change "
              f"edits, and leaving the rest to the merge queue's full suite", file=sys.stderr)
        chosen = [c for c in chosen if c in changed_files(a.base)]
    if a.shard:
        chosen = shard(chosen, a.shard)
    print("\n".join(chosen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
