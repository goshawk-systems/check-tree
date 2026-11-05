---
name: spend
description: How agent teams spend money. Use before adding a CI job, runner, scheduled run, large machine, extra agent, or any recurring cost, and before claiming a cost saving. The bill follows how much CI runs; quote monthly; measure before claiming; keep a hard ceiling.
---

# Spend: the bill follows the volume

Agent teams push often and each push starts CI. The bill is mostly how many times CI runs, not how
expensive each machine is. Halving a machine's price does nothing if volume triples. Cost lessons from a team
that learned this the hard way:

1. **Volume first.** Before a cheaper machine, ask how many runs a change costs and whether any can go.
   Fewer runs per change is the real cut: run the full suite once per change (in the merge queue), not on
   every push; no agent polls CI; push when the change is done.
2. **Quote every recurring cost per month,** in dollars, next to what it replaces. "A few cents a run" is
   not a quote. Multiply by runs per day.
3. **No standing spend increase as a default.** Reliability comes from recovery (retry the lost job),
   smarter pools, and removing waste, not from buying permanent capacity. A standing increase needs the
   owner's explicit yes to a monthly figure.
4. **Measure the exact step before claiming a saving.** A step quoted as a large share of cost turned out to
   be seconds per job. Time the step, count the jobs, multiply, then say it.
5. **Never size from an old measurement.** Read the newest run's peak before choosing a machine size.
6. **The fleet has a hard ceiling.** The only guaranteed limit is the cap on concurrent machines. Set it,
   and set the agent cap, to what the owner can afford. Ask for the monthly figure she can afford, then size
   to it.
7. **Ephemeral runners bill their boot.** Many short jobs cost more than a few long ones.
8. **Dollar figures are monitored, not gates.** Do not build a check that refuses a run on a dollar
   estimate. Do block a shape: an on-demand class where spot is intended, a hosted-runner count that grew.

## Agents
- The default model is the cheaper one. The expensive model needs an approval line in the dispatch.
- A dispatched agent never watches CI (no `gh run watch`, no polling loop). It pushes and returns.
- Cap concurrent agents; check-tree's `agent_cap` hook does this.

## Talk about money plainly
One question at a time, in plain words, with your recommendation and the monthly number.

## Where it is enforced
check-tree hooks: `agent_cap.py`, `model_gate.py`, `no_ci_polling.py`. A cost rule with no hook is a request.
