"""Shared by the ops scripts: settings from `.check-tree/repo.json`, a budgeted `gh api` reader, time helpers.

Every script takes a `Gh` object, so tests hand it canned payloads and production hands it the real `gh`.
The call ceiling exists because the GitHub limit (5,000 calls an hour) is shared by every workflow and person."""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

REPO_JSON = ".check-tree/repo.json"
DEFAULT_WORKFLOWS = {"pr_check": "pr-check.yml", "queue_suite": "queue-suite.yml", "master_health": "master-health.yml"}


class BudgetExceeded(RuntimeError):
    """The sweep reached its call ceiling; it stops and says so instead of spending the shared limit."""


def load_settings(root: str | os.PathLike = ".") -> dict:
    path = Path(root) / REPO_JSON
    cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg.setdefault("default_branch", "main")
    cfg.setdefault("product_prefixes", [])
    cfg.setdefault("process_prefixes", [])
    cfg.setdefault("fleet", {}).setdefault("classes", {})
    cfg.setdefault("required_statuses", ["pr-check"])  # the status contexts a PR must have green to merge
    cfg["workflows"] = {**DEFAULT_WORKFLOWS, **(cfg.get("workflows") or {})}
    return cfg


def fleet_classes(cfg: dict) -> dict[str, list[str]]:
    """Runner classes as {class name: [runner labels]} from `fleet.classes` (a dict of name to labels, or to {labels: [...]})."""
    out: dict[str, list[str]] = {}
    for name, v in (cfg.get("fleet", {}).get("classes") or {}).items():
        labels = v.get("labels") if isinstance(v, dict) else v
        if isinstance(labels, str):
            labels = [labels]
        out[name] = [str(x) for x in (labels or [name])]
    return out


def parse_time(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Gh:
    """`gh api` with a call counter and a ceiling. `get` returns parsed JSON, or the raw text for a log."""

    def __init__(self, repo: str, max_calls: int = 150):
        self.repo, self.max_calls, self.calls = repo, max_calls, 0

    def _tick(self) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"stopped at {self.calls} API calls")
        self.calls += 1

    def _run(self, args: list[str], stdin: str | None = None) -> str:
        self._tick()
        p = subprocess.run(["gh", "api", *args], input=stdin, capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            raise RuntimeError(f"gh api {args[0]} failed: {p.stderr.strip()[:300]}")
        return p.stdout

    def get(self, path: str):  # path is complete: /repos/<owner>/<repo>/...
        out = self._run([path])
        try:
            return json.loads(out)
        except ValueError:
            return out

    def post(self, path: str, body: dict | None = None):
        out = self._run(["--method", "POST", path, "--input", "-"], json.dumps(body or {}))
        try:
            return json.loads(out) if out.strip() else {}
        except ValueError:
            return {}


def median(xs: list[float]) -> float | None:
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def p90(xs: list[float]) -> float | None:
    """Nearest-rank 90th percentile."""
    s = sorted(xs)
    if not s:
        return None
    import math
    return s[max(0, math.ceil(0.9 * len(s)) - 1)]
