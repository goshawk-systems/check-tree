#!/usr/bin/env python3
"""The review a script can decide, as a required status, instead of an AI review.

Checks, from the pull request's description and diff:
  1. every required section heading is present and not empty;
  2. the description cites a work item (a line matching --item-pattern);
  3. no test assertion is removed unless the description says why, under a "Removed assertions" heading.
What a script cannot judge (is the change good?) is not pretended here; that belongs to the check tree's
evaluator and the owner.

    scripted_review.py --body body.md --base origin/main [--section "## What changed"]... [--item-pattern REGEX]

In GitHub Actions, write the body from the pull request event (see example-workflow.yml).
Exit 1 with one line per problem. Standard library only.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

DEFAULT_SECTIONS = ["## What changed", "## Why", "## How it was proven"]
DEFAULT_ITEM = r"(?mi)^(Closes|Refs)-item:\s*\S+"
ASSERT_LINE = re.compile(r"^-(?!--)\s*(assert\b|self\.assert|expect\(|.*\.should\b)", re.M)
TEST_FILE = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$|[^/]*\.test\.[jt]sx?$)")


def sections(body: str) -> dict[str, str]:
    out, cur, buf = {}, None, []
    for line in body.splitlines():
        if line.startswith("#"):
            if cur is not None:
                out[cur] = "\n".join(buf).strip()
            cur, buf = line.strip(), []
        else:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf).strip()
    return out


def removed_assertions(base: str) -> list[str]:
    mb = subprocess.run(["git", "merge-base", base, "HEAD"], capture_output=True, text=True).stdout.strip() or base
    names = subprocess.run(["git", "diff", "--name-only", mb], capture_output=True, text=True).stdout.split()
    hits = []
    for f in names:
        if not TEST_FILE.search(f):
            continue
        diff = subprocess.run(["git", "diff", "-U0", mb, "--", f], capture_output=True, text=True).stdout
        n = len(ASSERT_LINE.findall(diff))
        if n:
            hits.append(f"{f} ({n})")
    return hits


def review(body: str, required: list[str], item_pattern: str, removed: list[str]) -> list[str]:
    problems = []
    secs = sections(body)
    for s in required:
        if s not in secs:
            problems.append(f"missing section: {s}")
        elif not secs[s]:
            problems.append(f"empty section: {s}")
    if not re.search(item_pattern, body):
        problems.append("no work item cited (e.g. a `Closes-item: ABC-12` line)")
    if removed:
        why = next((v for k, v in secs.items() if "removed assertions" in k.lower()), "")
        if not why:
            problems.append("test assertions removed without a `## Removed assertions` section saying why: "
                            + ", ".join(removed))
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--body", required=True)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--section", action="append")
    ap.add_argument("--item-pattern", default=DEFAULT_ITEM)
    a = ap.parse_args(argv)
    body = open(a.body).read()
    problems = review(body, a.section or DEFAULT_SECTIONS, a.item_pattern, removed_assertions(a.base))
    for p in problems:
        print(f"- {p}")
    print("scripted review: " + ("FAIL" if problems else "pass"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
