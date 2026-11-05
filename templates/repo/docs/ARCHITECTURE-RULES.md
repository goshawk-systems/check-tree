# Architecture rules: {{repo_name}}

A skeleton. Fill it in once, keep it short, and tag every rule with the checker that enforces it. A rule with no
checker is a wish: delete it, or write the checker. Violations that already exist go on a shrink-only baseline
(`ratchet.py`), never on a waiver.

## 1. Layers

Replace the example rows with your real folders. A layer may import only what its row lists.

| Layer | Folder | Holds | May import |
|---|---|---|---|
| Entry points | `src/api/` | request parsing, auth, one call into a service, response shaping | services, core |
| Services | `src/services/` | business rules and workflows | data, core, integrations |
| Data access | `src/data/` | every query | models, core |
| Integrations | `src/integrations/` | outside systems, one folder per vendor | core |
| Core | `src/core/` | settings, logging, clock, errors | nothing above it |

## 2. The one approved way

One way to do each recurring thing. Before writing a new helper, search for the row; add a row only with the owner.

| Thing | The one approved way | Checker |
|---|---|---|
| Time | the clock module in core; never read the wall clock directly | ratchet rule `no-direct-clock` |
| Configuration | one settings module; no direct environment reads | ratchet rule `no-direct-env` |
| Logging | the logger from core; no `print` in application code | ratchet rule `no-print` |
| Errors | the shared error types in core | ratchet rule `no-bare-except` |
| Tests against real systems | marked `live`; refused otherwise | `conftest.py` |

## 3. Rules

1. Entry points never touch the database. [CI: ratchet.py rule `no-db-in-entry-points`]
2. Core never imports upward. [CI: ratchet.py rule `no-upward-import-in-core`]
3. No import cycles; the count only goes down. [CI: ratchet.py rule `import-cycles`]
4. A function over 150 lines or a file over 1,500 lines needs a stated reason. [CI: ratchet.py rule `size`]
5. One generation of any module at a time: when a new version ships, delete the old one. [prose]

## 4. How to add a rule

Write the regular expression, seed today's count with `ratchet.py seed <name>`, commit the rule and its baseline in
one pull request. The count can then only go down.
