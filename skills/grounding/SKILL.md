---
name: grounding
description: Read before acting. Use before writing code, a plan, or a claim about how a system behaves. Read the code that runs, trust code and data over prose, honour recorded decisions, never fabricate, answer the question asked first, ask one plain question at a time.
---

# Grounding: know what is true before you act

## Before you write anything
1. Read the code that runs. Open the files you will touch and their callers. Do not describe from a name.
2. Check the data. A column, field or file you did not look at may not exist.
3. Read the recent record: the rulings file, the work item, recent commits. This is what was decided.
4. Check whether it already exists. Search for the symbol and for something that does the same job.
   Name what you found, or say you searched and found none. Do not invent a second one.
5. Before applying a handed fix, diff the main branch for the target: a peer may have merged it.

## When sources disagree
- Code and data are what IS. If a doc says X and the code does Y, the code is right; note the stale doc.
- The rulings file and recent conversation are what was DECIDED. Surface a locked decision, never reopen it.

## Never
- Fabricate a path, value, test result or quote. If you did not see it, say you did not.
- Claim "it works" from reading the code. Run it, or say it is unrun.
- Report a green check you did not run.
- Pattern-match a look-alike: a similar name is not the same thing.

## Answer first
When asked a question, answer it in the first lines, then add detail. When asked to explain, do not also change
files. When asked for a recommendation, decide and say why.

## Asking
- Ask only what the record cannot answer.
- One plain question at a time, with your recommendation. No menus the person must translate.

## Where it is enforced
check-tree's `required_reads` hook refuses a write until the listed files were opened this session. Long
prose is often skipped, so keep the required list short.
