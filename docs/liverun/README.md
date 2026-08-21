# Live-run supervisor

Errorta can launch, watch, and safely stop a long-running program from Slack.
It runs what **you** declare in a profile; it never composes commands itself.

## Where profiles live

`$ERRORTA_HOME/liverun/profiles/<name>.yaml` (default
`~/.errorta/liverun/profiles/`). The file must be owned by you, mode
`0600`/`0640`/`0644`, not a symlink, resolve to a path inside the profiles
directory, and contain `created_by: operator` and `version: 1`. The profile's
`name` is the filename stem. Invalid profiles are listed by
`list_live_profiles` with the reason code; they never run.

Start from [`example-profile.yaml`](example-profile.yaml) — it is the OSRS
skeleton with every operator-specific value replaced by a `# FILL:` line, so it
is invalid by construction until you author it.

## Slack verbs

| Verb | Trust | Approval | What it does |
|---|---|---|---|
| `list_live_profiles` | R | none | names + whether each validates, with the failing rule |
| `start_live_run <profile>` | C | staged confirmation (autopilot may fire it) | launch steps in order → watch |
| `stop_live_run` | R | none, immediate — never waits on approval | evidence → teardown → logoff literals |
| `live_status` | R | none | phase, elapsed, per-probe last-ok age, cap headroom, literals |
| `resume_live_run <profile>` | C | **human tap only** | clears a `paused_awaiting_human` hold |

`resume_live_run` is the one verb autopilot will not auto-approve
(`errorta_slack.tools.HUMAN_ONLY_VERBS`): the hold exists *because* something
ban-class or cap-class happened, and its whole value is that a person looks
first. All five verbs answer only from a channel bound to a project, and only
for allowlisted Slack team/user ids.

A bare "stop", "cancel" or "abort" in the channel calls `stop_live_run` in
addition to the existing `stop_runtime` behaviour.

## What you will see in Slack

Progress is read from the run's own `events.jsonl`, one channel line per event,
deduped by a `liverun:<run_id>:<seq>` marker: every launch step and its check,
every stall warning, the stall itself, each evidence step, each teardown
sub-step with its literal (`logoff_verified=PRESENT` or `ABSENT`), the closing
`Teardown literals — logoff_verified: PRESENT`, cap hits, ban-signal pauses,
and refusals.

Stalls, ban signals, cap hits, the literals line, and any phase change into a
terminal phase (`stopped`, `failed`, `paused_awaiting_human`,
`lost_on_restart`) are **mandatory**: they post even when the channel is muted.
Routine `phase → watching` chatter is not.

A `window_shot` evidence step's PNG is attached to its own line. Only `.png`
evidence refs are ever uploaded. Every field is escaped and capped before it
reaches the channel — a stderr tail and a matched ban pattern are untrusted
text, not markup.

## Safety rules (enforced by the validator, fail-closed at load)

- `version: 1`, `created_by: operator`, unknown keys rejected at every level.
- **No shell.** Every argv is a list of strings; `$`, `` ` ``, `|`, `;`, `&`,
  `<`, `>` in any element is rejected. `$SESSION_ID` and `$RUN_ID` are the only
  substitutions, applied *after* validation, from supervisor-generated values.
- **Argv identity.** At run time a step may only execute an argv that is
  byte-identical to one the validated profile declared (`ArgvIdentityError`
  otherwise) — there is no path by which a composed string becomes a command.
- local commands: absolute `argv[0]`, or `osascript`/`pgrep`; `./gradlew` only
  with an absolute `cwd`. Any `cwd` must be absolute.
- banned tokens anywhere in any argv: `--ignore-risk-budget`,
  `--no-safety-plane`.
- a `remote` step whose argv contains `senditai_ng.cli` and `run` must also
  carry `--max-session-seconds`, `--receipt-id` and `--require-live-feed`.
- `remote` with `detach: true` requires a `pidfile`, and the command must stay
  alive: the launcher checks liveness **~0.2 s after spawn** and reports a
  failed launch (and removes the pidfile it just wrote) if the process is
  already gone. A one-shot command cannot be detached.
- `pidfile` / `log` / `file_mtime_newer` paths match `^[A-Za-z0-9~/._-]+$`.
  A leading `~` is expanded by the *remote* shell, not locally.
- `remote_signal.signal` and `.then` ∈ `TERM`, `KILL`, `INT`, `HUP`;
  `grace_s` ≥ 0. A pidfile that does not hold a bare positive integer is a
  clean no-op, never a broadcast `kill`.
- `http` / `http_json` URLs must be loopback (`127.0.0.1`, `localhost`, `::1`),
  scheme `http`, no credentials. Redirects are refused, never followed.
- every `host` must already be in `~/.ssh/known_hosts` (checked with
  `ssh-keygen -F`); otherwise the profile reports `host_unknown`.
- `caps` may only be **lowered** below the defaults: 2 launches/hour, 900 s
  minimum gap, 8/day, 2 consecutive failed cycles.
- `teardown` must include a step with `evidence_literal: logoff_verified`.
- **`literal_without_check`**: a step carrying *any* `evidence_literal` must
  also declare a `check`. A literal is a claim about the world, and only a
  check can substantiate one — `remote_signal` exits 0 when there was no
  pidfile to signal at all, so a check-less `logoff_verified` would be a forged
  receipt. The supervisor enforces the same rule a second time at run time, for
  profiles built in code.
- any `ban_signals` regex matching evidence, launch or teardown output →
  `paused_awaiting_human` plus a marker file that refuses every later start of
  that profile until a human runs `resume_live_run`.
- probes/checks reject unknown parameters: `http`, `http_json`,
  `file_mtime_newer` and `remote_pid_alive` checks only accept their
  documented keys, and `remote_stdout_advancing` / `remote_stdout_matches` /
  `remote_file_mtime_advancing` probes require the `argv` / `regex` / `path`
  they depend on — an unrecognised or missing field fails the load rather
  than being silently ignored at run time.

## Stopping, and what a literal means

Every exit path — stall, launch failure, operator stop, supervisor crash —
goes through the same sequence: **evidence first** (while the world is still
standing), then **teardown** in declared order, then the literals verdict, then
a terminal phase. The teardown sequence runs in a `finally`: a step that throws
halfway through cannot skip the kills, the verdict, or the phase.

`logoff_verified: ABSENT` is reported, never assumed. Absence of evidence is
posted as absence, not read as health.

## Restart behaviour

A live run is **never resumed** across an Errorta restart. On boot, every
non-terminal run on disk gets the full teardown against its *persisted* owned
resources — process groups, remote pidfiles, tunnels — and is marked
`lost_on_restart`. Boot recovery runs off the startup critical path, in a
daemon thread.

Recovery touches the caps ledger not at all: it is not a launch, and it records
no outcome either. A run lost to a restart tells you nothing about the profile,
so a restart neither spends the budget nor launders a genuine failure streak.

A persisted pgid names a number, not a process. Before any `SIGKILL`, recovery
checks that the pid still leads that group, runs as you, and was created no
earlier than the run — pid reuse means the alternative is signalling a
stranger. It fails closed: an orphan left behind is recoverable, a killed
bystander is not.

The sidecar exits with the desktop app, so an overnight run needs Errorta open.

## Authoring the OSRS profile — verify these first

These are the spec's open questions (§7). Resolve them while authoring the
profile, not in code — the validator reports, it never assumes.

1. `osascript` / `cliclick` from the sidecar: Accessibility permission is
   per-app, and agent shells have been seen failing with `-25211`. The example
   routes `jagex-play` through Terminal.app by absolute path for that reason.
2. The exact `/state` field on `:8081` that proves logged-out (the example
   assumes `gameState`).
3. Cost of the journal `seq` probe at a 30 s cadence: a direct read-only
   `sqlite3` query versus `osrs-watcher --json`.
4. Whether `senditai_ng.cli kill --session` resolves the same `--base-dir` the
   run itself used.
