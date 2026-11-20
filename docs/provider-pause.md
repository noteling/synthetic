# Provider pause (#98)

When the model provider refuses a session for quota (or, untested, an outage), the worker treats it as
**no verdict** on the session, its settings or its work. It pauses, retries by itself, and tells the owner once
when it stops and once when it recovers. Before #98 the same refusal failed the session, failed probation and
reverted good settings, quarantined routines, and after three tries stopped the worker (11-19 23:32Z).

## Classification (D1)
- Claude: `brain._result` reads `runs/<run>.claude.json` **before** the exit status. A result line with
  `terminal_reason: "api_error"` and `api_error_status: 429`, `api_error_type: "rate_limit_error"` or
  `api_error: "usage_limit_reached"` is `ProviderUnavailable(kind="quota")`. `529` / `overloaded_error` is
  `kind="outage"`: **untested**, classified on documented fields with a synthetic fixture only, no observed sample.
  Everything else keeps today's path; an absent or invalid `claude.json` with a nonzero exit gives today's
  message byte for byte. Turn count, duration and stderr are never used.
- Codex: there is no structured error field, so `codexevents` classifies on the failure text. Authentication is
  checked **before** quota (an auth failure needs a human; read as quota it would retry silently). A quota
  failure maps to `ProviderUnavailable` at both codexbrain raise sites; no provider text is carried.

## Visible cause (D2)
The run's `error` is `provider_unavailable <kind> (<api_error>/<status>): <provider text>`, and its `provider`
field is `{kind, status, api_error, reset_at}`. The provider's own text is bounded to 300 UTF-8 bytes, cut at a
character boundary. The stream result fact keeps `terminal_reason`, `api_error`, `api_error_status`,
`api_error_type`, and `api_error_text` only for `api_error` results.

## No verdict (D3)
- `failure_streak` skips runs with `provider` set; `serve` neither counts nor resets its three-failure counter on
  a provider run or a poll that finds the provider pause in force.
- `run_fail(provider=…)` quarantines no routine.
- Probation is not decided: the next session that finishes, or fails for its own reasons, decides it.
- Inputs stay pending and replay after the pause (at-least-once). Tool effects made before a mid-session refusal
  can repeat on replay; that is the existing contract.

## Pause and resume (D4/D5)
- One `capture_meta` row (`provider_pause`), so a restart keeps it; every change is audited
  (`provider.paused`, `provider.resume_requested`, `provider.resumed`).
- Next try: a parsed reset hint (`resets 8am (America/New_York)`) + 2 min when it is in the future and at most 8
  days ahead; otherwise back off 5, 10, 20, 40, then 60 min (cap). The try is an ordinary session.
- Precedence: failure pause > provider pause > daily limit. The provider pause admits no limit bypass.
- `limit resume` also asks for an early try: the engine sets the next try to now and keeps the attempt count.
- The owner (`limit_notify`) gets one message on entering (`provider-pause-<first run>`) and one on leaving
  (`provider-resume-<first run>`); both name the home and say "next try", never "resets at". Lookout's paused
  field (`reason provider_unavailable`, `kind`, `next_try_at`, `attempts`) is the primary signal.
- A second runtime request applied while a probation is undecided marks the first `superseded_on_disk`
  (runtime.py); that is not a probation failure.

## Known limits
- Per-home state only: with several homes on one subscription the owner can get one stop and one recovery message
  per home per outage, and a home with no route to its `limit_notify` leaves a stuck outbox item.
- A probation that changes the model to one whose own quota is exhausted stays undecided until that quota returns.
  Future work, once `allowed.models` has more than one entry: fail probation when the request changed the model.
- The outage kind is untested on this host.
