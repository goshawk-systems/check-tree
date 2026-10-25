#!/usr/bin/env python3
"""Rules that can only get stricter.

Each rule is a regular expression counted over a set of files. The counts are compared with a committed
baseline. A count above its baseline fails. A count below it is good news: `--lower` writes the new, lower
baseline. Nothing ever raises a baseline: a new rule is seeded with today's count once, so it never blocks
existing work, and then it only goes down.

    ratchet.py check   [--rules ratchet-rules.json] [--baseline ratchet-baseline.json]
    ratchet.py lower   (rewrite the baseline with any counts that went down)
    ratchet.py seed NAME   (record today's count for a NEW rule; refuses a rule that already has a baseline)

ratchet-rules.json:
    [{"name": "no-print-in-app", "pattern": "^\\s*print\\(", "include": ["src/**/*.py"],
      "exclude": ["src/cli/**"], "why": "use the logger"}]
Standard library only.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import pathlib
import re
import subprocess
import sys


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


def count(rule: dict, files: list[str], root: pathlib.Path) -> tuple[int, list[str]]:
    rx = re.compile(rule["pattern"], re.M)
    total, where = 0, []
    for f in files:
        if not any(fnmatch.fnmatch(f, g) for g in rule.get("include", ["**"])):
            continue
        if any(fnmatch.fnmatch(f, g) for g in rule.get("exclude", [])):
            continue
        p = root / f
        if not p.is_file():
            continue
        n = len(rx.findall(p.read_text(errors="replace")))
        if n:
            total += n
            where.append(f"{f}: {n}")
    return total, where


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["check", "lower", "seed"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("--rules", default="ratchet-rules.json")
    ap.add_argument("--baseline", default="ratchet-baseline.json")
    a = ap.parse_args(argv)
    root = pathlib.Path.cwd()
    rules = json.loads((root / a.rules).read_text())
    bpath = root / a.baseline
    baseline = json.loads(bpath.read_text()) if bpath.is_file() else {}
    files = tracked_files()
    counts = {r["name"]: count(r, files, root) for r in rules}

    if a.action == "seed":
        if not a.name or a.name not in counts:
            raise SystemExit("seed NAME: name a rule from the rules file")
        if a.name in baseline:
            raise SystemExit(f"{a.name} already has a baseline ({baseline[a.name]}); baselines only go down")
        baseline[a.name] = counts[a.name][0]
        bpath.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
        print(f"seeded {a.name} at {baseline[a.name]}")
        return 0

    failed = False
    lowered = []
    for r in rules:
        name, (n, where) = r["name"], counts[r["name"]]
        if name not in baseline:
            print(f"FAIL {name}: no baseline. Seed it once: ratchet.py seed {name}")
            failed = True
            continue
        b = baseline[name]
        if n > b:
            failed = True
            print(f"FAIL {name}: {n} (baseline {b}). {r.get('why', '')}")
            for w in where[:20]:
                print(f"     {w}")
        elif n < b:
            lowered.append(name)
            print(f"ok   {name}: {n}, down from {b}" + (" (run `ratchet.py lower` to lock it in)"
                                                       if a.action == "check" else ""))
            if a.action == "lower":
                baseline[name] = n
        else:
            print(f"ok   {name}: {n}")
    if a.action == "lower" and lowered:
        bpath.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
