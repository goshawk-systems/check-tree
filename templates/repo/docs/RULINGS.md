# Rulings

The owner's decisions that bind this repo, in her or his own words. A ruling is applied, never re-argued: if it
looks wrong, bring evidence to the owner as a proposed change. Newest first. Add a row the moment the owner rules,
with the verbatim words (copied, not paraphrased).

Every row needs: the date, the owner's words, why it was ruled, and how to apply it (what an agent does differently,
and what enforces it).

| Date | The owner's words (verbatim) | Why | How to apply |
|---|---|---|---|
| 2026-01-01 (EXAMPLE, delete) | "No pull request over fifty files." | Large PRs were never really reviewed and broke the queue. | Split by capability; one finished step per PR. [CI: max_pr_files in .check-tree/repo.json] |
| 2026-01-01 (EXAMPLE, delete) | "Production deploys are mine. Write me the command." | An agent deployed an unfinished change. | Write the exact command in the report and stop. [prose: protect the prod environment] |
| 2026-01-01 (EXAMPLE, delete) | "Agents do not watch CI." | Many agents polling exhausted the API rate limit. | Push, return the commit sha, stop. [hook: no_ci_polling.py] |
