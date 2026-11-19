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
| `/` | Open questions across the homes, first line only (from `status.json`). |
| `/q/<home>/<question>` | The full question via `channel question` (contract §3.2) and the answer form. Any question the contract refuses (not open, not addressed to that home's human, relayed elsewhere, unknown) is 404 with the same text. |
| `POST /a/<home>/<question>` | Re-checks the question with `channel question`, then submits one `channel inbound` line: `external_id` `desk:<question>:<form nonce>`, `sender` = the question's `to` as the contract returned it (the home's `default_human`; a form `sender` field is ignored), `source` `lookout:desk`, `question_id`, the question's `thread`. `routed` is never set. The answer is 1 to 8192 bytes. Success is 303 to the status page. |
| `/s/<home>/<event>?q=<question>` | Status in three steps (below). |

The desk writes nothing to any store directly; its only write is `channel inbound`.

## Accepted is not applied (status page)

Each step comes from a record, never from the HTTP response alone:

1. **Submitted**: `channel event-status` knows the event and it came through the desk's channel.
2. **Employee read it**: `event-status` is `processed`, meaning the session that captured the
   event has finished (with its run ID). A captured but unfinished event is not "read".
3. **Outcome**: step 2 holds and the home's `status.json` no longer lists the question as open.
   Otherwise it says "still open"; a missing or unreadable `status.json` also counts as "still
   open". `status.json` is rewritten by the employee's tools, so this step can lag behind step 2.
   It says "no longer open", not "answered": a question that was withdrawn or answered by proxy
   also leaves the list. The question ID for this step comes from the `?q=` link the desk
   itself redirected to; P2 derives it from the event record instead.

## Honest limit

A process running as David's OS user can read the desk key and call `channel inbound` directly, as it can with the Telegram or Postern keys. The desk adds no new forgery route, because neither the cookie nor the code is ever on disk. Cookies are scoped to the host, not the port, so other 127.0.0.1 services see the cookie. It's short-lived and only gives access to the desk.

Later hardening: WebAuthn/Touch ID presence per answer is the only option that also stops a
same-user process. It needs JavaScript and the `localhost` host, because WebAuthn refuses IP
hosts.

## Not in P1

Context-only replies (no `question_id`), partial answers with a linked remaining question, and
retry or restart recovery come in P2.
