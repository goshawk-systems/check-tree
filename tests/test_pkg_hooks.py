"""The pipeline hooks (prod_guard, secret_guard, other_checkout_guard, pr_open_rules, no_subagent_pr): each shown
refusing what it exists to refuse, with a control beside every refusal that must still pass."""
import json
import os
import stat
import subprocess
import sys

import pytest
from conftest import HOOKS, decision, sh

BASE_ENV = {k: v for k, v in os.environ.items() if k not in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID")}


def call(name, payload, cwd, env=None, bin_dir=None):
    e = dict(BASE_ENV, **(env or {}))
    if bin_dir:
        e["PATH"] = f"{bin_dir}{os.pathsep}{e['PATH']}"
    p = subprocess.run([sys.executable, str(HOOKS / name)], input=json.dumps(payload), capture_output=True,
                       text=True, cwd=str(cwd), env=e)
    assert p.returncode == 0 and "hook error" not in p.stderr, p.stderr
    return json.loads(p.stdout) if p.stdout.strip() else None


def bash(repo, cmd, sub=False, **extra):
    p = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(repo), "session_id": "s1",
         "tool_input": {"command": cmd}, **extra}
    if sub:
        p.update(agent_id="a1")
    return p


def refuses(name, repo, cmd, **kw):
    out = call(name, bash(repo, cmd), repo, **kw)
    assert decision(out) == "deny", cmd
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def allows(name, repo, cmd, **kw):
    assert call(name, bash(repo, cmd), repo, **kw) is None, cmd


def settings(repo, **cfg):
    (repo / ".check-tree" / "repo.json").write_text(json.dumps(cfg))


# ── prod_guard ──

PROD = {"prod": {"db_patterns": ["*prod-db*", "tenant_*_prod"], "aws_profiles": ["acme-prod"],
                 "aws_accounts": ["123456789012"]}}


def test_a_production_deploy_is_refused_by_default_and_other_workflows_pass(repo):
    why = refuses("prod_guard.py", repo, "gh workflow run deploy-prod.yml -f ref=main")
    assert "write the exact command in your report for the owner to run" in why
    refuses("prod_guard.py", repo, "gh workflow run Release-Production -r main")
    refuses("prod_guard.py", repo, "gh api -X POST repos/o/r/actions/workflows/deploy-prod.yml/dispatches -f ref=main")
    allows("prod_guard.py", repo, "gh workflow run deploy-staging.yml -f ref=main")
    allows("prod_guard.py", repo, "gh workflow list")


def test_deploy_patterns_come_from_repo_json(repo):
    settings(repo, prod={"deploy_workflow_patterns": ["ship-it*"]})
    refuses("prod_guard.py", repo, "gh workflow run ship-it.yml")
    allows("prod_guard.py", repo, "gh workflow run deploy-prod.yml")  # replaced, not added to


def test_production_database_writes_are_refused_and_reads_pass(repo):
    settings(repo, **PROD)
    refuses("prod_guard.py", repo, "psql -h prod-db.example.com -d app -c 'DELETE FROM t WHERE id=1'")
    refuses("prod_guard.py", repo, "PGHOST=prod-db.example.com psql -c 'update t set a=1'")
    refuses("prod_guard.py", repo, "psql postgresql://u@prod-db.example.com/app -c 'drop table t'")
    refuses("prod_guard.py", repo, "pg_restore -h prod-db.example.com -d app dump.sql")
    allows("prod_guard.py", repo, "psql -h prod-db.example.com -d app -c 'SELECT count(*) FROM t'")
    allows("prod_guard.py", repo, "psql -h localhost -d app -c 'DELETE FROM t'")
    allows("prod_guard.py", repo, "pg_dump -h prod-db.example.com app")


def test_aws_mutations_in_a_production_account_are_refused_and_reads_pass(repo):
    settings(repo, **PROD)
    refuses("prod_guard.py", repo, "aws ec2 terminate-instances --instance-ids i-1 --profile acme-prod")
    refuses("prod_guard.py", repo, "AWS_PROFILE=acme-prod aws ecs update-service --service s")
    refuses("prod_guard.py", repo, "aws --region us-east-1 --profile acme-prod s3 rm s3://b/k")
    refuses("prod_guard.py", repo, "aws lambda delete-function --function-name arn:aws:lambda:us-east-1:123456789012:function:f")
    refuses("prod_guard.py", repo, "export AWS_PROFILE=acme-prod && aws iam attach-role-policy --role-name r")
    refuses("prod_guard.py", repo, "aws s3 cp x s3://b/x", env={"AWS_PROFILE": "acme-prod"})
    allows("prod_guard.py", repo, "aws ec2 describe-instances --profile acme-prod")
    allows("prod_guard.py", repo, "aws s3 ls s3://b --profile acme-prod")
    allows("prod_guard.py", repo, "aws s3 cp s3://b/k ./k --profile acme-prod")
    allows("prod_guard.py", repo, "aws ec2 terminate-instances --instance-ids i-1 --profile acme-dev")
    allows("prod_guard.py", repo, "env -u AWS_PROFILE aws ec2 stop-instances --instance-ids i-1",
           env={"AWS_PROFILE": "acme-prod"})


def test_with_no_repo_json_only_the_workflow_names_match(repo):
    allows("prod_guard.py", repo, "aws ec2 terminate-instances --instance-ids i-1 --profile acme-prod")
    allows("prod_guard.py", repo, "psql -h prod-db.example.com -c 'delete from t'")
    refuses("prod_guard.py", repo, "gh workflow run deploy-prod.yml")


def test_a_wrongly_typed_setting_falls_back_to_the_default(repo):
    settings(repo, prod={"deploy_workflow_patterns": "deploy-*"})  # a string, not a list
    refuses("prod_guard.py", repo, "gh workflow run deploy-prod.yml")


# ── secret_guard ──

def test_commands_that_print_a_secret_are_refused(repo):
    for cmd in ("cat server.pem", "cat ~/.aws/credentials", "head -5 .env", "less config/secrets.json",
                "sed -n 1p db_password.txt", "base64 < api_token.txt", "grep KEY .env",
                "echo $AWS_SECRET_ACCESS_KEY", "printf '%s' \"${DB_PASSWORD}\"", "printenv GITHUB_TOKEN",
                "env", "printenv", "set", "env -u AWS_PROFILE",
                "aws secretsmanager get-secret-value --secret-id x",
                "aws secretsmanager get-secret-value --secret-id x --query SecretString --output text",
                "aws ssm get-parameter --name /a/b --with-decryption",
                "aws ssm get-parameter --name /a/b --with-decryption --query Parameter.Value",
                "cd /x && cat deploy.key.pem", "bash -c 'cat id.pem'"):
        assert "print a secret" in refuses("secret_guard.py", repo, cmd), cmd


def test_presence_checks_and_ordinary_files_pass(repo):
    for cmd in ("cat README.md", "cat .env.example", "cat src/token_utils.py", "grep -c KEY .env",
                "grep -l KEY .env", "test -f .env", "wc -l server.pem", "env FOO=1 ls", "env -u AWS_PROFILE ls",
                "echo $HOME", "printenv HOME", "[ -n \"$GITHUB_TOKEN\" ] && echo set", "source .env",
                "aws secretsmanager get-secret-value --secret-id x --query ARN",
                "aws ssm get-parameter --name /a/b --with-decryption --query Parameter.Version",
                "aws ssm get-parameter --name /a/b", "cat id_rsa.pub", "echo hello > out.txt"):
        allows("secret_guard.py", repo, cmd)


def test_extra_secret_name_patterns_come_from_repo_json(repo):
    allows("secret_guard.py", repo, "cat keystore.jks")
    settings(repo, secrets={"extra_name_patterns": ["*.jks"]})
    refuses("secret_guard.py", repo, "cat keystore.jks")


# ── other_checkout_guard ──

@pytest.fixture
def other(tmp_path):
    o = tmp_path / "other"
    o.mkdir()
    sh(o, "git", "init", "-q", "-b", "main")
    (o / "f.txt").write_text("x\n")
    return o


def edit(repo, path, tool="Edit"):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "cwd": str(repo), "session_id": "s1",
            "tool_input": {"file_path": str(path)}}


def test_a_listed_shared_checkout_cannot_be_written_to(repo, other):
    settings(repo, shared_checkouts=[str(other)])
    for tool in ("Edit", "Write", "MultiEdit"):
        assert decision(call("other_checkout_guard.py", edit(repo, other / "f.txt", tool), repo)) == "deny"
    for cmd in (f"echo x > {other}/f.txt", f"mv {repo}/README.md {other}/", f"rm {other}/f.txt",
                f"git -C {other} commit -am x", f"cd {other} && git checkout -b b", f"cp README.md {other}/g.txt",
                f"sed -i s/x/y/ {other}/f.txt"):
        assert "own project root" in refuses("other_checkout_guard.py", repo, cmd), cmd


def test_reads_and_the_sessions_own_checkout_are_allowed(repo, other):
    settings(repo, shared_checkouts=[str(other)])
    assert call("other_checkout_guard.py", edit(repo, repo / "src" / "new.py"), repo) is None
    for cmd in (f"cat {other}/f.txt", f"git -C {other} status", f"git -C {other} log -1",
                f"cp {other}/f.txt {repo}/f.txt", f"echo x > {repo}/out.txt", "rm tests/test_add.py",
                "echo hi > /dev/null"):
        allows("other_checkout_guard.py", repo, cmd)


def test_an_unlisted_checkout_is_not_fenced_without_a_lock(repo, other):
    assert call("other_checkout_guard.py", edit(repo, other / "f.txt"), repo) is None


def test_another_sessions_lock_fences_its_checkout_and_your_own_lock_does_not(repo, other):
    (other / ".check-tree").mkdir()
    (other / ".check-tree" / "session.lock").write_text(json.dumps({"session_id": "s2"}))
    assert decision(call("other_checkout_guard.py", edit(repo, other / "f.txt"), repo)) == "deny"
    (other / ".check-tree" / "session.lock").write_text("s1\n")  # this session's id, plain text
    assert call("other_checkout_guard.py", edit(repo, other / "f.txt"), repo) is None


# ── pr_open_rules and no_subagent_pr ──

@pytest.fixture
def gh_bin(tmp_path):
    """A fake `gh` whose `pr list` prints the file named by $FAKE_OPEN_PRS and which fails on anything else."""
    b = tmp_path / "bin"
    b.mkdir()
    g = b / "gh"
    g.write_text('#!/bin/sh\n[ "$1 $2" = "pr list" ] && { cat "$FAKE_OPEN_PRS"; exit 0; }\nexit 1\n')
    g.chmod(g.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "prs.json").write_text("[]")
    return b, {"FAKE_OPEN_PRS": str(tmp_path / "prs.json")}, tmp_path / "prs.json"


@pytest.fixture
def branch(repo):
    sh(repo, "git", "update-ref", "refs/remotes/origin/main", "HEAD")
    sh(repo, "git", "checkout", "-q", "-b", "work")
    (repo / "a.txt").write_text("a\n")
    sh(repo, "git", "add", "a.txt")
    sh(repo, "git", "commit", "-q", "-m", "a")
    return repo


def pr(repo, gh_bin, body, flags="--draft", title="t"):
    b, env, _ = gh_bin
    return call("pr_open_rules.py", bash(repo, f"gh pr create {flags} --title {title} --body '{body}'"), repo,
                env=env, bin_dir=b)


def test_a_pull_request_must_be_a_draft(branch, gh_bin):
    out = pr(branch, gh_bin, "Closes-item: APP-1", flags="")
    assert decision(out) == "deny" and "draft" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert pr(branch, gh_bin, "Closes-item: APP-1", flags="--draft") is None
    assert pr(branch, gh_bin, "Closes-item: APP-1", flags="-d") is None


def test_gh_api_pulls_needs_a_typed_draft_field(branch, gh_bin):
    b, env, _ = gh_bin
    base = "gh api repos/o/r/pulls -f title=t -f head=work -f base=main -f body='Closes-item: APP-2'"
    assert decision(call("pr_open_rules.py", bash(branch, base), branch, env=env, bin_dir=b)) == "deny"
    assert call("pr_open_rules.py", bash(branch, base + " -F draft=true"), branch, env=env, bin_dir=b) is None
    # a GET of the same endpoint is not opening anything
    assert call("pr_open_rules.py", bash(branch, "gh api repos/o/r/pulls"), branch, env=env, bin_dir=b) is None


def test_one_open_pr_per_work_item(branch, gh_bin):
    gh_bin[2].write_text(json.dumps([{"number": 7, "body": "text\nCloses-item: APP-1, APP-9\n"}]))
    out = pr(branch, gh_bin, "Refs-item: APP-1")
    assert decision(out) == "deny" and "#7" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert pr(branch, gh_bin, "Closes-item: APP-3") is None                                    # a different item
    assert pr(branch, gh_bin, "Refs-item: APP-1\nFixes-breakage: the build is red") is None   # escape
    assert pr(branch, gh_bin, "Refs-item: APP-1", title="Revert-the-thing") is None            # a revert


def test_a_pr_with_too_many_files_is_refused(branch, gh_bin):
    settings(branch, max_pr_files=2)
    names = [f"f{i}.txt" for i in range(3)]
    for n in names:
        (branch / n).write_text("x\n")
    sh(branch, "git", "add", *names)
    sh(branch, "git", "commit", "-q", "-m", "more")
    out = pr(branch, gh_bin, "Closes-item: APP-4")
    assert decision(out) == "deny" and "4 files" in out["hookSpecificOutput"]["permissionDecisionReason"]
    settings(branch, max_pr_files=10)
    assert pr(branch, gh_bin, "Closes-item: APP-4") is None


def test_process_work_is_frozen_unless_allowed(branch, gh_bin):
    out = pr(branch, gh_bin, "Closes-item: PROC-12")
    assert decision(out) == "deny" and "frozen" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert pr(branch, gh_bin, "Closes-item: PROC-12\nFixes-breakage: gate is red") is None
    assert pr(branch, gh_bin, "Closes-item: PROC-12, APP-5") is None                           # not only process
    settings(branch, freeze_allowed_items=["PROC-12"])
    assert pr(branch, gh_bin, "Closes-item: PROC-12") is None
    settings(branch, process_prefixes=["OPS"])
    assert decision(pr(branch, gh_bin, "Closes-item: OPS-1")) == "deny"


def test_a_body_that_cannot_be_read_is_refused(branch, gh_bin):
    b, env, _ = gh_bin
    out = call("pr_open_rules.py", bash(branch, "gh pr create --draft --title t --body-file -"), branch,
               env=env, bin_dir=b)
    assert decision(out) == "deny" and "body" in out["hookSpecificOutput"]["permissionDecisionReason"]
    (branch / "body.md").write_text("Closes-item: APP-6\n")
    ok = call("pr_open_rules.py", bash(branch, "gh pr create --draft --title t --body-file body.md"), branch,
              env=env, bin_dir=b)
    assert ok is None


def test_pr_rules_ignore_other_commands_and_a_missing_gh(branch):
    assert call("pr_open_rules.py", bash(branch, "gh pr view 5 --json state"), branch) is None
    assert call("pr_open_rules.py", bash(branch, "git status"), branch) is None
    # gh unavailable: the one-per-item rule cannot be checked and passes
    assert call("pr_open_rules.py", bash(branch, "gh pr create --draft -t t -b 'Closes-item: APP-8'"), branch,
                env={"PATH": "/usr/bin:/bin"}) is None


def test_subagents_still_cannot_open_prs_by_any_spelling(repo):
    for cmd in ("gh pr create --draft -t t", "cd /tmp && gh pr create -d", "GH_TOKEN=x gh pr create --fill",
                "bash -c 'gh pr create -d'", "gh api repos/o/r/pulls -f title=t -f head=h -f base=main",
                "gh api -X POST repos/o/r/pulls -F draft=true"):
        assert decision(call("no_subagent_pr.py", bash(repo, cmd, sub=True), repo)) == "deny", cmd
    for cmd in ("gh pr view 5", "gh api repos/o/r/pulls", "gh api repos/o/r/pulls/5/comments -f body=x",
                "git commit -m 'gh pr create'"):
        assert call("no_subagent_pr.py", bash(repo, cmd, sub=True), repo) is None, cmd
    assert call("no_subagent_pr.py", bash(repo, "gh pr create --draft -t t"), repo) is None  # the lead may


# ── marking a pull request ready needs a green pr-check on its current head ──

FAKE_READY_GH = """#!{py}
import json, os, sys
a = sys.argv[1:]
r = json.load(open(os.environ["FAKE_READY"]))
if a[:2] == ["pr", "view"]:
    out = r.get("pr view")
elif a[:2] == ["api", "graphql"]:
    out = r.get("graphql")
elif a[:1] == ["api"]:
    out = r.get(a[1].split("?")[0])
else:
    out = None
if out is None:
    sys.exit(1)
print(json.dumps(out))
"""
SLUG = "o/r"
HEAD = "abcdef1234567890"
PULL = {"number": 5, "url": f"https://github.com/{SLUG}/pull/5", "headRefOid": HEAD, "title": "Add a thing", "body": "Refs-item: APP-1"}


def checks(*runs):
    return {f"repos/{SLUG}/commits/{HEAD}/check-runs": {"check_runs": [
        {"id": i, "name": "all-green", "status": st, "conclusion": c} for i, (st, c) in enumerate(runs, 1)]}}


@pytest.fixture
def ready_gh(tmp_path):
    b = tmp_path / "rbin"
    b.mkdir()
    g = b / "gh"
    g.write_text(FAKE_READY_GH.format(py=sys.executable))
    g.chmod(g.stat().st_mode | stat.S_IEXEC)
    f = tmp_path / "ready.json"

    def serve(**routes):
        base = {"pr view": PULL, f"repos/{SLUG}/pulls/5": {**PULL, "head": {"sha": HEAD}},
                "graphql": {"data": {"node": PULL}}}
        f.write_text(json.dumps({**base, **routes}))
        return b, {"FAKE_READY": str(f)}

    return serve


def ready(repo, ready_gh, cmd, **routes):
    b, env = ready_gh(**routes)
    return call("pr_open_rules.py", bash(repo, cmd), repo, env=env, bin_dir=b)


def why(out):
    assert decision(out) == "deny", out
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_draft_whose_head_never_passed_pr_check_cannot_be_marked_ready_and_the_refusal_names_the_command(repo, ready_gh):
    text = why(ready(repo, ready_gh, "gh pr ready 5", **checks()))
    assert "gh workflow run pr-check.yml -f pr=5" in text and "abcdef1" in text and "missing" in text


@pytest.mark.parametrize("runs,word", [([("completed", "failure")], "failure"), ([("completed", "skipped")], "skipped"),
                                       ([("in_progress", None)], "pending"),
                                       ([("completed", "success"), ("completed", "failure")], "failure")])
def test_only_a_successful_latest_all_green_on_the_head_lets_it_through(repo, ready_gh, runs, word):
    assert word in why(ready(repo, ready_gh, "gh pr ready 5", **checks(*runs)))


def test_a_head_with_a_successful_all_green_can_be_marked_ready_even_after_an_earlier_failure(repo, ready_gh):
    assert ready(repo, ready_gh, "gh pr ready 5", **checks(("completed", "failure"), ("completed", "success"))) is None
    assert ready(repo, ready_gh, "gh pr ready", **checks(("completed", "success"))) is None          # the current branch's PR
    assert ready(repo, ready_gh, f"gh pr ready 5 -R {SLUG}", **checks(("completed", "success"))) is None


def test_a_commit_status_named_all_green_counts_when_there_is_no_check_run(repo, ready_gh):
    st = {f"repos/{SLUG}/commits/{HEAD}/status": {"statuses": [{"context": "all-green", "state": "success"}]}}
    assert ready(repo, ready_gh, "gh pr ready 5", **checks(), **st) is None
    st = {f"repos/{SLUG}/commits/{HEAD}/status": {"statuses": [{"context": "all-green", "state": "failure"}]}}
    assert "failure" in why(ready(repo, ready_gh, "gh pr ready 5", **{f"repos/{SLUG}/commits/{HEAD}/check-runs": {"check_runs": []}}, **st))


def test_going_back_to_draft_and_other_pr_commands_are_never_refused(repo, ready_gh):
    for cmd in ("gh pr ready 5 --undo", "gh pr view 5", "gh pr checks 5", "gh pr comment 5 -b hi",
                "gh api -X PATCH repos/o/r/pulls/5 -f title=new", "gh api repos/o/r/pulls/5"):
        assert ready(repo, ready_gh, cmd, **checks()) is None, cmd


def test_a_revert_and_a_fixes_breakage_description_are_exempt(repo, ready_gh):
    assert ready(repo, ready_gh, "gh pr ready 5", **checks(), **{"pr view": {**PULL, "title": "Revert the thing"}}) is None
    assert ready(repo, ready_gh, "gh pr ready 5", **checks(), **{"pr view": {**PULL, "body": "x\nThis reverts commit 0d09bfa0ff1234567890abcdef1234567890abcd."}}) is None
    assert ready(repo, ready_gh, "gh pr ready 5", **checks(),
                 **{"pr view": {**PULL, "body": "Refs-item: APP-1\nFixes-breakage: main is red"}}) is None


def test_the_graphql_mutation_and_the_rest_patch_are_held_to_the_same_rule(repo, ready_gh):
    lit = "gh api graphql -f query='mutation{markPullRequestReadyForReview(input:{pullRequestId:\"PR_x\"}){clientMutationId}}'"
    var = ("gh api graphql -f query='mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){clientMutationId}}' "
           "-f id=PR_x")
    rest = "gh api -X PATCH repos/o/r/pulls/5 -F draft=false"
    for cmd in (lit, var, rest, "gh api repos/o/r/pulls/5 --method PATCH -f draft=false"):
        assert "gh workflow run pr-check.yml -f pr=5" in why(ready(repo, ready_gh, cmd, **checks())), cmd
        assert ready(repo, ready_gh, cmd, **checks(("completed", "success"))) is None, cmd
    assert ready(repo, ready_gh, "gh api graphql -f query='query{viewer{login}}'", **checks()) is None


def test_a_mutation_whose_pull_request_cannot_be_read_is_refused(repo, ready_gh):
    cmd = "gh api graphql -f query='mutation($i:MarkPullRequestReadyForReviewInput!){markPullRequestReadyForReview(input:$i){clientMutationId}}'"
    assert "cannot be read" in why(ready(repo, ready_gh, cmd, **checks(("completed", "success"))))


def test_the_ready_rule_passes_when_gh_or_the_api_cannot_answer(repo, ready_gh):
    assert call("pr_open_rules.py", bash(repo, "gh pr ready 5"), repo, env={"PATH": "/usr/bin:/bin"}) is None   # no gh
    assert ready(repo, ready_gh, "gh pr ready 5") is None                          # no check-runs route: the API fails


def test_only_a_real_revert_is_exempt_not_a_description_that_mentions_the_phrase():
    """Goshawk #147 (2026-11-05) explained the exemption in its own description ("or `This reverts commit`") and the ready gate let it
    through untested. A revert is a title starting `Revert` or a line git writes: `This reverts commit <sha>.`"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("por_revert", HOOKS / "pr_open_rules.py")
    import sys as _sys
    _sys.path.insert(0, str(HOOKS))
    try:
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    finally:
        _sys.path.remove(str(HOOKS))
    assert m.is_revert('Revert "add x"', "")
    assert m.is_revert("undo", "Undo it.\n\nThis reverts commit 0d09bfa0ff1234567890abcdef1234567890abcd.\n")
    assert not m.is_revert("PROC-159: ready gate", "- Exempt: a title starting `Revert`, or `This reverts commit`, as before.")
    assert not m.is_revert("x", "This reverts commit nothing in particular")
