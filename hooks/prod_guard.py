#!/usr/bin/env python3
"""PreToolUse on Bash: production actions are the owner's to run.

Refused: (1) `gh workflow run` (or a dispatches POST through `gh api`) of a workflow matching
`prod.deploy_workflow_patterns` (default `deploy-prod*`, `*production*`); (2) a write to a production database:
`psql`/`pgcli` naming a host or database in `prod.db_patterns` with SQL that writes, `pg_restore`, `createdb`,
`dropdb` and friends naming one; (3) an `aws` command with a mutating verb (create, delete, put, update, modify,
terminate, run, start, stop, attach, detach, apply, ...) under a profile in `prod.aws_profiles` (`--profile`,
`AWS_PROFILE=` in the command, or the session's AWS_PROFILE) or naming an account id in `prod.aws_accounts`.
Reads (describe, get, list) pass. Settings come from `.check-tree/repo.json`; a missing file means the defaults
above, and with empty lists nothing but the workflow names can match.

Patterns are case-insensitive globs matched against each word of the command and against its pieces split on
`= / @ : ? &`, so a URI or `host=...` is found; write `*prod*` for "contains". The write test on SQL is a keyword
scan of the whole command, so it can refuse a read that mentions the word `update`: the owner runs it.
It cannot see: SQL inside a script file it cannot read, a profile chosen by a wrapper script, a connection
string held in a variable set in another command, or anything run on another machine (`ssh host 'aws ...'`).
"""
from __future__ import annotations

import fnmatch
import os
import re

import _common as C
import _shell as S

REFUSAL = "This is a production action: write the exact command in your report for the owner to run."
AWS_MUTATING = re.compile(r"^(create|delete|put|update|modify|terminate|run|start|stop|attach|detach|apply|"
                          r"remove|revoke|authorize|deregister|register|reboot|restore|import|replace|"
                          r"disable|enable|cancel|release)(-|$)")
AWS_VALUE_FLAGS = {"--profile", "--region", "--output", "--query", "--endpoint-url", "--ca-bundle", "--color",
                   "--cli-read-timeout", "--cli-connect-timeout", "--cli-binary-format", "--cli-input-json"}
S3_ALWAYS_WRITES = {"rm", "mv", "rb", "mb", "sync"}
SQL_WRITE = re.compile(r"\b(insert\s+into|update\s+[\w\".]+\s+set|delete\s+from|drop\s+\w+|alter\s+\w+|create\s+\w+|"
                       r"truncate|grant\s|revoke\s|copy\s+[^;]*\bfrom\b|merge\s+into|vacuum|reindex|refresh\s+materialized)",
                       re.I)
PG_WRITE_TOOLS = {"pg_restore", "createdb", "dropdb", "createuser", "dropuser", "reindexdb", "vacuumdb", "pg_upgrade"}
PG_SQL_TOOLS = {"psql", "pgcli"}


def _matches(patterns: list, words: list) -> bool:
    pieces = set()
    for w in words:
        pieces.add(w)
        pieces.update(p for p in re.split(r"[=/@:?&,\s]", w) if p)
    return any(fnmatch.fnmatch(p.lower(), pat.lower()) for pat in patterns for p in pieces)


def _workflow_name(c: S.Cmd) -> str:
    a = c.argv[1:]
    if a[:2] == ["workflow", "run"]:
        skip, rest = {"-f", "--raw-field", "-F", "--field", "-r", "--ref", "-R", "--repo", "--json"}, a[2:]
        i = 0
        while i < len(rest):
            if rest[i] in skip:
                i += 2
                continue
            if not rest[i].startswith("-"):
                return rest[i]
            i += 1
    if a[:1] == ["api"]:
        m = re.search(r"actions/workflows/([^/\s]+)/dispatches", " ".join(a))
        if m:
            return m.group(1)
    return ""


def _aws_operation(argv: list) -> "tuple[str, str, list]":
    pos, i = [], 1
    while i < len(argv):
        a = argv[i]
        if a in AWS_VALUE_FLAGS:
            i += 1
        elif not a.startswith("-"):
            pos.append(a)
        i += 1
    return (pos[0] if pos else ""), (pos[1] if len(pos) > 1 else ""), pos[2:]


def _aws_profile(c: S.Cmd) -> str:
    for i, a in enumerate(c.argv):
        if a == "--profile" and i + 1 < len(c.argv):
            return c.argv[i + 1]
        if a.startswith("--profile="):
            return a.split("=", 1)[1]
    return c.env.get("AWS_PROFILE", os.environ.get("AWS_PROFILE", ""))


def decide(data: dict) -> "str | None":
    if data.get("tool_name") != "Bash":
        return None
    prod = C.repo_config(data)["prod"]
    command = (data.get("tool_input") or {}).get("command", "")
    for c in S.simple_commands(command, data.get("cwd") or ""):
        words = c.argv + list(c.env.values())
        if c.verb == "gh":
            name = _workflow_name(c)
            if name and _matches(prod["deploy_workflow_patterns"], [name, os.path.splitext(name)[0]]):
                return f"check-tree: `gh workflow run {name}` is a production deploy. {REFUSAL}"
        elif c.verb in PG_WRITE_TOOLS and _matches(prod["db_patterns"], words):
            return f"check-tree: `{c.verb}` against a production database. {REFUSAL}"
        elif c.verb in PG_SQL_TOOLS and _matches(prod["db_patterns"], words) and SQL_WRITE.search(command):
            return f"check-tree: SQL that writes, run against a production database. {REFUSAL}"
        elif c.verb == "aws":
            service, op, rest = _aws_operation(c.argv)
            if service == "s3":
                writes = op in S3_ALWAYS_WRITES or (op == "cp" and rest and str(rest[-1]).startswith("s3://"))
            else:
                writes = bool(AWS_MUTATING.match(op))
            profile = _aws_profile(c)
            on_prod = (profile and profile in prod["aws_profiles"]) or any(
                acct and acct in w for w in words for acct in prod["aws_accounts"])
            if writes and on_prod:
                return f"check-tree: `aws {service} {op}` changes a production account. {REFUSAL}"
    return None


def main(data: dict) -> None:
    reason = decide(data)
    if reason:
        C.deny(reason)


if __name__ == "__main__":
    C.run(main)
