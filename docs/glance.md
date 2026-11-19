# Glance (RW-1): an employee looks at the human's front browser page

`screen_glance {find?, role?, app?}` is a Synthetics MCP tool. It asks the installed Noteling, read-only, what
is on the human's front browser page. Without `find` it gets counts. With `find` it asks whether a named control
is there. It never clicks, types, speaks or reads page text. Code: `synthetics/glance.py`. Intent: pm-c12016e1.

## Opt-in (off by default)

The tool reads the human's screen, so no employee gets it implicitly. A home is offered `screen_glance` only when
`<home>/glance.json` is a JSON object with `"enabled": true`:

```json
{"enabled": true}
```

The operator or manager writes this file. A missing, unreadable or different file means off. Then the tool is
left out of `tools/list` and a call is refused as an unknown tool. Noteling is not launched and nothing is
recorded. For the live acceptance kit only Rowan's and Codex's homes opt in.

## What is read

Synthetics starts a second, background Noteling instance through LaunchServices:

    open -n -g -W -a /Applications/Noteling.app -o OUT --stderr ERR --args --read-page --json
    open -n -g -W -a /Applications/Noteling.app -o OUT --stderr ERR --args --find NAME [--role R] --json [--app B]

- `-n` starts a new instance, so the running app is untouched. `-g` keeps it in the background, so focus doesn't
  move. `-W` waits for it to exit.
- The instance reads with Noteling.app's own Accessibility grant. A Noteling exec'd straight from a worker shell
  has none.
- `--full` and `--contains` are never passed. There is no fallback to them.
- The employee's values are checked before anything launches, because Noteling picks its mode by looking for a
  flag anywhere in its arguments (`CommandLine.arguments.contains`), and modes like `--ask` are tested before
  `--find`. So `find` may not start with `-` or contain line breaks, `role` is letters only, and `app` must look
  like a bundle ID (`com.google.Chrome`). A refused value is a tool error: no launch, no record.
- One glance runs at a time on the Mac: each takes the host-wide lock `/tmp/synthetics-glance.lock` and waits up
  to 15 s for it (`busy` otherwise). This matters because a timeout kills the instances that appeared since the
  glance began; without the lock, two homes glancing at once could kill each other's instance.
- Noteling ≥ 0.10.5 is needed (`--read-page --json` and `--find`, noteling #131/#132). It is browsers only.
- OUT and ERR live in a fresh 0700 temp folder that is removed on every exit path.
- The read is capped at 15 s. On timeout, `open` is killed, and so is the Noteling instance it started (found as
  this app's executable run with `--json` that wasn't there before the launch). There are no retries: one glance
  launches at most one instance.

## What the employee gets back

The tool result has `ok`, `exit`, `status`, Noteling's page counts (including `site`), and, with `find`, Noteling's
hits: role, kind, label, visible, enabled and frame. It also has a sentence when the glance failed, and
`glance_id`.

A hit's label can carry page words, because an "alike" hit names the control as the page does. Only known
page-count fields are passed on; Noteling's page text (`text`, only with `--full`) and `contains` never are.

## Where things persist

| Where | What | Who can see it |
| --- | --- | --- |
| Store table `glances`, audit log (`glance.recorded`) | Whitelisted fields only: time, run, ok/exit/status, numeric counts and `kinds`, `site` as host only, the asked name and role, match kind, count, unique, visible/enabled per hit, browser bundle asked for | Shared: Lookout and status readers |
| `<home>/shared/status.json` → `glances` | The newest 5 stored rows | Shared |
| Lookout Team page, "Recent glances" | The same rows | Shared (localhost GET) |
| The calling session's model transcript (Claude's session log, outside Synthetics) | The full tool result, **including hit labels and frames** | Private to the calling home |
| `<home>/runs/*.mcp.jsonl` | The receipt only: tool name, arguments (`find`, `role`, `app`) and success. No result. (`synthetics/mcp.py` `Server.receipt`) | Private to the calling home |

**The asked name is stored as given.** `find` is the employee's own words, so the `glances` table, status.json and
Lookout show it. If an employee copies a label from an earlier "alike" hit into a new `find`, those page words
land on the shared surfaces. Ask by the name a person would say ("Add to cart"), not by a label read off the page.

The private run files are home data, like any other tool result. The leak expectation is 0 page words on the
shared surfaces: the project logs, status.json, Lookout and the `glances` table. On the private run files the kit
reports counts and doesn't fail.

## Refusals

| status | sentence |
| --- | --- |
| `noteling_missing` | Noteling isn't installed at PATH, so the screen can't be read. |
| `noteling_too_old` | Noteling too old (need ≥ 0.10.5). This covers an Info.plist version below 0.10.5 and output that has no JSON report (0.10.2 silently ignores `--json`; its human line is never parsed). |
| `no_answer` | Noteling exited without answering (it crashed or was stopped). A later glance may work. |
| `busy` | Another glance is still running on this Mac. Try again shortly. |
| `timeout` | Noteling didn't answer within 15 s. |
| `launch_failed` | Noteling couldn't be launched. |
| `not_found` (exit 1) | The page was read, but no control by that name is on it. |
| `usage` (exit 2) | Noteling rejected the request as a usage mistake. |
| `no_browser` (exit 3) | There is no browser window with a page in front, so there is nothing to read. Don't retry until the human brings a browser page forward. |
| `no_permission` (exit 4) | Noteling has no Accessibility permission. Don't retry: tell your manager, who can ask the human to grant it. |
| `partial` (exit 5) | The page was only partly read (still loading, or too large to walk). A later glance may see more. |

Every refusal is a normal tool result with `ok: false` and is recorded like a successful glance.

## Testing

`tests/test_glance.py` replaces the reader seam (`glance.reader`) with a fake, and `open` with a stub script. It
checks these behaviours:
- the opt-in switch;
- argument building (no `--full`/`--contains`) and refusal of values that could be Noteling flags;
- the host-wide lock and the `no_answer` status;
- whitelisting into the table, status.json and Lookout;
- host-only `site`;
- the shape-based version gate;
- timeout cleanup.

The live path needs a signed Noteling 0.10.5 and the human at an unlocked screen. It is the acceptance kit, not
the unit suite.
