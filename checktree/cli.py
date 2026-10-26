"""check-tree command line.

    check-tree init <program>                      write a starter tree and config
    check-tree validate <program>                  every reason the working copy's tree cannot be approved or run
    check-tree table <program> [--owner]           the tree; --owner prints the plain-words approval sheet
    check-tree red-at <commit> [--expect RE] -- <test command>
                                                   passes if the test FAILED at that commit (not just could not run)
    check-tree approve <program> --by <who> --words "<the owner's words>" [--without-validation]
    check-tree run <program> [--check ID ...]      run checks fresh at the head where passes count
    check-tree score <program>                     the scoreboard (computed; nothing is written by hand)
    check-tree start <program>                     a lead's opening page, in place of a handoff
    check-tree brief <program> <check>             the brief a builder gets for one check
    check-tree land <program> <check>              merge a builder's passing branch into the integration branch
    check-tree card <program> [--days 7]           a short progress summary you can share as is
    check-tree clean <program> [--hours N]         remove builder worktrees that hold no unlanded work
    check-tree progress <program> "<line>"         add one line to the program's progress log
    check-tree list                                every program and its pass count
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

from . import core

STARTER = {
    "program": "",
    "objective": "One sentence, in the owner's words: what is true when this program is done.",
    "areas": ["<the folders this program owns, e.g. src/payments/**>"],
    "validation": "docs/plans/<program>-validation.md",
    "open_for_the_owner": [],
    "dropped": [],
    "checks": [
        {
            "id": "objective",
            "kind": "objective",
            "title": "The outcome the owner cares about, measured where it is used",
            "run": "python3 -m pytest -q -p no:cacheprovider tests/acceptance/test_<ident>.py",
            "fails_when": "The outcome is not true for a real user or caller.",
            "proven_with": "Say in plain words what this is measured against, e.g. the real page in staging, or a fake payment provider in tests.",
            "needs": ["h-acceptance-test"],
            "frozen": ["tests/acceptance/test_<ident>.py"],
            "appetite_hours": 8,
            "plan_text": "",
        },
        {
            "id": "h-acceptance-test",
            "kind": "harness",
            "title": "The acceptance test exists and fails, for a real reason, on the code as it was before the work",
            "run": "test -f tests/acceptance/test_<ident>.py && \"$CHECK_TREE_BIN\" red-at <TESTS-COMMIT> -- python3 -m pytest -q -p no:cacheprovider tests/acceptance/test_<ident>.py",
            "fails_when": "The test is missing, passes before the work (it would prove nothing), or fails only because it cannot run.",
            "needs": [],
            "frozen": ["tests/acceptance/test_<ident>.py"],
            "appetite_hours": 2,
            "plan_text": "",
        },
    ],
}


def _root() -> pathlib.Path:
    return core.repo_root()


def _binding(root: pathlib.Path, program: str) -> tuple[core.Tree, str]:
    cfg = core.load_config(root)
    ref = core.binding_ref(root, cfg)
    tree = core.load_tree(root, program, ref) or core.load_tree(root, program)
    if tree is None:
        sys.exit(f"no tree for {program!r}: expected {core.tree_path(program)}")
    return tree, ref


def cmd_init(a) -> int:
    root = _root()
    path = root / core.tree_path(a.program)
    if path.exists():
        sys.exit(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    ident = re.sub(r"[^A-Za-z0-9_]", "_", a.program)
    tree = json.loads(json.dumps(STARTER).replace("<program>", a.program).replace("<ident>", ident))
    tree["program"] = a.program
    path.write_text(json.dumps(tree, indent=2) + "\n")
    cfg = root / core.CONFIG_PATH
    if not cfg.exists():
        template = core.PKG_ROOT / "templates" / "config.json"
        config = json.loads(template.read_text()) if template.is_file() else dict(core.DEFAULT_CONFIG)
        if core.git(root, "remote", "get-url", "origin")[0] != 0:
            config["remote"] = None  # no remote: read trees and score on the local base branch
        cfg.write_text(json.dumps(config, indent=2) + "\n")
    gi = root / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    for entry in (".check-tree/state/", ".check-tree/worktrees/"):
        if entry not in lines:
            lines.append(entry)
    gi.write_text("\n".join(lines) + "\n")
    print(f"wrote {path.relative_to(root)} and {core.CONFIG_PATH}.\n"
          f"Next (docs/method.md): validate the plan, write and COMMIT the defining tests, put that commit in place "
          f"of <TESTS-COMMIT>, then `check-tree validate {a.program}`.")
    return 0


def cmd_validate(a) -> int:
    root = _root()
    tree = core.load_tree(root, a.program)
    if tree is None:
        sys.exit(f"no tree at {core.tree_path(a.program)}")
    problems = core.validate(tree, root)
    for p in problems:
        print(f"- {p}")
    print("valid" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def owner_sheet(tree: core.Tree) -> str:
    """The approval sheet in plain words: no ids, kinds, paths or hashes."""
    out = [f"# Approval: {tree.program}", "", f"Your objective: \"{tree.objective}\"", "",
           "Read each line and answer yes, no, or change it, in your own words.", ""]
    n = 0
    for c in core.order(tree):
        n += 1
        label = "Before building" if c.get("kind") == "harness" else "Done means"
        line = f"{n}. {label}: {c.get('title', '')}\n   It fails when: {c.get('fails_when', '')}"
        if c.get("proven_with"):
            line += f"\n   Proven with: {c['proven_with']}"
        h = c.get("appetite_hours")
        line += f"\n   Time budget: {h} hour{'' if h == 1 else 's'} (an estimate: change it if you disagree)" \
                f"\n   Your answer: ________"
        out.append(line)
    if tree.data.get("dropped"):
        out += ["", "Dropped from the plan, at your word:"] + [f"- {d}" for d in tree.data["dropped"]]
    if tree.data.get("open_for_the_owner"):
        out += ["", "Questions only you can answer (your answers are recorded with your approval):"]
        out += [f"Q{i}. {q}\n    Your answer: ________" for i, q in enumerate(tree.data["open_for_the_owner"], 1)]
    out += ["", "If any line is a no or a change, it is changed and shown to you again before you approve.",
            "By approving you agree: nothing else counts as done; when a check runs out of time you decide to cut,",
            "reshape or extend it; if the number of passing checks does not move in 7 days, this goes back to the old",
            "way of working."]
    return "\n".join(out)


def cmd_table(a) -> int:
    root = _root()
    tree = core.load_tree(root, a.program)
    if tree is None:
        sys.exit(f"no tree at {core.tree_path(a.program)}")
    if a.owner:
        print(owner_sheet(tree))
        return 0
    print(f"{tree.program}\nObjective: {tree.objective}\nOwns: {', '.join(tree.areas)}\n")
    for c in core.order(tree):
        needs = f" (after {', '.join(c.get('needs', []))})" if c.get("needs") else ""
        print(f"{c['id']} [{c['kind']}, {c.get('appetite_hours')}h]{needs}\n  {c.get('title', '')}\n"
              f"  fails when: {c.get('fails_when', '')}")
        if c.get("frozen"):
            print(f"  defined by (frozen): {', '.join(c['frozen'])}")
    print(f"\nhash: {core.tree_hash(tree.data)}")
    return 0


def cmd_approve(a) -> int:
    root = _root()
    rec = core.approve(root, a.program, a.by, a.words, a.without_validation, a.answer)
    print(f"approved {a.program} ({rec['tree_sha256'][:12]}) by {a.by}: \"{a.words}\"\n"
          f"Commit {core.tree_path(a.program)} to the base branch (with the validation file and config, if not yet "
          f"committed); it binds from there.")
    return 0


def cmd_run(a) -> int:
    root = _root()
    results = core.run_checks(root, a.program, a.check or None)
    failed = 0
    for r in results:
        if "warning" in r:
            print(f"warning: {r['warning']}")
            continue
        print(f"[{r['status'].upper():>7}] {r['check']}" + (f"  ({r.get('seconds')}s)" if "seconds" in r else ""))
        if r["status"] == "fail":
            failed += 1
            print("    " + r.get("output_tail", "").replace("\n", "\n    "))
    tree, _ = _binding(root, a.program)
    print()
    print(core.render_score(core.score(root, a.program, do_fetch=False), tree))
    return 1 if failed else 0


def cmd_score(a) -> int:
    root = _root()
    tree, _ = _binding(root, a.program)
    print(core.render_score(core.score(root, a.program), tree))
    return 0


def cmd_brief(a) -> int:
    root = _root()
    tree, _ = _binding(root, a.program)
    check = tree.check(a.check)
    if check is None:
        sys.exit(f"no check {a.check!r}")
    print(core.brief(tree, check, core.score(root, a.program), None, None, tree.integration_branch))
    return 0


def cmd_start(a) -> int:
    root = _root()
    cfg = core.load_config(root)
    tree, ref = _binding(root, a.program)
    s = core.score(root, a.program)
    print(f"# You are the {a.program} lead\n")
    print("Your job: dispatch one builder per ready check (`Check: "
          f"{a.program}/<id>` in its prompt), run `check-tree run {a.program}` after work lands, land passing "
          "branches, and bring the owner proposed tree changes. You do not edit this program's files.\n")
    print(core.render_score(s, tree))
    if not s.approved:
        print("\n## Only the owner can do this first\n"
              f"The tree is not approved ({'; '.join(s.unapproved_why)}). Show them `check-tree table {a.program} --owner`; "
              f"on their yes: `check-tree approve {a.program} --by owner --words \"<their words>\"`, then commit.")
    if s.ready:
        print("\n## Ready checks")
        for cid in s.ready:
            c = tree.check(cid)
            print(f"- {cid} ({c.get('appetite_hours')}h): {c.get('title', '')}")
            if c.get("plan_text"):
                print(f"    plan: {c['plan_text']}")
    if s.over_appetite:
        print("\n## Over their time budget (the owner decides: cut, reshape or extend)\n- "
              + "\n- ".join(s.over_appetite))
    passed = [r for r in core.read_jsonl(core.state_dir(root, a.program) / "progress.jsonl")
              if "PASSES in the builder's worktree" in r.get("line", "")]
    if passed:
        print("\n## Returned passing, waiting to land")
        for r in passed[-10:]:
            print(f"- {r.get('check')}: `check-tree land {a.program} {r.get('check')}`")
    print(f"\nIntegration branch: {tree.integration_branch} (from {cfg['base_ref']}). Binding tree: {ref}.")
    return 0


def cmd_land(a) -> int:
    root = _root()
    tree, _ = _binding(root, a.program)
    runs = [r for r in core.read_jsonl(core.state_dir(root, a.program) / "builder-runs.jsonl")
            if r.get("check") == a.check and r.get("status") == "pass"]
    if not runs:
        sys.exit(f"no passing builder run for {a.program}/{a.check}")
    last = runs[-1]
    integ = core.ensure_integration_branch(root, tree, core.load_config(root))
    code, cur = core.git(root, "rev-parse", "--abbrev-ref", "HEAD")
    code, out = core.git(root, "merge-base", "--is-ancestor", last["commit"], integ)
    if code == 0:
        print(f"{a.check} is already on {integ}")
        return 0
    wt = pathlib.Path(last["worktree"])
    code, out = core.git(wt, "merge", "--no-edit", "-q", integ)  # bring the builder up to date first
    if code != 0:
        sys.exit(f"the builder's branch does not merge cleanly with {integ}: {out}")
    _, head = core.git(wt, "rev-parse", "HEAD")
    code, out = core.git(root, "fetch", "-q", str(wt), f"HEAD:refs/heads/{integ}") if cur != integ else (1, "")
    if code != 0:
        sys.exit(f"could not move {integ} to {head[:10]} (is it checked out here?): {out}")
    core.progress(root, a.program, f"{a.check}: landed on {integ} at {head[:10]}", check=a.check, commit=head)
    print(f"landed {a.check} on {integ} at {head[:10]}. Open one pull request from {integ} when the milestone "
          f"is proven; run `check-tree run {a.program}` to rescore.")
    return 0


def cmd_red_at(a) -> int:
    import shlex
    args, expect = list(a.cmd), a.expect
    if "--" in args:  # options may sit before the `--` in either order: red-at C --expect X -- cmd
        cut = args.index("--")
        head, args = args[:cut], args[cut + 1:]
        while head:
            opt = head.pop(0)
            if opt == "--expect" and head:
                expect = head.pop(0)
            elif opt.startswith("--expect="):
                expect = opt.split("=", 1)[1]
            else:
                sys.exit(f"red-at: unexpected {opt!r} before `--` (only --expect goes there)")
    elif args and args[0].startswith("--expect"):
        sys.exit("red-at: put `--` between the options and the test command")
    cmd = args[0] if len(args) == 1 else shlex.join(args)
    if not cmd:
        sys.exit("usage: check-tree red-at <commit> -- <test command>")
    ok, why = core.red_at(_root(), a.commit, cmd, expect=expect)
    print(why)
    return 0 if ok else 1


def cmd_card(a) -> int:
    print(core.card(_root(), a.program, a.days))
    return 0


def cmd_clean(a) -> int:
    for line in core.clean_worktrees(_root(), a.program, a.hours):
        print(line)
    return 0


def cmd_progress(a) -> int:
    core.progress(_root(), a.program, a.line)
    return 0


def cmd_list(a) -> int:
    root = _root()
    cfg = core.load_config(root)
    for name in core.list_programs(root, core.binding_ref(root, cfg)):
        s = core.score(root, name, do_fetch=False)
        print(f"{name}: {s.passing}/{s.total}" + ("" if s.approved else " (not approved)")
              + (" STALLED" if s.stalled else ""))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="check-tree", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("init", "validate", "score", "start"):
        sp = sub.add_parser(name)
        sp.add_argument("program")
    sp = sub.add_parser("table")
    sp.add_argument("program")
    sp.add_argument("--owner", action="store_true", help="the plain-words approval sheet for the owner")
    sp = sub.add_parser("red-at")
    sp.add_argument("commit")
    sp.add_argument("--expect", help="a regular expression the failure output must contain (pins the reason)")
    sp.add_argument("cmd", nargs=argparse.REMAINDER)
    sp = sub.add_parser("approve")
    sp.add_argument("program")
    sp.add_argument("--by", required=True)
    sp.add_argument("--words", required=True)
    sp.add_argument("--answer", action="append", default=[],
                    help="the owner's answer to each open question, in order (repeat)")
    sp.add_argument("--without-validation", action="store_true",
                    help="approve a tree that names no plan validation file (shown on the scoreboard)")
    sp = sub.add_parser("run")
    sp.add_argument("program")
    sp.add_argument("--check", action="append")
    for name in ("brief", "land"):
        sp = sub.add_parser(name)
        sp.add_argument("program")
        sp.add_argument("check")
    sp = sub.add_parser("card")
    sp.add_argument("program")
    sp.add_argument("--days", type=int, default=7)
    sp = sub.add_parser("clean")
    sp.add_argument("program")
    sp.add_argument("--hours", type=float, default=24)
    sp = sub.add_parser("progress")
    sp.add_argument("program")
    sp.add_argument("line")
    sub.add_parser("list")
    a = ap.parse_args(argv)
    return globals()[f"cmd_{a.command.replace('-', '_')}"](a)


if __name__ == "__main__":
    sys.exit(main())
