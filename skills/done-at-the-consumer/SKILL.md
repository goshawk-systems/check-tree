---
name: done-at-the-consumer
description: What "done" means. Use before saying a task is finished, before grading another agent's return, and when writing acceptance for a work item. Done is demonstrated where the work is used (the screen, the API response, the job output), never "merged" or "tests pass".
---

# Done is shown at the consumer

Merged is not done. Deployed is not done. Tests green is not done. A producer-side signal says the code
ran; it does not say anyone can use what you built. Most false "done" reports in agent teams are one of these.

## The rule
Done means a real consumer used the capability and you looked at what they got:
- a user feature: the rendered screen, driven in a browser, showing the real content
- an API: the response of the running service, with its body (a document hash or a field, not just status 200)
- a job: the output it produced (rows, files, counts) from a real run
- a fix: the symptom reproduced before, gone after, at the same place the user met it

## How to prove it
1. State the acceptance in consumer terms before you build: who does what, and what they see.
2. Build. Then run the thing the way the consumer does, against the real running system or the nearest real
   one (staging, a local server), not a mock of it.
3. Check at the seam: where your change meets the next system, look at what crosses it, with something the
   tests do not own. An unmocked call, the actual response, the actual rendered page, a query count.
4. Report what you saw: the command or URL, and the output or a screenshot. "It works" is not a report.
5. If you cannot reach the consumer (no browser, no staging access), say so and name what is unproven. Do not
   substitute a unit test and call it done.

## Common false-done
- "The tests pass." The tests may not exercise the path the user takes. Show the path.
- "It deployed." The page may still be empty. Open it.
- "The job ran, errors zero." Zero errors on zero rows. Look at the rows.
- "Merged." Check the content landed on the main branch, then check the consumer.
- A test written with a fake of the very thing under test. It proves the fake.

## Where it is enforced
In check-tree, the objective check of a program is measured at the consumer (`proven_with` says against
what), and the evaluator runs it and tries to make the pass false. A reviewer reads your "proof" line and
reproduces it. Write it so they can.
