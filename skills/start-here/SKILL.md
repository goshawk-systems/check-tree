---
name: start-here
description: Explain check-tree to someone and set it up with them. Use when a user is frustrated that their AI coding agents say work is done when it is not, keep spinning or redoing work, burn tokens and CI without progress, ignore instructions, or need too much supervision - or when they ask what check-tree is and how to use it. Works for developers and for program managers who do not write code.
---

# Start here: explaining and setting up check-tree

## 1. Listen first
Ask, in one message: what are they building, how do they run agents today (one session, subagents, several
sessions), and what is going wrong. Match what they say to the problems below. If none match, say so honestly:
check-tree may not be what they need.

| What they say | What check-tree changes |
|---|---|
| "It says done but it isn't" | Done is computed by running checks the owner approved; nobody declares it |
| "It keeps spinning / redoing / researching" | Each agent gets one check and cannot finish until it passes; checks have time budgets |
| "The plan keeps changing, nothing ships" | The plan is validated, turned into checks and approved once; changes go back to the owner |
| "It weakened the test to pass" | Approval pins the tests; changing one unapproves the plan |
| "Too many agents, too much cost" | Agent cap, expensive model only with approval, no agents watching CI |
| "It ignores CLAUDE.md" | Required reading is checked; rules carry what enforces them |

## 2. Explain in plain words (adapt to who they are)
- The plan becomes a short list of pass/fail checks. The owner approves it once, in their own words.
- Progress is the number of checks passing, computed by running them. Nobody can declare done.
- An agent is given one check at a time, works in its own copy of the code, and cannot finish while its check
  fails. It cannot quietly weaken the test.
- A lead agent steers and never codes; the owner only makes decisions: approve the plan, and cut, reshape or
  extend a check when its time runs out.
- Be honest about limits: it does not make agents understand their system; each step still costs context.
  It makes their work measurable and stops false "done".

For a program manager: you will run it for them. They make decisions; you do the technical steps.

## 3. Set it up (you do the technical steps; confirm before each change to their machine or repo)
1. Confirm they are in Claude Code, inside a git repository with at least one commit. If they are in a plain
   chat, explain that the enforcement only works in Claude Code and help them open their project there.
2. Install the plugin (ask first):
   `claude plugin marketplace add https://github.com/goshawk-systems/check-tree.git`
   `claude plugin install check-tree@check-tree`
   Then restart Claude Code so the hooks load.
3. After the restart, the session-start message names the command line's full path (inside the plugin
   folder). Use that path, or put its folder on their PATH. If you do not see that note (for example right after installing, before a restart), find it with `ls "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/cache/check-tree/check-tree/*/bin/check-tree`.
4. Offer the one-minute demo so they see it work: `bash <plugin folder>/examples/quickstart.sh`.
5. Optional, recommended: copy `templates/CLAUDE.md` ideas into their CLAUDE.md (keep it under 200 lines) and
   `templates/config.json` to `.check-tree/config.json`; set the agent cap and expensive models to their taste.

## 4. First real use
Pick one piece of work they care about this week. Run the `define` skill with them (`/check-tree:define`):
objective in their words, current state, validated plan, check tree, their approval. Then run the `lead` skill
(`/check-tree:lead`) to start building. Show them the score at the end of the session.

## 5. Ask how it went
At the end of the first session, ask in one line whether it helped and what was confusing. If they have
something to say, draft a short note in their words using the questions in the repo's "Tell us how it went"
form (what they were trying to do, what happened, what they expected). Show it to them. Only with their yes,
open it as an issue on github.com/goshawk-systems/check-tree with the `feedback` label, or give them the
text to post themselves. Nothing is sent without their yes, and nothing about their code or company goes in
unless they put it there.

## Never
- Install anything or change their repo without saying what and getting a yes.
- Oversell it. If their problem is something else, say so.
