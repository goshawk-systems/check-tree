#!/usr/bin/env bash
# A one-minute tour: a throwaway repo, a two-check tree, an approval, a failing score, a fix, a passing score.
# Nothing outside a temporary folder is touched.
set -euo pipefail
CT="$(cd "$(dirname "$0")/.." && pwd)/bin/check-tree"
DEMO="$(mktemp -d)/demo"
mkdir -p "$DEMO/src/adder" "$DEMO/tests" && cd "$DEMO"
git init -q -b main
git config user.name demo && git config user.email demo@example.invalid

printf 'def add(a, b):\n    return a - b\n' > src/adder/__init__.py
cat > tests/test_add.py <<'EOF'
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
from adder import add

def test_add():
    assert add(2, 3) == 5
EOF
git add -A && git commit -qm "start"

"$CT" init adder >/dev/null
cat > .check-tree/config.json <<'EOF'
{"base_ref": "main", "remote": null}
EOF
cat > .check-tree/programs/adder/checks.json <<'EOF'
{
  "program": "adder",
  "objective": "add() returns the sum of two numbers for every caller",
  "areas": ["src/adder/**"],
  "checks": [
    {"id": "objective", "kind": "objective", "title": "the acceptance test passes",
     "run": "python3 -m pytest -q -p no:cacheprovider tests/test_add.py", "fails_when": "add() is wrong",
     "needs": ["h-test"], "frozen": ["tests/test_add.py"], "appetite_hours": 2},
    {"id": "h-test", "kind": "harness", "title": "the acceptance test exists",
     "run": "test -f tests/test_add.py", "fails_when": "the test is missing", "needs": [],
     "frozen": ["tests/test_add.py"], "appetite_hours": 1}
  ]
}
EOF
echo "== validate"; "$CT" validate adder
echo; echo "== what the owner reads"; "$CT" table adder
"$CT" approve adder --by owner --words "yes, done means the acceptance test passes" >/dev/null
git add -A && git commit -qm "adder: approved check tree"
echo; echo "== score before the fix"; "$CT" run adder || true
printf 'def add(a, b):\n    return a + b\n' > src/adder/__init__.py
git commit -qam "fix add"
echo; echo "== score after the fix"; "$CT" run adder
echo; echo "== try to weaken the frozen test, then score"
printf 'def test_add():\n    assert True\n' > tests/test_add.py && git commit -qam "weaken the test"
"$CT" score adder
echo; echo "Demo repo left at $DEMO"
