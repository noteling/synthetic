# Click (stage 2): one approved, named click

An employee asks to press **one named control** on the human's front browser page. It is pressed only after the
home's default human explicitly approves **that exact control**, and at most once. Code: `synthetics/click.py`.
Design: Rowan's P0 (realworld/click-P0, SHA256SUMS ab76386f), approved with conditions by Codex
(management/realworld/click-P0/review.md, owner-decisions.md). David's go: event-bcf48896.

**Slice status.** P1 (this code) is the engine only. Noteling's `--click` mode doesn't exist yet: it is P2, a
Noteling release ≥ 0.10.6. Until then the real launcher refuses to start Noteling (`noteling_too_old`, nothing
launched; 0.10.5 doesn't know `--click`, and an unknown flag would start the full app), so only the test reader
can answer. The live click (P3) waits on David's next sitting.

## Opt-in (off by default)

The tools are offered only in a home whose `<home>/click.json` is `{"enabled": true}`. This is separate from
`glance.json`. The operator or manager writes it; a missing, unreadable or different file means off, the two tools
are left out of `tools/list`, and a call is refused as an unknown tool.

Honest limit: every home runs as the same OS user, so a process of that user could write the file. The file is an
off switch, not a security boundary. The boundary is the approval rule below, which needs an authenticated reply
from the human through a registered channel key the session doesn't hold.

## The two tools

`screen_click_request {name, why, role?, app?, work_id?}`

1. Values are checked as Glance checks them (nothing may start with `-` or contain control characters, `role` is
   letters, `app` is a bundle ID), plus `why`: one line, at most 200 characters.
2. **Irreversible-looking names are refused outright, whatever any answer would say** (v1, Codex's call, not
   David's limit). Whole words, any case: Noteling's `IrreversibleGuard.confirmWords` (send, submit, pay, purchase,
   buy, checkout, confirm, delete, remove, discard, sign, approve, reject, publish, post, transfer, unsubscribe,
   reply all) plus order, book, donate, subscribe, place, reserve, agree, accept, check out. Noteling isn't launched
   and nothing is asked.
3. One live request per home (`request_open`).
4. Noteling `--click NAME [--role R] --prepare --json --token T [--app B]` checks the control. Refused, with no
   question, unless: match `exact`, count 1, unique, visible, enabled, no sheet open, Noteling's guard class `safe`
   (a `confirm` or `forbidden` class is refused too), and both opaque tokens present. `T` is a fresh random
   per-request key; Noteling returns `page_token` and `hit_token` as HMACs under it, so the URL path and DOM ids
   never leave Noteling.
5. The **engine** asks the home's `default_human` (humans.json) one tracked question in a fixed form, naming the
   employee, the role and name asked, the site's host, the why, the reply that allows it (`yes <name>`), and the
   windows. The question is marked `click_id`. An employee's own `question_ask` never creates one.

`screen_click {request}` reads the answer from records only and fires only when **all** of these hold:

- The request's own question has status `answered` through a **correlated** reply: the answer event's `reply_to`
  is that question (Telegram reply or the answer desk). `question_resolve` and `question_close` with an `event_id`
  refuse click questions outright (Codex C1), and `screen_click` re-checks the event's `reply_to` itself, so an
  unrelated "yes" from the human can never be bound as the approval. A proxy answer never counts.
- The answer is a message from the home's `default_human`, with non-null `auth`, and its role is `human`: operator
  and secretary channels (for example a desktop relay) never count, even when the sender field says the human
  (Codex C2).
- The text reads `yes <name>`: case and spacing are ignored, and so is punctuation the human adds (`Yes, add to
  cart!`), but every punctuation mark or symbol **in the name** must be typed, in order (`yes C` doesn't approve
  `C++`; curly and straight quotes count as the same). Anything else (`no`, a bare `yes`, another name)
  **declines, finally**. A new click needs a new request.
- **Limits:** the answer came within **30 minutes** of the ask, and the fire starts within **10 minutes** of the
  answer. Most asks made while the human is away will therefore expire; v1 is meant for a human who is at the
  screen.
- Compare-and-set `asked → firing` inside one write transaction. Exactly one caller gets past it. Then Noteling
  `--click NAME --fire --json --token T --expect PAGE,HIT --named-approval NAME --require-front` presses at most
  once: it refuses when the page or control changed (`page_changed`) or the browser isn't frontmost (`not_front`).

## States

| state | meaning |
|---|---|
| `refused` | checked and refused before any question (reason says why) |
| `asked` | the question is out; `screen_click` says `waiting` until it is answered |
| `declined` | answered with anything but `yes <name>` (`declined`), or the answer doesn't count (`not_approved`) |
| `expired` | outside the 30/10-minute windows. If its question is still open, it is withdrawn in the same transaction, so it leaves the human's Docket. An abandoned request (never fired) is settled the same way before the next request and at the start of every session |
| `firing` | the one fire is in progress |
| `done` | pressed (`result.pressed`, `result.changed` as Noteling saw it) |
| `failed` | Noteling refused or couldn't start; nothing was pressed (`page_changed`, `not_front`, `busy`, ...) |
| `unknown` | a fire started but never finished (timeout, crash, any unexpected reader error, `firing` older than 2 minutes). **Never retried**: the employee tells the human and may glance |

Every later `screen_click` on a used request answers `already_used`.

## Where things persist

| Where | What |
|---|---|
| Store table `clicks`, audit log (`click.*`) | asked name, role, browser, why, host, state, reason, question ID, guard class, find summary (match, count, unique, visible/enabled), result codes, and the per-request key and opaque tokens. Never a hit's label, frame, URL path or DOM id |
| The question and its outbox message | the fixed-form body above, which names the host (owner-decisions Q6) |
| `<home>/shared/status.json` → `clicks`, Lookout `/e/<home>` "Recent clicks" | the newest 5 without key, tokens or why |

## Who presses vs who approves

The approval gate is the engine's and doesn't depend on who presses. `click.reader` is the actor (today a second,
background Noteling instance launched like Glance). If the employee later gets its own OS-level identity (its own
account or machine, David's longer direction), that replaces the actor; the gate stays in front of it.
