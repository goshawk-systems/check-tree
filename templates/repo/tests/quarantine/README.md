# The flaky-test quarantine: one file per entry

A known-flaky test may stop blocking for at most 48 hours while it has an entry here, and at most 5 entries live at once.
The test still RUNS on every gate; only its failure stops failing the run. A quarantined test must be fixed or deleted
by `expires`. `quarantine.yml` proposes entries daily, from the merge queue's own evidence, as a DRAFT pull request;
a person reads the evidence and decides. A test whose failure reproduces is never proposed.

One entry, one file `<id>.json` (`id` is the file name without `.json`), so two pull requests never conflict:

```json
{"id": "test-checkout-total", "node": "tests/test_checkout.py::test_total", "tracker": null,
 "reason": "failed in a queue run and passed in another run holding the identical PR heads",
 "evidence": "failed run 111, passed run 222", "run_sha": "abc1234",
 "added": "2026-11-05T12:00:00Z", "expires": "2026-11-07T12:00:00Z"}
```

## Mark quarantined tests xfail until they expire

Add to the repository's `conftest.py` (non-strict: a failure is XFAIL and the run stays green; a pass is XPASS, meaning
the test is fixed and the entry can be deleted). An expired entry is ignored, so that test fails like any other.

```python
import datetime as dt
import json
import pathlib

import pytest


def pytest_collection_modifyitems(config, items):
    now = dt.datetime.now(dt.timezone.utc)
    entries = {}
    for p in pathlib.Path(__file__).parent.glob("tests/quarantine/*.json"):
        e = json.loads(p.read_text())
        if dt.datetime.fromisoformat(e["expires"].replace("Z", "+00:00")) > now:
            entries[e["node"]] = e
    for item in items:
        e = entries.get(item.nodeid)
        if e:
            item.add_marker(pytest.mark.xfail(strict=False, reason=f"quarantined until {e['expires']}: {e['reason']}"))
```
