#!/usr/bin/env python3
"""Cut the full test run into balanced shards.

    shards.py matrix                  print `matrix={"shard":[1,2,..]}` and `total=N` (GitHub Actions output lines)
    shards.py files --shard I/N       print the test files of shard I of N, one per line
    shards.py discovered              print how many test files were found (0: the test command is run bare, on shard 1)

The count comes from the durations file (`{test file: seconds}`, `durations_file` in the settings) when it exists:
ceil(total seconds / shard_target_seconds), at most shard_max. Without one it is min(4, test files). Files are dealt out
longest first to the lightest shard; a file with no recorded time is priced at the mean of those that have one.
Standard library only.
"""
from __future__ import annotations

import json
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import settings as settings_mod  # noqa: E402

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "build", "dist", ".tox"}


def test_files(repo: pathlib.Path, tests_dir: str) -> list[str]:
    base = repo / tests_dir
    if not base.is_dir():
        return []
    out = []
    for p in base.rglob("*.py"):
        rel = p.relative_to(repo)
        if SKIP_DIRS & set(rel.parts):
            continue
        if p.name.startswith("test_") or p.name.endswith("_test.py"):
            out.append(rel.as_posix())
    return sorted(out)


def durations(repo: pathlib.Path, path: str) -> dict[str, float]:
    p = repo / path
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(v, (int, float)) and v >= 0} if isinstance(data, dict) else {}


def shard_count(files: list[str], known: dict[str, float], target: float, maximum: int) -> int:
    if not files:
        return 1
    if not known:
        return max(1, min(4, len(files)))
    prices = priced(files, known)
    return max(1, min(maximum, len(files), math.ceil(sum(prices.values()) / max(target, 1.0))))


def priced(files: list[str], known: dict[str, float]) -> dict[str, float]:
    have = [known[f] for f in files if f in known]
    mean = (sum(have) / len(have)) if have else 1.0
    return {f: known.get(f, mean) for f in files}


def partition(files: list[str], known: dict[str, float], total: int) -> list[list[str]]:
    prices = priced(files, known)
    bins: list[list[str]] = [[] for _ in range(total)]
    load = [0.0] * total
    for f in sorted(files, key=lambda f: (-prices[f], f)):
        i = min(range(total), key=lambda k: (load[k], k))
        bins[i].append(f)
        load[i] += prices[f]
    return [sorted(b) for b in bins]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    s = settings_mod.load()
    repo = pathlib.Path(".")
    files = test_files(repo, s["tests_dir"])
    known = durations(repo, s["durations_file"])
    n = shard_count(files, known, float(s["shard_target_seconds"]), int(s["shard_max"]))
    if cmd == "matrix":
        print("matrix=" + json.dumps({"shard": list(range(1, n + 1))}))
        print(f"total={n}")
        return 0
    if cmd == "discovered":
        print(len(files))
        return 0
    if cmd == "files" and len(argv) == 3 and argv[1] == "--shard":
        i, total = (int(x) for x in argv[2].split("/"))
        if not 1 <= i <= total:
            raise SystemExit("--shard I/N needs 1 <= I <= N")
        parts = partition(files, known, total)
        if parts[i - 1]:  # an empty shard prints nothing (a blank line reads as one empty file name)
            print("\n".join(parts[i - 1]))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
