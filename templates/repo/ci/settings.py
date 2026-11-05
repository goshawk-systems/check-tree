#!/usr/bin/env python3
"""The per-repo settings every check-tree CI script and workflow reads: `.check-tree/repo.json`.

    settings.py get KEY        print one value (a list prints one item per line; a missing key prints its default)
    settings.py json           print every setting, defaults filled in, as JSON

Keys (all optional; a missing file means every default):
  default_branch      the branch the merge queue lands on            (main)
  product_prefixes    work-item prefixes that are product work        ([])      e.g. ["FC", "SIM"]
  process_prefixes    work-item prefixes that are process work        (["PROC"])
  max_pr_files        a PR over this many changed files is refused    (50)
  test_command        runs the tests; test files are appended         (python3 -m pytest -q)
  lint_command        lints; the changed files are appended           (ruff check)
  work_item_pattern   a regex for one work-item id                    ([A-Z][A-Z0-9]*-[0-9]+)
Optional extras, same file:
  setup_command       installs dependencies before a test job         ("")
  tests_dir           where the tests live                            (tests)
  src_roots           source roots stripped from import names         (["src"])
  lint_extensions     file suffixes the lint command is given         ([".py"])
  durations_file      {test file: seconds} for balancing shards       (.check-tree/durations.json)
  shard_target_seconds, shard_max   how many shards a full run is cut into   (300, 16)
Standard library only.
"""
from __future__ import annotations

import json
import pathlib
import sys

SETTINGS_PATH = ".check-tree/repo.json"

DEFAULTS: dict = {
    "default_branch": "main",
    "product_prefixes": [],
    "process_prefixes": ["PROC"],
    "max_pr_files": 50,
    "test_command": "python3 -m pytest -q",
    "lint_command": "ruff check",
    "work_item_pattern": "[A-Z][A-Z0-9]*-[0-9]+",
    "setup_command": "",
    "tests_dir": "tests",
    "src_roots": ["src"],
    "lint_extensions": [".py"],
    "durations_file": ".check-tree/durations.json",
    "shard_target_seconds": 300,
    "shard_max": 16,
}


def load(path: str | pathlib.Path = SETTINGS_PATH) -> dict:
    """The settings file's keys over the defaults. A missing file is all defaults; a broken one is an error."""
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    p = pathlib.Path(path)
    if p.is_file():
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise SystemExit(f"{p}: expected a JSON object")
        out.update({k: v for k, v in data.items() if not k.startswith("_")})
    return out


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    path = SETTINGS_PATH
    if "--settings" in argv:
        i = argv.index("--settings")
        path = argv[i + 1]
        del argv[i:i + 2]
    s = load(path)
    if argv[:1] == ["json"]:
        print(json.dumps(s, indent=2, sort_keys=True))
        return 0
    if len(argv) == 2 and argv[0] == "get":
        if argv[1] not in s:
            print(f"unknown setting: {argv[1]}", file=sys.stderr)
            return 2
        v = s[argv[1]]
        print("\n".join(str(x) for x in v) if isinstance(v, list) else v)
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
