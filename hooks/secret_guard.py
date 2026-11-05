#!/usr/bin/env python3
"""PreToolUse on Bash: a command that would print a secret into the transcript is refused.

Refused: reading a secret-shaped file with a command that shows its content (cat, head, tail, less, sed, awk,
base64, grep without -c/-l/-q, ... or `< file`); a bare `env`, `printenv`, `set`, `export -p`; `echo`/`printf`/
`printenv` of a variable named like a secret; `aws secretsmanager get-secret-value` unless a `--query` masks the
value; `aws ssm get-parameter(s)` with `--with-decryption` unless a `--query` leaves the Value out; `aws configure
get` of a secret key. A secret-shaped file name is `*.pem`, `*secret*`, `*password*`, `*token*`, `.env`, `.env.*`,
`.pgpass`, `.netrc`, `.aws/credentials`, `id_rsa`-style keys, plus `secrets.extra_name_patterns` from
`.check-tree/repo.json`; source files (.py, .js, .md, ...), `.env.example` and public keys (.pub) are not.
A variable is secret-shaped when its name contains SECRET, PASSWORD, PASSWD, TOKEN, API_KEY, PRIVATE_KEY or
CREDENTIAL.

The reason a refusal gives is what to do instead: check presence or shape, never the value.
It cannot see: a script or `python -c` that prints a secret, the content of a variable another command set,
a secret reached through a symlink with an innocent name, or output captured by a command's own logging.
"""
from __future__ import annotations

import fnmatch
import os
import re

import _common as C
import _shell as S

READ_VERBS = {"cat", "less", "more", "head", "tail", "bat", "strings", "xxd", "base64", "awk", "sed", "cut", "sort",
              "uniq", "tr", "od", "hexdump", "nl", "tac", "rev", "grep", "egrep", "fgrep", "rg", "jq", "yq"}
GREP_SAFE = {"-c", "--count", "-l", "--files-with-matches", "-L", "--files-without-match", "-q", "--quiet"}
SECRET_FILE_GLOBS = ["*.pem", "*secret*", "*password*", "*passwd*", "*token*", ".env", ".env.*", ".pgpass",
                     ".netrc", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"]
EXEMPT_FILE_GLOBS = [".env.example", ".env.sample", ".env.template", "*.pub", "*.py", "*.js", "*.ts", "*.tsx",
                     "*.jsx", "*.md", "*.rst", "*.go", "*.rs", "*.java", "*.rb", "*.html", "*.css", "*.c", "*.h",
                     "*.cpp", "*.sh", "*.sql"]
SECRET_VAR = re.compile(r"(?i)(secret|password|passwd|token|api_?key|private_?key|credential)")
HOW = ("check-tree: this would print a secret into the transcript. Check only that it exists or its shape "
       "(`test -f FILE`, `wc -l FILE`, `grep -c PATTERN FILE`, `[ -n \"$VAR\" ] && echo set`), or use the tool "
       "that consumes it without printing it.")


def is_secret_file(path: str, extra: list) -> bool:
    base = os.path.basename(path.rstrip("/")).lower()
    parent = os.path.basename(os.path.dirname(path.rstrip("/")))
    if base == "credentials" and parent == ".aws":
        return True
    if any(fnmatch.fnmatch(base, g) for g in EXEMPT_FILE_GLOBS):
        return False
    return any(fnmatch.fnmatch(base, g.lower()) for g in SECRET_FILE_GLOBS + list(extra))


def _flag_value(argv: list, flag: str) -> "str | None":
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def _check(c: S.Cmd, extra: list) -> "str | None":
    for op, target in c.redirects:
        if op in ("<", "<<<") and is_secret_file(target, extra):
            return HOW
    v, args = c.verb, c.argv[1:]
    if c.bare_env or (v in ("env", "printenv") and not [a for a in args if not a.startswith("-")]):
        return HOW
    if v in ("set", "declare", "typeset") and not args or (v in ("export", "declare") and args == ["-p"]):
        return HOW
    if v == "printenv" and any(SECRET_VAR.search(a) for a in args):
        return HOW
    if v in ("echo", "printf") and any(SECRET_VAR.search(m) for a in args for m in re.findall(r"\$\{?(\w+)", a)):
        return HOW
    if v in READ_VERBS:
        if v in ("grep", "egrep", "fgrep", "rg") and any(a in GREP_SAFE for a in args):
            return None
        if any(is_secret_file(a, extra) for a in args if not a.startswith("-")):
            return HOW
    if v == "aws":
        flat = " ".join(c.argv)
        query = _flag_value(c.argv, "--query")
        if "secretsmanager get-secret-value" in flat and (
                query is None or re.search(r"SecretString|SecretBinary|^[@*]$", query)):
            return HOW + " (`aws secretsmanager get-secret-value` needs a --query that leaves the secret out.)"
        if re.search(r"ssm get-parameters?(-by-path)?\b", flat) and "--with-decryption" in c.argv and (
                query is None or "Value" in query):
            return HOW + " (`--with-decryption` needs a --query that leaves Value out.)"
        if "configure get" in flat and SECRET_VAR.search(flat.split("configure get", 1)[1]):
            return HOW
    return None


def main(data: dict) -> None:
    if data.get("tool_name") != "Bash":
        return
    extra = C.repo_config(data)["secrets"]["extra_name_patterns"]
    for c in S.simple_commands((data.get("tool_input") or {}).get("command", ""), data.get("cwd") or ""):
        reason = _check(c, extra)
        if reason:
            C.deny(reason)
            return


if __name__ == "__main__":
    C.run(main)
