"""The guardrail hooks: each shown refusing what it exists to refuse, and allowing everything else."""
from conftest import decision, hook, write_transcript


def bash(repo, cmd, sub=True):
    p = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": cmd}}
    if sub:
        p.update(agent_id="a1", agent_type="general-purpose")
    return p


# ── required_reads ──

def test_code_changes_wait_for_the_required_reading(repo, tmp_path):
    (repo / "CLAUDE.md").write_text("# rules\n```required-reads\ndocs/DESIGN.md\nREADME.md\n```\n")
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "DESIGN.md").write_text("design\n")
    t = write_transcript(tmp_path / "s.jsonl", "change add()", reads=[repo / "README.md"])
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Edit", "cwd": str(repo), "transcript_path": t,
               "tool_input": {"file_path": str(repo / "src" / "demo" / "__init__.py")}}
    out = hook("required_reads.py", payload, repo)
    assert decision(out) == "deny" and "docs/DESIGN.md" in out["hookSpecificOutput"]["permissionDecisionReason"]
    write_transcript(tmp_path / "s.jsonl", "change add()", reads=[repo / "README.md", repo / "docs" / "DESIGN.md"])
    assert hook("required_reads.py", payload, repo) is None


def test_no_required_reads_block_means_no_gate(repo, tmp_path):
    t = write_transcript(tmp_path / "s.jsonl", "x")
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Write", "cwd": str(repo), "transcript_path": t,
               "tool_input": {"file_path": str(repo / "x.py")}}
    assert hook("required_reads.py", payload, repo) is None


# ── agent_cap ──

def test_the_agent_cap_refuses_one_more_and_frees_on_stop(repo):
    (repo / ".check-tree" / "config.json").write_text('{"base_ref": "main", "remote": null, "guards": {"agent_cap": 2}}')
    base = {"cwd": str(repo), "session_id": "s1"}
    for a in ("a1", "a2"):
        hook("agent_cap.py", {**base, "hook_event_name": "SubagentStart", "agent_id": a}, repo)
    dispatch = {**base, "hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": {"prompt": "x"}}
    assert decision(hook("agent_cap.py", dispatch, repo)) == "deny"
    hook("agent_cap.py", {**base, "hook_event_name": "SubagentStop", "agent_id": "a1"}, repo)
    assert hook("agent_cap.py", dispatch, repo) is None


# ── no_ci_polling ──

def test_subagents_do_not_watch_ci(repo):
    for cmd in ("gh run watch 123", "gh pr checks 5 --watch", "watch -n 30 gh pr checks 5",
                "while true; do gh run list; sleep 60; done"):
        assert decision(hook("no_ci_polling.py", bash(repo, cmd), repo)) == "deny", cmd


def test_one_off_reads_and_the_main_session_are_allowed(repo):
    assert hook("no_ci_polling.py", bash(repo, "gh pr view 5 --json state"), repo) is None
    assert hook("no_ci_polling.py", bash(repo, "gh run watch 123", sub=False), repo) is None
    assert hook("no_ci_polling.py", bash(repo, "for f in *.py; do ruff check $f; done"), repo) is None


# ── model_gate ──

def agent(repo, prompt, model=None, sub=False):
    p = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": str(repo),
         "tool_input": {"prompt": prompt, **({"model": model} if model else {})}}
    if sub:
        p["agent_id"] = "a1"
    return p


def test_the_expensive_model_needs_an_approval_line(repo):
    assert decision(hook("model_gate.py", agent(repo, "build it", "opus"), repo)) == "deny"
    ok = agent(repo, "build it\nMODEL-APPROVED: owner 2026-10-25 hard proof", "opus")
    assert hook("model_gate.py", ok, repo) is None
    assert hook("model_gate.py", agent(repo, "build it", "sonnet"), repo) is None
    assert hook("model_gate.py", agent(repo, "build it"), repo) is None


def test_subagents_do_not_dispatch_agents(repo):
    assert decision(hook("model_gate.py", agent(repo, "help", sub=True), repo)) == "deny"


# ── no_subagent_pr ──

def test_subagents_do_not_open_pull_requests(repo):
    assert decision(hook("no_subagent_pr.py", bash(repo, "gh pr create --fill"), repo)) == "deny"
    assert hook("no_subagent_pr.py", bash(repo, "gh pr create --fill", sub=False), repo) is None
    assert hook("no_subagent_pr.py", bash(repo, "git push -u origin HEAD"), repo) is None
