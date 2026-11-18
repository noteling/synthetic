# Postern: the reference channel adapter

Postern bridges two directories to one or more employee homes through the
channel contract (docs/channel-contract.md). Anything that can write and read
files can talk to employees through it: a mail gateway, a ticket system, a
cron job or a person with a shell. It is the maintained example of a
third-party adapter. It never imports the Store; it only runs
`synthetics --home H channel …`.

## Setup

Create the employee and start its worker first (docs/channel-contract.md §0).
Then:

```sh
# 1. Provision a key for the channel in every home it serves. --home goes after
#    the subcommand, once per home. The key is written to
#    ~/.config/synthetics-channels/postern.key (pass --key-dir to choose another
#    directory). Route every address the employee should reply to.
python3 -m synthetics channel provision postern \
    --source-prefix postern: --route alex --route ops-desk \
    --home /path/to/homes/alex-assistant
# 2. Write the config.
mkdir -p /srv/postern
cat > /srv/postern/postern.json <<'JSON'
{"v": 1, "channel": "postern",
 "key_file": "~/.config/synthetics-channels/postern.key",
 "homes": ["/path/to/homes/alex-assistant"],
 "inbox": "inbox", "outbox": "outbox"}
JSON
# 3. Try one cycle, then install it as a launch agent.
python3 -m synthetics postern run --config /srv/postern/postern.json --once
python3 -m synthetics adapter install --label com.synthetics.adapter.postern -- \
    /usr/bin/python3 -m synthetics postern run --config /srv/postern/postern.json
```

`adapter install` writes the same captured environment as `worker install`
(docs/channel-contract.md §6): the installer's `HOME`, a fixed `PATH`, and
`PYTHONPATH` plus `WorkingDirectory` set to the Synthetics checkout. So
`-m synthetics` imports in the launch agent, and `~` in `postern.json` means the
installer's `HOME`, not whatever `HOME` a later shell has. For a throwaway test,
point `key_file` at an absolute path under the test's `--key-dir` instead of
changing `HOME`.

Paths in `postern.json` (`key_file`, `homes`, `inbox`, `outbox`): `~` is
expanded first. A path that is still relative is taken relative to the directory
holding `postern.json`, not the current directory. All are then resolved to
absolute paths.

The `source` of every inbound line must start with the channel's
`--source-prefix` (`postern:` above). For the employee to treat the sender as
its human, the home's `humans.json` must list that sender in `senders`, or list
the same prefix in `channel_prefixes` (for example
`init --human alex --human-channel postern:`).

Postern creates `inbox/`, `inbox/done/` and `outbox/` itself on start (inbox
with your umask). It refuses to start if `inbox/` is group- or world-writable,
because anyone who can write there speaks with the channel's authority. Use
`chmod 700` on it if your umask allows group writes. `outbox/read/` in the client
below is the reader's own directory; Postern never creates or reads it.

## Files

| path | written by | meaning |
|---|---|---|
| `inbox/<name>.jsonl` | you | inbound contract lines, plus optional `"home"` (path or directory name; optional with one home). Write a temporary name, then rename. |
| `inbox/<name>.result.jsonl` | Postern | one contract result per input line, in order, with `home` |
| `inbox/done/<name>.jsonl` | Postern | the input, moved only after its results are durable |
| `outbox/<message_id>.json` | Postern | one delivered message (the contract pending view plus `home`) |

A crash before the input moves replays the file; the contract's dedup answers
`duplicate`, so nothing is fed twice.

## One cycle

1. Each inbox file is fed with `channel inbound`, grouped by home.
2. For each home, uncertain messages are reconciled from evidence: if
   `outbox/<id>.json` exists and names the message, `delivered` with receipt
   `file:<sha256>`; otherwise `failed`, which returns it to pending as a
   recorded decision.
3. Each pending message is claimed, written atomically to the outbox and
   acked `delivered`. A refused claim is skipped.

Each process has its own instance ID, so claims left by a crashed Postern
become uncertain at the next start and are settled by step 2. Nothing is resent
blindly.

`postern run` repeats the cycle every `--interval` seconds (default 5) until
stopped; `--once` runs one cycle. A reply is written by a later cycle than the one
that fed the message, after the employee's session has finished (see the
contract's §3 on latency). A single `--once` therefore never shows the reply.

## A ten-line client

```sh
#!/bin/sh
# Send one message to an employee, then print the employee's replies so far.
IN=/srv/postern/inbox; OUT=/srv/postern/outbox; ID=$(date +%s)
mkdir -p "$OUT/read"
python3 -c 'import json,sys; print(json.dumps({"v":1,"external_id":sys.argv[1],
  "sender":"alex","source":"postern:shell","body":sys.argv[2]}))' "$ID" "$1" \
  > "$IN/.msg-$ID.tmp" && mv "$IN/.msg-$ID.tmp" "$IN/msg-$ID.jsonl"
sleep 10; cat "$IN/msg-$ID.result.jsonl"
for f in "$OUT"/message-*.json; do [ -e "$f" ] && cat "$f" && mv "$f" "$OUT/read/"; done
```

The line is built with `json.dumps` (or `jq -n --arg`), never `printf %s`, so
quotes, backslashes and newlines in the message cannot break the JSON or inject
fields. The result file appears after the next cycle (up to `--interval`). A
reply usually takes longer, so run the last line again later. Postern never
deletes outbox files; consuming them is the reader's job. If Postern crashes
between writing a file and acking it, the next start reconciles from that file.
A reader that already moved the file causes a recorded `failed` and one
redelivery, never a silent loss.
