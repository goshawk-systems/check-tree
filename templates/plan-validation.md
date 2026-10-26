# Plan validation: <program>

Objective (owner's words): "<verbatim>"
Plan read: <path or link, and its version or commit>
Validated by: <who>, <date>

One row per open plan step. Every cell that states a fact names its source.

| Step | True state today (source) | Does its done-test still measure the objective? (why) | Does the approach hold? (internal + external source) | Recommendation |
|---|---|---|---|---|
| 1. <plan's words> | partly built: `src/x.py` handles A not B (commit abc123) | No: it checks the API returns 200, not that the user sees the receipt | Holds: same pattern works in `src/y.py`; vendor docs confirm (link) | Keep; change done-test to the end-to-end receipt check |
| 2. ... | | | | |

## What holds
- ...

## What should change, and why
- ...

## Proposed order
1. ...

## Questions only the owner can answer
- ...
