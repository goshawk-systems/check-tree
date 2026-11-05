#!/usr/bin/env python3
"""The pull-request description check: four items, no more. A script decides them, so no reviewer has to.

  1. a `Closes-item: <ID>` or `Refs-item: <ID>` line, the id matching `work_item_pattern`;
  2. every changed file is named in the description;
  3. a `Test edits: <path> -- <reason>` line for each EXISTING test file the change edits (a new test needs none);
  4. a non-empty "Consumer proof" section: something the change's own tests do not own.
HTML comments (the template's prompts) never count as content.

    check_pr_body.py --body body.md [--base origin/main] [--settings .check-tree/repo.json] [--files-from list.txt]

Changed files come from `git diff` against the merge base with --base, unless --files-from gives them. Exit 1 with
one line per problem. Also `size` mode:  check_pr_body.py size --body body.md --base origin/main  (max_pr_files).
Standard library only.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import settings as settings_mod  # noqa: E402

COMMENT = re.compile(r"<!--.*?-->", re.S)
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$")
TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)(test_[^/]*|[^/]*_test\.[A-Za-z]+|[^/]*\.(test|spec)\.[A-Za-z]+)$")
DASH = r"(?:--|—|–|-)"
NOT_A_PROOF = {"n/a", "na", "none", "todo", "tbd", "-", "n.a."}


def strip_comments(body: str) -> str:
    return COMMENT.sub("", body)


def is_test_file(path: str) -> bool:
    return bool(TEST_PATH.search(path))


def section(body: str, name: str) -> str | None:
    """The text under the first heading containing `name` (case-insensitive), up to the next heading; None if absent."""
    out, inside = [], False
    for line in body.splitlines():
        m = HEADING.match(line)
        if m:
            if inside:
                break
            inside = name.lower() in m.group(1).lower()
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip() if inside or out else None


def has_section(body: str, name: str) -> bool:
    return any((m := HEADING.match(line)) and name.lower() in m.group(1).lower() for line in body.splitlines())


def work_item_line(body: str, pattern: str) -> bool:
    return bool(re.search(rf"(?mi)^[ \t]*(Closes|Refs)-item:[ \t]*`?(?:{pattern})`?[ \t]*$", body))


def test_edits(body: str) -> dict[str, str]:
    """{path: reason} from every `Test edits: <path> -- <reason>` line."""
    out = {}
    for m in re.finditer(rf"(?mi)^[ \t]*Test edits:[ \t]*`?([^\s`]+)`?[ \t]*{DASH}+[ \t]*(\S[^\n]*?)[ \t]*$", body):
        out[m.group(1)] = m.group(2)
    return out


def check(body: str, changed: list[str], edited_existing: list[str], work_item_pattern: str) -> list[str]:
    body = strip_comments(body)
    problems = []
    if not work_item_line(body, work_item_pattern):
        problems.append(f"no work item: add a `Closes-item: <ID>` or `Refs-item: <ID>` line (an id matching {work_item_pattern})")
    unnamed = [f for f in changed if f not in body]
    if unnamed:
        shown = ", ".join(unnamed[:10]) + (f" and {len(unnamed) - 10} more" if len(unnamed) > 10 else "")
        problems.append(f"{len(unnamed)} changed file(s) not named in the description: {shown}")
    edits = test_edits(body)
    for f in edited_existing:
        if f not in edits:
            problems.append(f"existing test edited without a line `Test edits: {f} -- <why the test keeps its protection>`: {f}")
    proof = section(body, "consumer proof")
    if proof is None:
        problems.append("no `## Consumer proof` section")
    elif not proof or proof.lower().strip(" .*_`") in NOT_A_PROOF:
        problems.append("the Consumer proof section is empty: say what you reproduced at the consumer, and how")
    return problems


def git_lines(*args: str) -> list[str]:
    p = subprocess.run(["git", *args], capture_output=True, text=True)
    return [x for x in p.stdout.splitlines() if x.strip()]


def merge_base(base: str) -> str:
    return (subprocess.run(["git", "merge-base", base, "HEAD"], capture_output=True, text=True).stdout.strip() or base)


def changed_files(base: str) -> tuple[list[str], list[str]]:
    """(every changed path, the existing test files among them that were modified or deleted or renamed away)."""
    mb = merge_base(base)
    every = git_lines("diff", "--name-only", "--no-renames", mb)
    existing = git_lines("diff", "--name-only", "--no-renames", "--diff-filter=MD", mb)
    return every, [f for f in existing if is_test_file(f)]


def size_problem(changed: list[str], body: str, max_files: int) -> str | None:
    if len(changed) <= max_files:
        return None
    if re.search(r"(?mi)^\s*Big-PR-approved-by:\s*\S+", strip_comments(body)):
        return None
    return (f"{len(changed)} changed files is over the limit of {max_files}: split it into smaller pull requests, "
            f"each proven on its own, or have the owner add a `Big-PR-approved-by: <name>` line")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = "body"
    if argv[:1] == ["size"]:
        mode, argv = "size", argv[1:]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--body", required=True)
    ap.add_argument("--base", default=None)
    ap.add_argument("--settings", default=settings_mod.SETTINGS_PATH)
    ap.add_argument("--files-from")
    ap.add_argument("--existing-tests-from", help="file listing the existing test files edited (with --files-from)")
    a = ap.parse_args(argv)
    s = settings_mod.load(a.settings)
    body = pathlib.Path(a.body).read_text(encoding="utf-8", errors="replace")
    if a.files_from:
        changed = [x for x in pathlib.Path(a.files_from).read_text().splitlines() if x.strip()]
        edited = ([x for x in pathlib.Path(a.existing_tests_from).read_text().splitlines() if x.strip()]
                  if a.existing_tests_from else [])
    else:
        changed, edited = changed_files(a.base or f"origin/{s['default_branch']}")
    if mode == "size":
        p = size_problem(changed, body, int(s["max_pr_files"]))
        print(f"- {p}" if p else f"pr size: {len(changed)} files, within {s['max_pr_files']}")
        return 1 if p else 0
    problems = check(body, changed, edited, s["work_item_pattern"])
    for p in problems:
        print(f"- {p}")
    print("pr body: " + ("FAIL" if problems else "pass"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
