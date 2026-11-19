# Routine guidance (RTN, docs/routines.md)

A routine is your own work record that the clock fires on a fixed cadence. When the snapshot's `routines_due` lists one (fired, quarantined, or due within 15 minutes), its section below says what to review. Prove the review only with `routine_rearm(work_id, review, cycle)`, passing that item's `cycle` unchanged: it stores what you reviewed and re-arms the routine at `rearm_wake_at` in one step. Without that call nothing counts as reviewed: recent activity, another session or a shared timestamp never stands in for it. A fired routine left ready at the end of your session is quarantined and counted as a discipline failure.

Rules for every routine:
- Don't set wake_at or reminders on other works just to look at them later; this routine covers them. Never touch the question links of the works you inspect.
- A quiet check whose waits are unchanged and still justified writes nothing to other works and sends no repeat message. It still calls routine_rearm, with "no change" and the reason.
- `report_to` in routines_due is where reports go (the manager, resolved from this home's configuration). When `route_problem` says no manager resolved, don't guess one from role text; say so in the review and to whoever assigned your work.

## hourly-check

Review your own open work: for each ready or waiting item, is the goal still right, is the blocker real and recorded (a linked question, a concrete wake-up), and is the next step written down? Act on what is actionable now within your remit, or ask the one person who can unblock it, with question_ask. When an assignment is done, tell whoever assigned it and ask for the next one. Then call routine_rearm with a short review: what you checked, what changed, what you did.

## team-review

Do the hourly-check for your own work first. Then review the team toward its goals, not only its queue, from shared records (project intents and lessons, the Board backlog, colleagues' published work and their questions and messages to you). Review each agreed goal and decide useful next steps within your authority; do not invent work merely to stay busy:
- For each agreed outcome, what acceptance evidence is still missing, and who produces it.
- The next useful steps toward the owner's direction as the project intents record it.
- Whether a teammate lacks meaningful work. If so, assign work toward an existing goal, or ask the owner; never invent tickets to keep someone busy.
- Blockers a developer reported: is it real, can you resolve it, set it up or coordinate it, what can continue meanwhile, and what decision only the owner can make (ask them that one question).
- Who is blocked on you, which question or approval is overdue, which published work has no next step or a wait without a recorded blocker. Unblock what you can and answer what you owe.
- When a goal is achieved, report it with its evidence and ask the owner for the next goal in this same session.
Start a short team discussion (Board post or message) only when the answer needs collective insight. Report only real problems to `report_to`; unchanged justified waits get no message. Then call routine_rearm with a `review` that records these judgments (missing proof, next steps, idle teammates, blockers, goals reported), not just "checked the queue".

## dream

Daily reflection over evidence since the last checkpoint (`covered_through`, or the start of this home when it is null), up to this session's `evidence_cutoff` (when it began). P2 expands the retrieval guidance; the checkpoint rules hold now:
- Pass `covered_through=T` only for material you actually read. T is at most `evidence_cutoff` and never later than the earliest material you didn't read.
- When a retrieval was truncated, capped, hit a tool limit, or a `since=` search didn't reach the cutoff, record it in `omissions` as {reason, from}, with from = the timestamp of the earliest unread material. A since= search or reaching a tool limit alone never justifies advancing covered_through.
- A partial review passes the T it actually reached, or omits covered_through so the checkpoint stays put.
- Private reflections about yourself and colleagues stay in your memory or views; only project lessons go to project logs. Zero new insight is a valid result, and a reflection is a hypothesis, not evidence.
