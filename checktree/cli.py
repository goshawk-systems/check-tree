"""check-tree command line.

    check-tree init <program>                      write a starter tree and config
    check-tree validate <program>                  every reason the working copy's tree cannot be approved or run
    check-tree table <program>                     the tree as the owner reads it before approving
    check-tree approve <program> --by <who> --words "<the owner's words>" [--without-validation]
    check-tree run <program> [--check ID ...]      run checks fresh at the head where passes count
    check-tree score <program>                     the scoreboard (computed; nothing is written by hand)
    check-tree start <program>                     a lead's opening page, in place of a handoff
    check-tree brief <program> <check>             the brief a builder gets for one check
    check-tree land <program> <check>              merge a builder's passing branch into the integration branch
    check-tree clean <program> [--hours N]         remove builder worktrees that hold no unlanded work
    check-tree progress <program> "<line>"         add one line to the program's progress log
    check-tree list                                every program and its pass count
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from . import core

STARTER = {
    "program": "",
    "objective": "One sentence, in the owner's words: what is true when this program is done.",
    "areas": ["src/<program>/**"],
    "validation": "docs/plans/<program>-validation.md",
    "checks": [
        {
            "id": "objective",
            "kind": "objective",
            "title": "The outcome the owner cares about, measured where it is used",
            "run": "python3 -m pytest -q tests/acceptance/test_<program>.py",
            "fails_when": "The outcome is not true for a real user or caller.",
            "needs": ["harness-acceptance-test"],
            "frozen": [],
            "appetite_hours": 8,
            "plan_text": "",
        },
        {
            "id": "harness-acceptance-test",
            "kind": "harness",
            "title": "The acceptance test exists and fails on today's code for the right reason",
            "run": "python3 -m pytest -q tests/acceptance/test_<program>.py; test $? -ne 0",
            "fails_when": "The test is missing, or passes before the work is done (it would prove nothing).",
            "needs": [],
            "frozen": [],
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
    tree = json.loads(json.dumps(STARTER).replace("<program>", a.program))
    tree["program"] = a.program
    path.write_text(json.dumps(tree, indent=2) + "\n")
    cfg = root / core.CONFIG_PATH
    if not cfg.exists():
        cfg.write_text(json.dumps(core.DEFAULT_CONFIG, indent=2) + "\n")
    gi = root / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    for entry in (".check-tree/state/", ".check-tree/worktrees/"):
        if entry not in lines:
            lines.append(entry)
    gi.write_text("\n".join(lines) + "\n")
    print(f"wrote {path.relative_to(root)}; edit it, then `check-tree validate {a.program}`")
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


def cmd_table(a) -> int:
    root = _root()
    tree = core.load_tree(root, a.program)
    if tree is None:
        sys.exit(f"no tree at {core.tree_path(a.program)}")
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
    rec = core.approve(root, a.program, a.by, a.words, a.without_validation)
    print(f"approved {a.program} ({rec['tree_sha256'][:12]}) by {a.by}: \"{a.words}\"\n"
          f"Commit {core.tree_path(a.program)} to the base branch; it binds from there.")
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
              f"The tree is not approved ({'; '.join(s.unapproved_why)}). Show them `check-tree table {a.program}`; "
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
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("init", "validate", "table", "score", "start"):
        sp = sub.add_parser(name)
        sp.add_argument("program")
    sp = sub.add_parser("approve")
    sp.add_argument("program")
    sp.add_argument("--by", required=True)
    sp.add_argument("--words", required=True)
    sp.add_argument("--without-validation", action="store_true",
                    help="approve a tree that names no plan validation file (shown on the scoreboard)")
    sp = sub.add_parser("run")
    sp.add_argument("program")
    sp.add_argument("--check", action="append")
    for name in ("brief", "land"):
        sp = sub.add_parser(name)
        sp.add_argument("program")
        sp.add_argument("check")
    sp = sub.add_parser("clean")
    sp.add_argument("program")
    sp.add_argument("--hours", type=float, default=24)
    sp = sub.add_parser("progress")
    sp.add_argument("program")
    sp.add_argument("line")
    sub.add_parser("list")
    a = ap.parse_args(argv)
    return globals()[f"cmd_{a.cmd}"](a)


if __name__ == "__main__":
    sys.exit(main())
