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
| `pause_fix_loop <profile>` | R | none, immediate | no new fix cycle starts, and a live run is asked to abort one in flight (dev run cancelled, staged merge withdrawn); live runs keep running |
| `resume_fix_loop <profile>` | C | **human tap only** | re-arms the fix loop |
| `accept_live_fix` | C | staged **by the supervisor** | merges + delivers one fix; not advertised to the concierge, and refuses any confirmation a live run is not waiting on |

`accept_live_fix` is dispatchable but **not** listed in the concierge's tool
catalog: the fix cycle stages it, and the effect refuses unless a non-terminal
live run reports that exact confirmation id as the one it is waiting on
(`LiveRunManager.accept_is_staged`). A chat turn can compose the verb name and
a run id; it cannot compose the id `stage_confirmation` minted. And "accepted"
is said only about a merge that actually applied — a conflicting merge-back
returns `applied: False` rather than raising, and that is reported as `error`,
never delivered and never deployed.

`resume_live_run` and `resume_fix_loop` are the verbs autopilot will not
auto-approve (`errorta_slack.tools.HUMAN_ONLY_VERBS`): the hold exists
*because* something ban-class or cap-class happened, and its whole value is
that a person looks first. Autopilot clearing its own hold is a loop, not a
gate. `accept_live_fix` joins them **conditionally** — see the fix loop below.
Every verb answers only from a channel bound to a project, and only for
allowlisted Slack team/user ids.

Pausing is deliberately asymmetric: `pause_fix_loop` is R-class and takes
effect the moment it is asked for, because turning autonomy *off* should never
wait on an approval; `resume_fix_loop` is C-class **and** human-only, because
turning it back on is exactly the decision a loop must not make for itself.

"The moment it is asked for" is literal, and it has to be: the fix-pause marker
alone only stops the *next* cycle, and a cycle already awaiting acceptance has
left a pending confirmation behind that the autopilot sweep would press minutes
later. So `pause_fix_loop` also aborts an in-flight cycle for that profile — it
cancels the dev run through the same `cancel_requested` signal `stop_live_run`
uses, withdraws the staged acceptance as `declined`, and lands that run
`stopped` with reason `fix_loop_paused`.

The abort is a **request**, honoured by the supervisor's own thread on its next
tick, exactly like `stop_live_run`: `_abort_fix` and `_close_out` are
check-then-set, so doing that work on the caller's thread while the daemon
thread sits in `_tick_fix` can duplicate the event and the fix-cycle ledger row.
The marker is written before the request, so the hold itself is instant; the
verb returns `pausing` when a live run was asked and `paused` when there was
none. `_tick` reads the request *above* the phase dispatch, so the cycle never
gets one more step — which could be the step that reads an approval and
deploys. A live run still *launching* or *watching* keeps going: by the time a
fix cycle exists, teardown has already completed.

A bare "stop", "cancel" or "abort" in the channel calls `stop_live_run` in
addition to the existing `stop_runtime` behaviour.

## Driving it from the terminal

Slack is one door onto the supervisor, not the only one. The sidecar exposes the
same manager over `/liverun/*`, and the CLI drives it:

| Command | Route | Gate |
|---|---|---|
| `errorta liverun profiles` | `GET /liverun/profiles` | none |
| `errorta liverun status [<profile>] [--watch]` | `GET /liverun/status` | none |
| `errorta liverun start <profile> [--project P]` | `POST /liverun/start` | `--yes` |
| `errorta liverun stop [<profile>] [--reason R]` | `POST /liverun/stop` | none |
| `errorta liverun resume <profile>` | `POST /liverun/resume` | `--yes` |
| `errorta liverun fix pause <profile>` | `POST /liverun/fix/pause` | none |
| `errorta liverun fix resume <profile>` | `POST /liverun/fix/resume` | `--yes` |

Start a run from the terminal:

```
errorta liverun start osrs --project senditai-ng --yes
```

Watch it:

```
errorta liverun status --watch
```

`--watch` repaints every 5 s — phase, elapsed, each probe's last-ok age against
its own `stall_after_s`, cap headroom, the literals — and **ends by itself** when
the run reaches a terminal phase, so the terminal is free again the moment the
run stops rather than after you notice and Ctrl-C. `--json` on any of these
prints the raw route payload for scripting.

The run belongs to the **sidecar**, not to the terminal that asked for it: this
is the same `live_run_manager` the Slack verbs drive, so a run started here shows
up in `live_status`, can be stopped from the channel, and survives the CLI
process exiting. Closing the terminal does not stop the run — `errorta liverun
stop` does.

The gate column mirrors the Slack trust classes exactly. Turning autonomy **on**
is gated: `start` launches real commands on a real host, `resume` clears a hold
something ban-class or cap-class put there, and `fix resume` re-arms autonomous
merging — each needs an interactive yes or `--yes`, and each re-checks the
sole-owner invariant first. Turning it **off** is not gated at all: `stop` and
`fix pause` fire immediately, and deliberately skip the sole-owner check too. A
stop you have to confirm — or that a second Errorta app on the host can refuse —
is a stop that arrives too late.

An operator note on a stop is recorded *inside* the reason, never as the reason:
`--reason "swapping the build"` is stored as `operator_stop:swapping the build`.
The fix loop decides whether a stop is a bug worth a cycle by matching
`^(stall|launch_step_failed):`, and a free-text note must not be able to talk it
into fixing a stop a human ordered.

A refused start is a normal `200` carrying the manager's own reason
(`already_running`, `project_has_live_run`, `profile_invalid:<rule>`, a caps
verdict) — printed as `start osrs: refused — already_running`. Profile names are
validated at the route (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, `422` otherwise)
because the name reaches the filesystem. All seven routes require a trusted
loopback origin plus the sidecar bearer token, and all seven fail closed under
remote data residency: a live run is a local-disk data plane end to end.

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

## The autonomous fix loop

Optional, off unless a profile declares `repos:` and `fix_loop.enabled: true`.
A profile without those keys is a complete live-run profile that watches,
stops and reports, and never touches a file. This is the one part of the
supervisor that changes code in your real checkouts — read this section before
you turn it on.

### The cycle

A run that stops for a **fixable** reason (a stall, or a launch step that
failed — never a ban signal, a cap, an operator stop, or the brain refusing)
goes: evidence → teardown → **triage** → a dev task → a dev run → the **merge
gate** → a staged acceptance → **deploy** → a **relaunch** as a new run id
linked by `fix_of`. It runs on the supervisor's own thread; `stop_live_run`
still interrupts it between steps.

Before filing its Focus, a cycle archives any previous liverun Focus and
drops/abandons the tasks and PRs created under it, so a cycle never inherits
the last one's debris (live 2026-08-23).

**Triage** is deterministic first. Each evidence class is a named signature
over supervisor-owned state (the stop reason, a `/state` capture, a traceback
banner), and the profile maps classes onto repos. Exactly one claimant is an
answer; zero or two is `ambiguous`, and the one PM turn that may follow
chooses from an enumeration of ids you already declared — a model cannot widen
the blast radius past your own `repos:` list. No class may be claimed by two
repos; the validator rejects that at load.

The **brief** filed as the dev task carries the run's evidence inside a
nonce-fenced UNTRUSTED block. The task title is template-generated from
operator- and supervisor-owned values only ("Fix: … during live session
`<run id>`" — "live session" rather than "live run" because the execution lint
reads "run" as a run-verb and would re-route the task). Nothing a log printed
ever becomes an instruction.

### What it will not do

The merge gate is **never** overridden. `accept_live_fix` is a separate verb
from the desktop app's accept route for exactly one reason: that route takes
an `override` that skips the gate, and a loop must not hold that switch. There
is no parameter, flag or profile key that bypasses it, and a grep test keeps
it that way.

Every one of these ends the cycle in `paused_awaiting_human`, which only a
person clears — the loop never retries its way past one:

| Pause code | What happened |
|---|---|
| `triage_ambiguous` | no repo owned the evidence, or two did |
| `repo_not_fixable` | triage named a repo marked `fixable: false` |
| `fix_no_gate` | the project has no registrable acceptance gate |
| `fix_project_not_existing` | the project's target is not `existing` — there is no repository to merge into |
| `fix_project_busy` | something else owns that project's run |
| `fix_run_failed` | the dev run could not start, or failed empty |
| `fix_idle` | the dev run went quiet and was cancelled |
| `fix_no_delivery` | a clean stop that delivered nothing is not a fix |
| `fix_unsafe_paths` | a delivered path left the repository |
| `fix_gate_blocked` | the merge gate said no; nothing was merged |
| `fix_accept_unverified` | approved, but the merge cannot be confirmed — including a merge-back that refused to apply, or a delivery that threw |
| `fix_declined` | the acceptance was declined, or never answered |
| `ban_signal` | a ban-class string surfaced during deploy |
| `fix_cycle_cap` | the day's fix cycles are spent |

Caps: `max_fix_cycles_per_day` (default 3) is arithmetic over the ledger, not
a counter in memory, and a cycle is counted whether it merged or paused.
`idle_timeout_s` (default 2400, floor 2100) must outlast the repository-read
dev turn, which can be TWO subprocess attempts back to back: the retrieval
attempt at `ERRORTA_REPO_READ_TIMEOUT_S` (1500 s by default) plus, when it
returns empty, the plain fallback at the request's own timeout (600 s by
default), so an idle detector at or below that 1500 + 600 = 2100 s floor
would cancel a run every time one turn fell through to the fallback. The
valid operator range is 2101–2400 (above 2400 fails `cap_raised`). The
relaunch is evaluated by the *untouched* Slice 1 launch caps — a fix that
lands inside the minimum launch gap posts `relaunch_refused` and stops there.

### Guarded paths — the human-only accept

`accept_live_fix` is staged as a button either way. Whether **autopilot** may
press it depends on what the diff touches. These prefixes always need a human
tap:

```
senditai_ng/safety/
senditai_ng/dispatch/killswitch
errorta_liverun/
```

...plus anything under your profiles directory. These are the paths that
decide whether the loop can be stopped at all, so a loop must not be the thing
that edits them unattended.

That list is **code** (`errorta_liverun.fixloop.GUARDED_PATH_PREFIXES`), not
configuration, on purpose: a profile that could shrink it could shrink its way
out of the gate. Adding to it is a one-line edit plus a test. A path the
predicate cannot normalize (absolute, escaping, empty) answers "human" too —
the cost of a false human-only is one button tap.

The predicate is asked **twice**, by two different processes. The fix cycle
asks it when it stages the confirmation, and the Slack bridge asks it again
from the workspace's own merge-back preview immediately before merging. A
staged record that says "safe" while the tree says otherwise is refused, not
merged: the record is a file on disk written minutes earlier, and what the
workspace is actually about to deliver is the only answer worth acting on.

### Operator setup, once per repository

1. **Register an acceptance gate** on the Errorta project the repo maps to:

   ```python
   LedgerStore("<project_id>").set_test_commands(
       {"gate": {"argv": ["/usr/bin/python3", "-m", "pytest", "-q"],
                 "cwd": ".", "timeout_seconds": 300}})
   ```

   argv only (no shell), ≤ 600 s, run under the seatbelt, no network. Without
   a registrable gate the cycle stops at `fix_no_gate` — there is nothing that
   could tell a good fix from a bad one, so it does not guess.

2. **Confirm `dev_repo_read: true`** in `<project>/autonomy.json`, and that the
   dev role is seated on a `claude_cli.*` route — only those members honour it.
   A dev that cannot read the repository cannot fix it. `fix_loop.dev_route`
   (default `claude_cli.opus`) is the route the cycle seats the dev role on
   before it files the task; it must be an available gateway route.

3. **Adopt a Slack channel** for the project with `adopt_project` — it opens
   and binds a NEW channel named from the `project_id` alone (and seats a team
   if the project has none). Pass the `project_id` and nothing else: `start`
   would begin a run now, and the fix loop starts its own. The staged
   acceptance is posted to that channel, so a project with no binding has
   nowhere to put its button and the cycle pauses at `fix_declined`.

4. **Point the profile at it**: `repos[].path` is your checkout (must exist and
   contain `.git`), `repos[].errorta_project` must name a project that already
   exists — both are resolved at load, so a typo is reported by
   `list_live_profiles` rather than minutes after a stall.

> **Existing profiles**: `idle_timeout_s` now defaults to 2400 with a floor of
> 2100 (up from 1200 / 600) — see "Caps" above. An operator profile that still
> declares `idle_timeout_s: 1200` must be edited to `2400` (or any value in
> the 2101–2400 range), or it will fail to load with `idle_below_turn_timeout`.

### Giving a project a trusted gate

Gradle cannot run inside the seatbelt the sandboxed acceptance gate executes
in (network off, synthetic `HOME`, no `JAVA_HOME`), so a Gradle/Maven project
has no registrable *sandboxed* gate at all. With nothing able to produce a
pass/fail signal, an autonomous merge there would be a merge on no evidence
— which is why `osrs-reaper` shipped as `fixable: false`.

A **trusted gate** is the operator-declared way out: a file at
`$ERRORTA_HOME/gates/<project_id>.yaml` (default `~/.errorta/gates/`) that
lists the exact commands the fix loop is allowed to run, unsandboxed. It uses
the same provenance bar as a profile — owned by you, mode
`0600`/`0640`/`0644`, a regular file (not a symlink, and `gates/` itself must
not be a symlink), and it must contain `version: 1`, `created_by: operator`,
and a `project_id` matching the filename stem. The engine never writes this
file; only a human puts it there. If the file is present but fails any of
these checks, the gate registers as invalid and loudly refuses to run rather
than silently falling back to the sandboxed registry — `errorta trusted-gate
<project>` will show the reason code.

Each command's `argv` must start with an absolute path or be exactly
`./gradlew` or `./mvnw`; it may not contain shell metacharacters (`` $ ` | ; &
< > ``) or the banned safety-plane-bypass flags. `timeout_seconds` is bounded
1–1800 (30 minutes, the same ceiling as everything else in the fix loop), and
`scope` is `unit` or `acceptance`. `env.passthrough` is a list of environment
variable *names* only — never values — and any name that looks like a secret
(`*_KEY`, `*_TOKEN`, `*_SECRET`, …) is refused at load.

Start from [`example-trusted-gate.yaml`](../gates/example-trusted-gate.yaml).
Two details matter for Gradle specifically:

- **`--offline`** makes the gate use the warm `~/.gradle` cache rather than
  reaching out mid-run — the same determinism the sandboxed executor got for
  free by having no network at all, just achieved differently since a trusted
  gate is unsandboxed and could reach the network.
- **`HOME` in `env.passthrough`** is what the sandboxed gate could never give
  Gradle: your real home directory, so `~/.gradle` (build cache, wrapper
  distributions) and `JAVA_HOME` resolve the way they do in your own shell.
- **`--no-daemon`** avoids a false-looking failure: a Gradle daemon detaches
  and keeps holding the output pipe open after the command "finishes," which
  the runner reports as `output abandoned: detached child held the pipe` in
  the result's `reason` — even on an otherwise clean pass.

A trusted gate and `require_sandbox` (`errorta test-settings`) refuse each other on
purpose: if the project's registry resolves to the trusted tier and
`require_sandbox` is on, every command in that run comes back `blocked` with
`sandbox_required_by_project` rather than silently running unsandboxed
anyway. Turn `require_sandbox` off for a project before giving it a trusted
gate.

Once `errorta trusted-gate <project>` shows the gate as valid with the
commands you expect, flip that project's `fixable: true` in the live-run
profile — the fix loop will resolve its acceptance-gate registry entirely
from the trusted file from that point on.

### What you will see, and what `live_status` adds

Fix-loop lines are posted per event like every other: triage and its
confidence, the filed task, the dev run, an idle cancellation, the staged
acceptance (with "a human has to approve this one" in the *title* when it is
human-only), the merge, each deploy step, a cap hit, an abandoned cycle and a
withdrawn button. **Mandatory even when muted:** `fix_idle_cancel`,
`fix_accept_staged`, `fix_accepted`, `fix_cycle_cap`, `relaunch_refused`,
`fix_aborted`, `fix_accept_withdrawn`, and any phase change into
`paused_awaiting_human` — a mute quiets routine progress, not an autonomous
merge or the loop giving up.

`live_status` gains `fix_cycle` and `fix_of` (where this run sits in a fix
chain), `fix_repo_id`, `fix_cycles_today`, `fix_cap`, and `fix_paused`.

A stop mid-cycle takes the merge button with it: the pending confirmation is
withdrawn and the dev run is cancelled, so nothing merges minutes after you
asked for a stop. If a human tapped Approve first, that race is reported
rather than fought — the line says the approval was already answered.

## Authoring the OSRS profile — what the first live runs settled

The spec's open questions (§7) were resolved by running the real thing on
2026-08-22 (five runs). Copy these into your profile rather than rediscovering
them:

1. **`osascript` / `cliclick` from the sidecar works** when `jagex-play` is
   routed through Terminal.app by absolute path (the example does this).
2. **Logout proof is `/healthz`, not `/state`.** `/state` requires the
   `X-Agent-Token` header, which probes cannot send; `/healthz` is token-exempt
   and carries `loggedIn`. The logoff check is
   `http_json: {url: http://127.0.0.1:8081/healthz, path: loggedIn, equals: false}`.
3. **Logoff must not depend on the brain.** If the brain has already exited,
   the kill marker logs nothing out and the literal is honestly `ABSENT`. Add a
   `client-logout` teardown step before `logoff-wait` that POSTs
   `/agent/logout` with the token read from `~/.runelite/.agent-token` into a
   header file (never argv). Keep that script outside the repo.
4. **Journal probe**: the box has no `sqlite3` CLI; `osrs-watcher --session
   $SESSION_ID --json --last 1` as a `remote_stdout_advancing` probe is fine
   at a 30 s cadence.
5. **`kill --session` resolves the default `--base-dir`**, the same one a bare
   `run` uses.
6. **Rebuild check**: Gradle leaves an up-to-date jar's mtime alone, so
   `file_mtime_newer` never passes; use `file_exists` and trust the exit code.
7. **Tunnels**: if an `autossh` already reverse-forwards 8081, forward only
   what it does not (8082), or `ExitOnForwardFailure` kills yours.
8. **Probe kinds drive triage.** The deterministic classifier keys on the
   stalled probe's *kind* (`remote_pid_alive` → `brain_pid_dead`,
   `remote_file_mtime_advancing` → `brain_log_stall`,
   `remote_stdout_advancing`/`remote_stdout_matches` → `journal_stall`,
   `http` → `client_port_dead`, `elapsed_lt_s` → nothing); name probes however
   you like.
9. **Caps are real.** 2 launches/hour and a 900 s gap means a shakedown with
   profile fixes takes hours; plan relaunch times instead of loosening caps.
10. **The fix cycle seeds a missing project worktree itself.** `adopt_project`
    does not, and the first `errorta run` (or `errorta setup --confirm`
    followed by a run) still does too — but a fix cycle that arrives first no
    longer pauses `fix_run_failed` on a missing worktree it could have
    created.
11. **`window_shot` needs Quartz.** The `pgrep` pattern
    `RuneLite.app/Contents/MacOS/RuneLite` matches; what was missing was
    `pyobjc-framework-Quartz` in the sidecar's environment. The step now
    reports `quartz_unavailable`, `no process matched`, or `no window
    captured` — three different failures.

## Status (2026-08-23)

Live-verified on the real client and brain, from the terminal and from the
sidecar: the full launch chain, wall-clock stall detection, teardown with
`logoff_verified: PRESENT`, caps, pauses, recovery after a killed sidecar,
triage, operative Focus + fix task, a coding run that opened a PR, and the
Slack narration of all of it.

Not yet demonstrated: a fix cycle that **converges** on its own. On the real
defect of the day (the brain's `tutorialProgress` never reaching its preflight)
the sonnet dev exhausted its repository-read budget on a 657-file repo twice
and misread the symptom; the defect was found and fixed by hand
(senditai-ng `9373014`). Treat the loop as mechanically complete and
model-limited until the convergence slice is measured.

On 2026-08-23, run `916bbd` demonstrated the launch chain and teardown again.
The brain fix (senditai-ng `9373014`) is necessary but not sufficient: the
preflight stalls because it reads a `StateManager` instance that nothing pumps
between the feed gate and the loop. The journal is legitimately silent for up to
120 + 180 seconds of preflight and up to 1800 seconds of a driven tutorial, so
the operator profile's `journal-seq` threshold is now 2100 seconds.

**Measured, 2026-08-23, against that stall.** Three fix cycles ran after the
convergence slice merged; none converged, and nothing wrong was merged:

- `20260823T060652-05f8b9`: triage by kind (`brain_pid_dead` → brain) worked
  and `window_shot` captured its first PNG, but seating the opus dev failed —
  `claude_cli.*` routes read `cli_not_verified` on a sidecar restarted from the
  CLI, because only the desktop panel's Test button had ever warmed the probe
  cache. Fixed: the cycle runs that probe itself before seating.
- `20260823T064621-e3bb5f`: the full path fired (probe, seats sonnet→opus,
  task, run). The opus dev's read turn ran 667 s — past the old 600 s cap — and
  still misdiagnosed: the brief said the probe was "quiet for 46s" and the dev
  (and the reviewer) shipped a heartbeat log line; the probe is a pid check and
  the brain had exited. `fix_no_delivery`. Fixed: the brief now states what the
  probe *kind* means.
- `20260823T072440-b81258`: the dev never reached the brief — the ledger still
  carried the previous cycle's Focus, six open PRs and a broken artifact, the
  gate failed on those, and the dev reported `blocked`. `fix_no_delivery`.
  Fixed: a cycle retires the previous cycle's Focus, tasks and PRs before it
  files its own.
- A fourth launch hit `fix_cycle_cap` (3/day), as designed.

Fix rate on this stall: 0/3 (0/4 counting 2026-08-22's sonnet attempt). Each
failure was a mechanical gap in the loop rather than the model, and each gap is
now closed; the model's own diagnosis has not yet been measured on a clean
cycle. The brain defect was fixed by hand (senditai-ng `cf9bd33`: the preflight
pumps the notification queue instead of sleeping) so that live runs can reach
the tutorial and the loop can be measured on the next genuinely new stall.

### Follow-ups, in priority order

- The `errorta_tunnels` registry is in-memory, so boot recovery reports a prior
  sidecar's tunnel `ABSENT` rather than closing it.
- `_PATH_RE`/`_TOKEN_RE` use `.match()` + `$`; switch to `fullmatch`.
