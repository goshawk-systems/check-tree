"""Shell-command reading shared by the pack's Bash hooks (prod_guard, secret_guard, other_checkout_guard,
pr_open_rules, no_subagent_pr).

Not a shell. It splits a command line into simple commands (on ; && || | & and parentheses), takes redirects out,
strips env assignments and wrappers (env, nice, nohup, time, sudo, timeout), follows `cd` and `export`, and
reads inside `bash -c '...'` and `eval`. It cannot see: variables other than those assigned earlier in the same
command, command substitution results, a script or interpreter's own behaviour (`python x.py`, `make`), a
heredoc's body, or anything run on another machine (`ssh host '...'`). Each hook's docstring says what that
means for it.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
from dataclasses import dataclass, field
from typing import Optional

ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
REDIRECTS = {">", ">>", ">|", "&>", "&>>", "<", "<<<"}


@dataclass
class Cmd:
    argv: list
    env: dict = field(default_factory=dict)       # assignments in force: prefixes, `export`, `env -u X` (value "")
    cwd: str = ""                                  # where it runs, following `cd`
    redirects: list = field(default_factory=list)  # [(">", "path"), ...]
    piped_out: bool = False
    bare_env: bool = False                         # `env` / `env -u X` with no command: prints the environment

    @property
    def verb(self) -> str:
        return os.path.basename(self.argv[0]) if self.argv else ""


def _strip_heredocs(text: str) -> str:
    return re.sub(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n[ \t]*\2[ \t]*(?=\n|$)", " ", text, flags=re.S)


def _newlines_to_semicolons(text: str) -> str:
    out, quote, i = [], "", 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text) and not quote:
            out.append(" " if text[i + 1] == "\n" else text[i:i + 2])
            i += 2
            continue
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == "\n":
            c = ";"
        out.append(c)
        i += 1
    return "".join(out)


def _tokens(text: str) -> list:
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=True)
        lex.whitespace_split, lex.commenters = True, ""
        return list(lex)
    except ValueError:
        return text.split()


def _is_sep(tok: str) -> bool:
    return bool(tok) and all(c in ";&|()" for c in tok)


def _strip_wrappers(argv: list, env: dict) -> "tuple[list, bool]":
    """Drop leading assignments and wrappers, recording env changes. Returns (command argv, bare_env)."""
    argv, bare = list(argv), False
    while argv:
        w, base = argv[0], os.path.basename(argv[0])
        if ASSIGN.match(w):
            k, v = w.split("=", 1)
            env[k] = v
            argv.pop(0)
        elif base == "env":
            argv.pop(0)
            bare = True
            while argv and (argv[0].startswith("-") or ASSIGN.match(argv[0])):
                a = argv.pop(0)
                if a in ("-u", "--unset") and argv:
                    env[argv.pop(0)] = ""
                elif ASSIGN.match(a):
                    env[a.split("=", 1)[0]] = a.split("=", 1)[1]
            if argv:
                bare = False
        elif base == "nice":
            argv.pop(0)
            if argv and argv[0] == "-n":
                del argv[:2]
            elif argv and re.match(r"^-\d+$", argv[0]):
                argv.pop(0)
        elif base == "timeout":
            argv.pop(0)
            while argv and argv[0].startswith("-"):
                argv.pop(0)
            if argv:
                argv.pop(0)
        elif base == "sudo":
            argv.pop(0)
            while argv and argv[0].startswith("-"):
                a = argv.pop(0)
                if a in ("-u", "-g") and argv:
                    argv.pop(0)
        elif base in ("nohup", "time", "command", "exec", "builtin"):
            argv.pop(0)
        else:
            break
    return argv, bare and not argv


def expand(path: str, cwd: str = "") -> str:
    p = os.path.expandvars(os.path.expanduser(path))
    return p if os.path.isabs(p) else os.path.join(cwd or os.getcwd(), p)


def simple_commands(command: str, cwd: str = "", env: Optional[dict] = None, depth: int = 0) -> list:
    """Every simple command in a command line, in order, as Cmd objects."""
    state = {"cwd": cwd or os.getcwd(), "env": dict(env or {})}
    out, toks = [], _tokens(_newlines_to_semicolons(_strip_heredocs(command)))
    seg, redirs, i = [], [], 0

    def flush(sep: str) -> None:
        nonlocal seg, redirs
        words, rd = seg, redirs
        seg, redirs = [], []
        if not words and not rd:
            return
        local = dict(state["env"])
        argv, bare = _strip_wrappers(words, local)
        if not argv and not bare:  # bare assignments: they persist for later commands in this line
            state["env"].update(local)
            return
        c = Cmd(argv=argv, env=local, cwd=state["cwd"], redirects=rd, piped_out=sep in ("|", "|&"), bare_env=bare)
        verb = c.verb
        if verb == "export":
            state["env"].update({a.split("=", 1)[0]: a.split("=", 1)[1] for a in argv[1:] if ASSIGN.match(a)})
        elif verb == "cd" and len(argv) > 1 and not argv[1].startswith("-"):
            state["cwd"] = expand(argv[1], state["cwd"])
        elif verb in SHELLS and "-c" in argv[1:] and depth < 3 and argv.index("-c") + 1 < len(argv):
            out.extend(simple_commands(argv[argv.index("-c") + 1], state["cwd"], local, depth + 1))
            return
        elif verb == "eval" and depth < 3:
            out.extend(simple_commands(" ".join(argv[1:]), state["cwd"], local, depth + 1))
            return
        out.append(c)

    while i < len(toks):
        t = toks[i]
        if _is_sep(t):
            flush(t)
        elif t in REDIRECTS and i + 1 < len(toks):
            if seg and re.fullmatch(r"\d", seg[-1]) and t.startswith(">"):
                seg.pop()  # the `2` of `2>file`
            redirs.append((t, toks[i + 1]))
            i += 1
        elif t.startswith((">&", "<&")):
            i += 1  # `2>&1`: a file-descriptor copy, not a file
        else:
            seg.append(t)
        i += 1
    flush(";")
    return out


# ── gh pull-request opening, read once for every hook that cares ──

@dataclass
class PrCall:
    kind: str                       # "create" or "api"
    cwd: str
    draft: bool = False
    title: str = ""
    body: str = ""
    base: str = ""
    fill: bool = False
    opaque: bool = False            # the body comes from stdin or an unreadable file: it cannot be checked


_CREATE_VALUE = {"-t": "title", "--title": "title", "-b": "body", "--body": "body", "-F": "body_file",
                 "--body-file": "body_file", "-B": "base", "--base": "base"}
_CREATE_OTHER = {"-H", "--head", "-a", "--assignee", "-l", "--label", "-m", "--milestone", "-p", "--project",
                 "-r", "--reviewer", "-R", "--repo", "-T", "--template", "--recover"}
_API_VALUE = {"-X", "--method", "-f", "--raw-field", "-F", "--field", "-H", "--header", "--input", "-q", "--jq",
              "-t", "--template", "--hostname", "--cache", "-p", "--preview"}


def _read(path: str, cwd: str) -> Optional[str]:
    if path == "-":
        return None
    try:
        return pathlib.Path(expand(path, cwd)).read_text()
    except OSError:
        return None


def _parse_create(args: list, cwd: str) -> PrCall:
    call, vals, i = PrCall("create", cwd), {}, 0
    while i < len(args):
        a, val = args[i], None
        if a.startswith("--") and "=" in a:
            a, val = a.split("=", 1)
        if a in ("-d", "--draft"):
            call.draft = val not in ("false", "0")
        elif a in ("-f", "--fill", "--fill-first", "--fill-verbose"):
            call.fill = True
        elif a in _CREATE_VALUE or a in _CREATE_OTHER:
            if val is None and i + 1 < len(args):
                val, i = args[i + 1], i + 1
            if a in _CREATE_VALUE and val is not None:
                vals[_CREATE_VALUE[a]] = val
        i += 1
    call.title, call.base, call.body = vals.get("title", ""), vals.get("base", ""), vals.get("body", "")
    if "body_file" in vals:
        text = _read(vals["body_file"], cwd)
        call.opaque, call.body = text is None, text or call.body
    return call


def _parse_api(args: list, cwd: str) -> Optional[PrCall]:
    """A POST to .../pulls. In `gh api`, -f/-F are fields (-F is typed, `@file` reads a file), never a body file."""
    method, endpoint, fields, has_params, i, opaque = "", "", {}, False, 0, False
    while i < len(args):
        a, val = args[i], None
        if a.startswith("--") and "=" in a:
            a, val = a.split("=", 1)
        if a in _API_VALUE:
            if val is None and i + 1 < len(args):
                val, i = args[i + 1], i + 1
            val = val or ""
            if a in ("-X", "--method"):
                method = val.upper()
            elif a in ("-f", "--raw-field", "-F", "--field") and "=" in val:
                k, v = val.split("=", 1)
                if a in ("-F", "--field") and v.startswith("@"):
                    text = _read(v[1:], cwd)
                    opaque, v = opaque or text is None, text or ""
                fields[k], has_params = v, True
            elif a == "--input":
                has_params = True
                try:
                    fields.update({k: str(v).lower() if isinstance(v, bool) else v
                                   for k, v in json.loads(_read(val, cwd) or "").items()})
                except (ValueError, AttributeError):
                    opaque = True
        elif not a.startswith("-") and not endpoint:
            endpoint = a
        i += 1
    if not re.search(r"(^|/)pulls/?$", endpoint.split("?")[0]) or (method or ("POST" if has_params else "GET")) != "POST":
        return None
    return PrCall("api", cwd, draft=str(fields.get("draft", "")).lower() == "true", title=str(fields.get("title", "")),
                  body=str(fields.get("body", "")), base=str(fields.get("base", "")), opaque=opaque)


def pr_open_calls(command: str, cwd: str = "") -> list:
    """Every `gh pr create` and every POST to a repo's pulls endpoint (`gh api`) in a command line."""
    out = []
    for c in simple_commands(command, cwd):
        if c.verb != "gh":
            continue
        rest = c.argv[1:]
        if rest[:2] == ["pr", "create"]:
            out.append(_parse_create(rest[2:], c.cwd))
        elif rest[:1] == ["api"]:
            call = _parse_api(rest[1:], c.cwd)
            if call:
                out.append(call)
    return out
