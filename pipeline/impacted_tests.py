#!/usr/bin/env python3
"""Which tests does this change reach? Run them on the pull request, before the change joins a merge batch.

Selected:
  * test files the change adds or edits (and every test under a directory whose conftest.py it edits);
  * test files that reach a changed module through the repo's own import graph, however many hops: a test imports a
    service, the service imports a helper, the helper changed. The graph is built once per run from the standard
    library's `ast`: `import a.b`, `from a.b import c`, relative imports, and the bare-name imports (`import helper`)
    of a file that edits `sys.path` first;
  * test files that name a changed file's path in a quoted string (a guard pinned to a path);
  * "whole-tree scanner" tests, always: tests that read the whole checkout (they anchor on `__file__` and walk the tree,
    or run `git ls-files`), because no import graph shows what they read. Only those priced at or under
    --scanner-max-seconds in the durations file; a scanner with no recorded time is taken.
Anything this misses the full suite in the merge queue still catches.

    impacted_tests.py --base origin/main [--settings .check-tree/repo.json] [--tests tests] [--src src]
                      [--durations .check-tree/durations.json] [--scanner-max-seconds 5]
                      [--shard 1/4] [--max-files 400] [--explain]

Prints one test file per line. With --shard I/N, prints only the I-th of N balanced slices. --settings reads `tests_dir`,
`src_roots` and `durations_file` from the repo's settings file when the flags are not given.
Standard library only.
"""
from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import subprocess
import sys
from collections import defaultdict, deque

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "build", "dist", ".tox", ".mypy_cache"}
TREE_ENUM_METHODS = {"rglob", "glob", "iterdir", "walk", "scandir", "listdir"}


def changed_files(base: str) -> list[str]:
    mb = subprocess.run(["git", "merge-base", base, "HEAD"], capture_output=True, text=True)
    ref = mb.stdout.strip() or base
    out = subprocess.run(["git", "diff", "--name-only", ref], capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line.strip()]


def is_test(path: str, tests_dir: str = "tests") -> bool:
    """A test file by name, wherever it lives (`tests_dir` is kept for callers; a src/pkg/test_x.py is a test too)."""
    name = pathlib.PurePosixPath(path).name
    if "/templates/" in f"/{path}":  # data the installer copies into a repo, never a test of this one
        return False
    return path.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def py_files(repo: pathlib.Path) -> list[str]:
    out = []
    for p in repo.rglob("*.py"):
        rel = p.relative_to(repo)
        if SKIP_DIRS & set(rel.parts):
            continue
        out.append(rel.as_posix())
    return sorted(out)


def module_names(path: str, src_roots: list[str]) -> set[str]:
    """`src/billing/charge.py` -> {'src.billing.charge', 'billing.charge'}; a package's __init__.py names the package."""
    if not path.endswith(".py"):
        return set()
    parts = list(pathlib.PurePosixPath(path).with_suffix("").parts)
    names: set[str] = set()
    candidates = [parts]
    for root in src_roots:
        rp = pathlib.PurePosixPath(root).parts
        if rp and tuple(parts[:len(rp)]) == rp:
            candidates.append(parts[len(rp):])
    for ps in candidates:
        if ps and ps[-1] == "__init__":
            ps = ps[:-1]
        if ps and all(x.isidentifier() for x in ps):
            names.add(".".join(ps))
    return names


class Graph:
    """Who imports whom, among the repo's own Python files. Built once; `reaching` walks it backwards."""

    def __init__(self, repo: pathlib.Path, src_roots: list[str]):
        self.repo = repo
        self.files = py_files(repo)
        self.by_name: dict[str, str] = {}
        self.by_stem: dict[str, list[str]] = defaultdict(list)
        for f in self.files:
            for n in module_names(f, src_roots):
                self.by_name.setdefault(n, f)
            p = pathlib.PurePosixPath(f)
            stem = p.parent.name if p.name == "__init__.py" else p.stem
            self.by_stem[stem].append(f)
        self.imports: dict[str, set[str]] = {}
        self.importers: dict[str, set[str]] = defaultdict(set)
        self.sources: dict[str, str] = {}
        for f in self.files:
            text = (repo / f).read_text(encoding="utf-8", errors="replace")
            self.sources[f] = text
            deps = self._deps_of(f, text)
            self.imports[f] = deps
            for d in deps:
                self.importers[d].add(f)

    def _package_of(self, f: str) -> list[str]:
        """The dotted package a file's relative imports are resolved against, by each name it is known under."""
        p = pathlib.PurePosixPath(f)
        return list(p.parent.parts)

    def _resolve(self, dotted: str) -> list[str]:
        """The files `import dotted` runs: the module and every package above it."""
        parts = dotted.split(".")
        out = []
        for i in range(1, len(parts) + 1):
            f = self.by_name.get(".".join(parts[:i]))
            if f:
                out.append(f)
        return out

    def _relative(self, f: str, level: int, module: str | None) -> list[str]:
        pkg = self._package_of(f)
        if level - 1 > len(pkg):
            return []
        base = pkg[: len(pkg) - (level - 1)]
        dotted_rel = ".".join(base + ([module] if module else []))
        # the package path is relative to the repo root; try it under every known source-root spelling
        hits = self._resolve(dotted_rel) if dotted_rel else []
        if not hits:
            hits = [g for g in self.files if g == "/".join(base + (module.split(".") if module else [])) + ".py"
                    or g == "/".join(base + (module.split(".") if module else []) + ["__init__.py"])]
        return hits

    def _deps_of(self, f: str, text: str) -> set[str]:
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return set()
        edits_path = False
        deps: set[str] = set()
        bare: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("insert", "append"):
                v = node.func.value
                if isinstance(v, ast.Attribute) and v.attr == "path" and isinstance(v.value, ast.Name) and v.value.id == "sys":
                    edits_path = True
            if isinstance(node, ast.Import):
                for a in node.names:
                    hit = self._resolve(a.name)
                    deps.update(hit)
                    if not hit:
                        bare.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    hits = self._relative(f, node.level, node.module)
                    deps.update(hits)
                    base = ".".join(self._package_of(f)[: len(self._package_of(f)) - (node.level - 1)]
                                    + ([node.module] if node.module else []))
                    for a in node.names:
                        deps.update(self._resolve(f"{base}.{a.name}"))
                    continue
                mod = node.module or ""
                hit = self._resolve(mod)
                deps.update(hit)
                for a in node.names:
                    deps.update(x for x in self._resolve(f"{mod}.{a.name}") if x not in hit)
                if not hit:
                    bare.add(mod.split(".")[0])
        if edits_path:
            for name in bare:
                deps.update(self.by_stem.get(name, []))
        deps.discard(f)
        return deps

    def reaching(self, changed: set[str]) -> dict[str, int]:
        """{file: hops} for every file that imports a changed file, directly (1) or through others, to a fixpoint."""
        seen: dict[str, int] = {}
        queue = deque((c, 0) for c in changed if c in self.imports)
        done = set(changed)
        while queue:
            cur, hop = queue.popleft()
            for imp in self.importers.get(cur, ()):
                if imp not in done:
                    done.add(imp)
                    seen[imp] = hop + 1
                    queue.append((imp, hop + 1))
        return seen

    def reads_tree_through(self, f: str) -> bool:
        """True when this file, or a repo module it imports (to any depth), reads the whole checkout."""
        stack, seen = [f], set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if reads_the_tree(self.sources.get(cur, "")):
                return True
            stack.extend(self.imports.get(cur, ()))
        return False


def reads_the_tree(source: str) -> bool:
    """True when the module's own AST reads the checkout: anchored to it by `__file__` and walking/globbing the tree,
    or running `git ls-files`. Unparseable source reads nothing."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    anchored = False
    enumerates = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "__file__":
            anchored = True
        elif isinstance(node, ast.Constant) and node.value == "ls-files":
            return True
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in TREE_ENUM_METHODS:
            enumerates = True
    return anchored and enumerates


def select(changed: list[str], tests_dir: str, src_roots: list[str], repo: pathlib.Path,
           durations: dict[str, float] | None = None, scanner_max_seconds: float = 5.0,
           scanners: bool = True) -> dict[str, list[str]]:
    """{test file: why it was picked}."""
    graph = Graph(repo, src_roots)
    tests = [f for f in graph.files if is_test(f, tests_dir)]
    why: dict[str, list[str]] = defaultdict(list)
    for c in changed:
        if is_test(c, tests_dir) and (repo / c).is_file():
            why[c].append("edited")
        if pathlib.PurePosixPath(c).name == "conftest.py" and (repo / c).is_file():
            d = str(pathlib.PurePosixPath(c).parent)
            for t in tests:
                if d == "." or t.startswith(d + "/"):
                    why[t].append(f"under edited {c}")
    reach = graph.reaching({c for c in changed if c in graph.imports})
    for t in tests:
        if t in reach:
            why[t].append(f"imports a changed module ({reach[t]} hop{'s' if reach[t] > 1 else ''})")
    paths = [c for c in changed if not is_test(c, tests_dir)]
    for t in tests:
        text = graph.sources[t]
        hit = next((c for c in paths if c in text), None)
        if hit:
            why[t].append(f"names {hit}")
    if scanners:
        known = durations or {}
        for t in tests:
            if graph.reads_tree_through(t) and known.get(t, 0.0) <= scanner_max_seconds:
                why[t].append("reads the whole tree")
    return {t: why[t] for t in sorted(why)}


def shard(files: list[str], spec: str) -> list[str]:
    i, n = (int(x) for x in spec.split("/"))
    if not 1 <= i <= n:
        raise SystemExit(f"--shard {spec}: expected I/N with 1 <= I <= N")
    return [f for k, f in enumerate(files) if k % n == i - 1]


def load_json(path: str) -> dict:
    p = pathlib.Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=None)
    ap.add_argument("--settings", default=".check-tree/repo.json")
    ap.add_argument("--tests", default=None)
    ap.add_argument("--src", action="append", default=None, help="source root(s) to strip, e.g. src")
    ap.add_argument("--durations", default=None)
    ap.add_argument("--scanner-max-seconds", type=float, default=5.0)
    ap.add_argument("--no-scanners", action="store_true")
    ap.add_argument("--shard")
    ap.add_argument("--max-files", type=int, default=400)
    ap.add_argument("--explain", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_json(a.settings)
    tests_dir = a.tests or cfg.get("tests_dir") or "tests"
    src_roots = a.src or cfg.get("src_roots") or ["src"]
    base = a.base or f"origin/{cfg.get('default_branch') or 'main'}"
    dur = {k: float(v) for k, v in load_json(a.durations or cfg.get("durations_file") or ".check-tree/durations.json").items()
           if isinstance(v, (int, float))}
    repo = pathlib.Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                                       text=True).stdout.strip() or ".")
    changed = changed_files(base)
    why = select(changed, tests_dir, list(src_roots), repo, dur, a.scanner_max_seconds, not a.no_scanners)
    chosen = sorted(why)
    if len(chosen) > a.max_files:
        print(f"# {len(chosen)} files selected, over --max-files {a.max_files}; running only tests the change "
              f"edits, and leaving the rest to the merge queue's full suite", file=sys.stderr)
        chosen = [c for c in chosen if c in changed]
    if a.explain:
        for t in chosen:
            print(f"# {t}: {'; '.join(why[t])}", file=sys.stderr)
    if a.shard:
        chosen = shard(chosen, a.shard)
    print("\n".join(chosen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
