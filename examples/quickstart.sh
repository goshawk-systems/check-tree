#!/usr/bin/env bash
# A one-minute tour: a throwaway repo, a two-check tree, an approval, a failing score, a fix, a passing score,
# and what happens when someone weakens the locked test. Nothing outside a temporary folder is touched.
# Needs only git and Python 3.9+.
set -euo pipefail
CT="$(cd "$(dirname "$0")/.." && pwd)/bin/check-tree"
DEMO="$(mktemp -d)/demo"
mkdir -p "$DEMO/src/adder" "$DEMO/tests" && cd "$DEMO"
git init -q -b main
git config user.name demo && git config user.email demo@example.invalid

printf 'def add(a, b):\n    return a - b\n' > src/adder/__init__.py
cat > tests/test_add.py <<'PY'
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
from adder import add

got = add(2, 3)
assert got == 5, f"add(2, 3) returned {got}, expected 5"
print("add() is correct")
PY
git add -A && git commit -qm "start: the code, and the defining test (written before the fix)"
TESTS_COMMIT="$(git rev-parse HEAD)"

"$CT" init adder >/dev/null
cat > .check-tree/config.json <<'JSON'
{"base_ref": "main", "remote": null}
JSON
cat > .check-tree/programs/adder/checks.json <<JSON
{
  "program": "adder",
  "objective": "add() returns the sum of two numbers for every caller",
  "areas": ["src/adder/**"],
  "validation": "docs/plans/adder-validation.md",
  "checks": [
    {"id": "objective", "kind": "objective", "title": "the acceptance test passes",
     "run": "python3 tests/test_add.py", "fails_when": "add() is wrong",
     "proven_with": "a test that calls add() the way a caller does",
     "needs": ["h-test"], "frozen": ["tests/test_add.py"], "appetite_hours": 2},
    {"id": "h-test", "kind": "harness", "title": "the acceptance test exists and failed before the fix",
     "run": "test -f tests/test_add.py && \\"\$CHECK_TREE_BIN\\" red-at $TESTS_COMMIT -- python3 tests/test_add.py",
     "fails_when": "the test is missing, or did not fail for a real reason before the fix", "needs": [],
     "frozen": ["tests/test_add.py"], "appetite_hours": 1}
  ]
}
JSON
mkdir -p docs/plans
cat > docs/plans/adder-validation.md <<'MD'
# Plan validation: adder
| Step | True state today | Done-test measures the objective? | Approach holds? | Recommendation |
|---|---|---|---|---|
| make add() correct | built but wrong: add(2, 3) returns -1 (src/adder/__init__.py) | yes: the acceptance test calls add() as a caller does | yes | keep |
MD
echo "== validate"; "$CT" validate adder
echo; echo "== what the owner reads"; "$CT" table adder --owner
"$CT" approve adder --by owner --words "yes, done means the acceptance test passes" >/dev/null
git add -A && git commit -qm "adder: approved check tree"

echo; echo "== score before the fix"; "$CT" run adder || true

printf 'def add(a, b):\n    return a + b\n' > src/adder/__init__.py
git commit -qam "fix add"
echo; echo "== score after the fix"; "$CT" run adder || true

echo; echo "== someone weakens the locked test so it always passes, then the score"
printf 'print("add() is correct")\n' > tests/test_add.py && git commit -qam "weaken the test"
"$CT" score adder

echo; echo "Demo repo left at $DEMO"
