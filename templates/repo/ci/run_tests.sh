#!/usr/bin/env bash
# Run the repo's test command (`test_command` in .check-tree/repo.json) on the given test files, or bare when none are given.
#   ci/run_tests.sh [file ...]
set -euo pipefail
cmd="$(python3 "$(dirname "$0")/settings.py" get test_command)"
setup="$(python3 "$(dirname "$0")/settings.py" get setup_command)"
if [ -n "${CHECK_TREE_SETUP:-}" ] && [ -n "$setup" ]; then bash -c "$setup"; fi
exec bash -c "$cmd \"\$@\"" _ "$@"
