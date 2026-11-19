# Answer desk: the human answers a tracked question locally (#25)

The answer desk is a small local web page where the human (the home's `default_human`, for
example David) answers an employee's open question without the desktop relay. The answer
enters the employee's home through the ordinary channel contract (docs/channel-contract.md), so
it is a first-hand answer with `auth`, and the engine wakes the linked work as for any other
channel.

It is a **separate process and port** from Lookout. Lookout stays GET/HEAD-only and holds no
key. When the desk is running, start Lookout with `--desk-port` and each Needs-you line on the
overview gets an "Answer here" link. Without `--desk-port` Lookout renders exactly as before.

## Setup (owner leg, not an employee session)

```sh
# 1. One key for the desk, registered in every home it serves. No --route: the desk delivers
#    nothing outbound (fail closed).
python3 -m synthetics channel provision lookout-desk --source-prefix lookout: \
    --home ROOT/alex --home ROOT/sam [--key-dir DIR]
# 2. In each home's humans.json, add "lookout:" to channel_prefixes, so the desk's events
#    count as the human's (docs/channel-contract.md §5).
# 3. Run the desk and Lookout.
python3 -m synthetics --home ROOT/alex answer-desk serve --key-file DIR/lookout-desk.key [--port 8766]
python3 -m synthetics --home ROOT/alex web --desk-port 8766
# 4. Pair your browser, in your own terminal:
python3 -m synthetics --home ROOT/alex answer-desk pair
```

`--root` (default: the directory holding `--home`) is the homes root, as for Lookout. Every
`answer-desk` command (`pair`, `serve`, `logout-all`) is refused when `SYNTHETICS_SESSION_RUN`
is set, so an employee session can't pair or serve. The desk binds to `127.0.0.1` only.

## Pairing and sessions

- `answer-desk pair` prints a one-time code of four groups of five letters and digits. It is
  valid for 5 minutes. Only a salted SHA-256 of it is written, to
  `ROOT/answer-desk/pairing.json` (directory 0700, file 0600); the code itself is never on
  disk or in a log. The file holds the salt, the hash, the expiry and the count of wrong tries.
  Typed codes are compared in constant time. Five wrong codes delete the
  pairing, and a new `pair` is needed. A used code is deleted.
- The code is four groups of five characters from a 31-character alphabet (no `0 1 i l o`),
  about 99 bits, rather than the P0's word list; Codex accepted both this and the salted-hash
  file.
- Every change to `pairing.json` and `sessions.json` (pair, redeem, logout-all) runs under one
  lock: a thread lock in the server plus `flock` on `ROOT/answer-desk/.lock`, because `pair`
  and `logout-all` are separate processes. So concurrent wrong codes still count one by one and
  the fifth kills the pairing. Each write goes to a fresh `mkstemp` file and is renamed into place.
- Typing the code at `http://127.0.0.1:8766/pair` sets the cookie `desk` (32 random bytes, hex;
  `HttpOnly; SameSite=Strict; Path=/; Max-Age=43200`). Only its SHA-256 is stored, in
  `ROOT/answer-desk/sessions.json` (0600). `answer-desk logout-all` deletes every session and
  any pending code.

## Every request

- `Host` must be exactly `127.0.0.1:<port>` (DNS rebinding), on GET as well as POST.
  `localhost:<port>` is refused too, so open the desk as `http://127.0.0.1:<port>/`.
- Every page except `/pair` needs a paired cookie; without one, GET redirects to `/pair` and
  POST is 401.
- Every POST also needs `Origin: http://127.0.0.1:<port>` (missing is refused),
  `Sec-Fetch-Site: same-origin` when that header is present, `Content-Type:
  application/x-www-form-urlencoded`, and a per-form token: HMAC-SHA256(cookie, question ID |
  form nonce), compared in constant time.
- Every response sets `Cache-Control: no-store`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer` and `Content-Security-Policy: default-src 'none';
  style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'`. There is no
  JavaScript. PUT, DELETE and PATCH get 405.
- The desk logs no request line, path, form field, header or body.

## Pages

| Path | What |
|---|---|
| `/` | Open questions across the homes, first line only (from `status.json`), then any **unconfirmed submits** (below). |
| `/q/<home>/<question>` | The full question via `channel question` (contract §3.2), its context (below) and the answer form. Any question the contract refuses (not open, not addressed to that home's human, relayed elsewhere, unknown) is 404 with the same text. |
| `POST /a/<home>/<question>` | Submits one `channel inbound` line (below). The answer is 1 to 8192 bytes. Success is 303 to the status page. |
| `/s/<home>/<event>` | Status in three steps (below). A `?q=` on older links is ignored. |

### Question context (P2)

Next to the question the page shows only what the home's `status.json` already exports (Lookout
shows the same) and the desk's own journal: who asks (name, role); delivery (the Docket's `ack`, and
when it was delivered elsewhere, its routes and a warning that only the first answer counts); each
linked work's ID, title, status and priority ("no longer open" once it is done); the questions it
carries onward and their askers; and earlier
desk submits for this question (time, kind, state, event ID). It never shows other messages in the
thread, other questions' bodies, work descriptions or notes, project log text or memories, and no
contract command reads them.

### Two buttons: the answer, or context that keeps the question open (P2 D1)

One text box, two buttons:

- **Send as the answer** ("This closes the question."): the line carries `question_id`. An accepted
  line closes the question in the same transaction (contract §3).
- **Send as context, keep the question open**: the same line *without* `question_id` and with
  `in_reply_to` = the question ID (D3). It never answers: the question stays open and its works keep
  waiting. The employee's session reads it and either records it as the answer (`question_resolve`)
  or asks again for the rest. The desk never changes question state, never edits the text and never
  decides whether an answer is complete.

Both lines use `external_id` `desk:<question>:<form nonce>`, `sender` = the question's `to` as
`channel question` returned it (the home's `default_human`; a form `sender` field is ignored),
`source` `lookout:desk` and the question's `thread`. `routed` is never set.

### Retry and the desk journal (P2 §3)

`answer-desk/submissions.json` (0600, written under the desk lock) records each form sent: home,
question ID, nonce, kind, sender, thread, linked work IDs, state (`pending`, `accepted`, `refused`, `lost`),
event ID, error code and time. It never holds the text, the cookie, the code or the form token.
Entries older than 7 days, or past the newest 200, are dropped.

- **First send of a form**: `channel question` must still return the question (else 409). The desk
  writes a `pending` entry with the returned sender and thread, then calls `channel inbound`.
- **The same form sent again** (same nonce): the desk first asks `channel event-status --source
  lookout:desk --external-id desk:<question>:<nonce>` whether the store has this form's event. If not,
  the earlier send never arrived and this one is a first send (open check included), so a resend can
  never create an event the open check would have refused. If it does, the open check is skipped
  and the journaled sender and thread are reused, so the line is byte-identical and the store's
  idempotency decides:
  `duplicate` → 303 to the original event; different text → 409 "already sent with different text";
  a switched button → 409 before any contract call. This is what makes a retry after a lost
  response safe: in P1 the retry hit the open check after the first send had already closed the
  question, and David was told his answer failed when it had gone through.
- **A double click** (two sends of one form at once): a send whose open check fails looks at the
  journal once more. If a concurrent send of the same form and button has claimed it and the store
  has that form's event (journal or lookup), the question closed because of this very form, so the
  line goes to `channel inbound` and gets `duplicate` (303) or "different text" (409). Without a
  found event (not routed, refused or silent lookup) it stays a 409 and nothing is sent. Before
  this, one click of a double click could read "not open any more" although its answer was recorded.
- **Answered elsewhere first** (for example on Telegram between the page load and the send): 409,
  nothing recorded, and his text is shown for his own copy.
- **Not confirmed** (timeout, no response or another refusal): 503 with the same form, the same nonce
  and token, and his own text, plus "Send again: the same form can't be counted twice".
- **Worker restart**: nothing to do. The event stays `accepted` until a session that read it finishes.
- **Desk killed mid-send**: the entry stays `pending`, and `/` lists it as an unconfirmed submit. On
  each load of `/` the desk asks `channel event-status --source lookout:desk --external-id …`
  (contract §3, own events only); when the event exists the entry becomes `accepted`. An entry
  still missing 2 × the contract timeout (120 s) after it was written can't be in flight any more,
  so it becomes `lost` and is listed under "Not received". After that it is never looked up on a
  view of `/` again, so it costs no more subprocesses or audit rows. Sending the same form again
  still looks it up first: if a late inbound did land, the resend finds it; if not, the resend is a
  first send (open check included) and the entry is `pending` again.
  Only the contract's `not_routed` answer ("no such event of this channel") counts as missing. Any
  other refusal (`auth_failed` after a key rotation, `bad_line`, `in_session`, a malformed or silent
  row) means the desk couldn't ask, so the entry stays `pending`. Reconcile settles only by
  compare-and-set under the journal lock: the entry must still be `pending` with the same write time
  it read, so a resend accepted meanwhile in another tab is never overwritten; a resend also renews
  that time.

The desk writes nothing to any store directly; its only write is `channel inbound`.

## Accepted is not applied (status page)

Each step comes from a record, never from the HTTP response alone:

1. **Submitted**: `channel event-status` knows the event and it came through the desk's channel. It
   says "accepted as the answer to Q; the question closed in the same step" or "accepted as context
   for Q; the question stays open". Q and the kind come from the event record (`question_id` or
   `in_reply_to` in `event-status`), never from the URL.
2. **Employee read it**: `event-status` is `processed`, meaning the session that captured the
   event has finished (with its run ID). A captured but unfinished event is not "read".
3. **Outcome**: step 2 holds and the home's `status.json` no longer lists the question as open.
   Otherwise it says "still open"; a missing or unreadable `status.json` also counts as "still
   open". `status.json` is rewritten by the employee's tools, so this step can lag behind step 2.
   It says "no longer open", not "answered": a question that was withdrawn or answered by proxy
   also leaves the list. For an answer it then lists each linked work's status. For context, the
   step only reports whether the question is still open once the employee has read it.

## Honest limit

A process running as David's OS user can read the desk key and call `channel inbound` directly, as it can with the Telegram or Postern keys. The desk adds no new forgery route, because neither the cookie nor the code is ever on disk. Cookies are scoped to the host, not the port, so other 127.0.0.1 services see the cookie. It's short-lived and only gives access to the desk.

Later hardening: WebAuthn/Touch ID presence per answer is the only option that also stops a
same-user process. It needs JavaScript and the `localhost` host, because WebAuthn refuses IP
hosts.

A same-form retry needs the cookie that signed the form's token, so it is possible for at most the
12-hour session; the journal's 7 days only matter for the unconfirmed-submits listing.
