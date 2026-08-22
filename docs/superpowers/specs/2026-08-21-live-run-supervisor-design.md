# Live-Run Supervisor — Design (Slice 1 of 3)

**Date:** 2026-08-21
**Status:** Design, approved in brainstorm (architect re-analysis accepted in full). Not yet implemented.
**Depends on:** Slack bridge slices 1–6 (studio, run control, autopilot, close-the-loop), merged.
**New module:** `python/errorta_liverun/` (sibling of `errorta_slack`, outside `errorta_council`).
**Touched:** `errorta_tools/runner/remote.py`, `errorta_tunnels/manager.py`, `errorta_slack/{tools,outbound,connection}.py`, `errorta_app/{server,slack_lifecycle}.py`.
**Series:** Slice 1 supervisor (this doc) → Slice 2 fix-loop closure → Slice 3 perception (deferred indefinitely).

---

## 1. Problem

The owner runs a long-lived program split across two hosts — a Python "brain" on
a remote box (`ssh senditai`) driving a Java game client on this Mac — and
supervises live runs from a Claude Code session following a human runbook
(`senditai-ng/CLAUDE.md` §7). That session has **no wake signal**: once it has
kicked off the launch steps it waits on nothing, so stalls go unnoticed for ~20
minutes. The owner wants the whole launch → watch → stop-safely → fix → redeploy
→ relaunch loop to be driven from Slack chat with no human in the loop.

Slice 1 delivers the part that is useful alone (§2 F-A explains the caps): a **wall-clock supervisor** that launches a declared
profile, watches declared signals, detects stalls, tears down safely with
logoff evidence, and narrates all of it to Slack. It does **no** fixing.

## 2. Grounded facts

These were read from the code and docs on 2026-08-21; several overturn the
first draft of this design.

- **F-A. The previous account was banned on 2026-08-20** (a replacement account is now in use) (`senditai-ng/docs/
  superpowers/specs/2026-08-20-session-risk-budget-design.md` §1 — six login
  cycles in 33 minutes). The brain now carries a persisted **session risk
  budget** (3 live sessions/hour, 600 s minimum gap, 12/day, 2 consecutive
  failed receipts) and *refuses* to start above it; `--ignore-risk-budget`
  overrides it (`senditai_ng/cli.py:356`). An autonomous launch→fail→relaunch
  loop is exactly the behaviour that caused that ban. Errorta's caps must sit
  strictly inside the brain's budget, a brain-side refusal is terminal, and
  Slice 1 is acceptance-tested against a **fake profile** before any run on
  the replacement account.
- **F-B. `RuntimeProcessManager` cannot launch this client.** It is bound to the
  Errorta-owned worktree (`runtime_process.py:391-411`) and wraps children in the
  seatbelt sandbox with a synthetic HOME (`:266-293`, `preview.py:87-126`). The
  client is launched by `senditai-ng/jagex-play` (osascript + Accessibility +
  `cliclick`, opens `Jagex Launcher.app`); the RuneLite process is not a child
  of anything Errorta spawns. `runtime_process` is a *pattern to copy*
  (process-group teardown, log pump, monitor thread, orphan reaping), not a
  component to reuse.
- **F-C. Errorta has no wall-clock stall detection anywhere.** Every autonomy
  detector is iteration-counted by design (`autonomy.py:1259-1266`); the only
  time limits are per-turn model timeouts (600 s CLI, `reasoning_budget.py:78`)
  and probe windows.
- **F-D. The Slack bridge learns everything by polling ledger state every 15 s**
  (`outbound.py:507-554`, `_current_items` at `:272`) and dedupes by a
  posted-marker set (`:295-311`). Slack tools call the engine **in-process**
  through injectable `ToolDeps` seams (`tools.py:307-340`), never over HTTP.
  `launch_runtime`/`stop_runtime` (`tools.py:94-101`, `:399`) are the precedent
  for a runtime verb.
- **F-E. Repo content is untrusted by design.** `next_goal.py:256-300` fences a
  repo's `CLAUDE.md` excerpt behind a per-call nonce because "any CLAUDE.md in
  any adopted repo addresses the model directly."
- **F-F. Remote execution is a fail-closed stub** (`errorta_tools/runner/
  remote.py:11-24`); `ExecutionLocation = Literal["local","remote_ssh"]` already
  exists (`runner/types.py:15`). Routing `ssh` through `LocalToolRunner` cannot
  work: synthetic HOME hides `~/.ssh` (`code_exec.py:128-134`), the env allowlist
  has no `SSH_AUTH_SOCK` (`runner/env.py:14-26`), and network is refused without
  the sandbox (`code_exec.py:90-91`). `errorta_tunnels/manager.py:88-109` already
  owns a hardened ssh argv (`BatchMode=yes`, `ConnectTimeout=10`,
  `ServerAlive*`, token validation) but only for `-L` forwards, and its
  `_Child.kill` (`:126-139`) kills the leader only, not the group.
- **F-G. The house pattern for long-lived background work** is
  `threading.Thread` + `threading.Event` + backoff loop (`errorta_tunnels/
  manager.py:304-350`, `slack_lifecycle.py:105-150`). There is no scheduler
  framework. Sidecar shutdown tears resources down in the lifespan `finally`
  (`server.py:484-497`); boot reconciles orphans (`server.py:267-325`). The
  sidecar dies with the desktop shell (`parent_watchdog.py`).
- **F-H. Sessions are never auto-resumed across a sidecar restart** (F101
  contract, `runtime_process.py:9-12`).
- **F-I.** `_start_outbound` never forwards `interval_s`/`timeout_minutes`
  (`slack_lifecycle.py:131-136`), so `config["timeout_minutes"]` is dead. A bare
  "stop" in a channel fires `stop_runtime`, not `stop_run`
  (`connection.py:108`, `:400`).
- **F-J. Screenshots have no consumer.** `MemberCaller = Callable[[dict, str],
  str]` (`runner.py:92`); the gateway is text-only; SPEC-14's `review_screenshot`
  was withdrawn for that reason (`autonomy.py:275-291`). `capture_app_window`
  (`preview.py:159-184`) takes a pid set and captures window-scoped PNGs.

## 3. Design

### 3.1 Principles

1. **Competence boundary, applied to ops.** Deterministic code owns
   mechanism — launch, probe, stall, kill, logoff. No model composes any argv
   that runs outside the sandbox. Model-chosen values are enumerations only
   (profile name, later: repo id, task text).
2. **The brain's risk budget is the outer law.** Errorta's caps are tunable
   only *downward* from defaults that sit inside it.
3. **Teardown is a state, not a step.** It runs on every exit path and reports
   its evidence as literals; absence of a literal is reported as absence,
   never read as success.
4. **Wall-clock everywhere.** Every probe has `every_s` and `stall_after_s`.
5. **Profiles are operator-owned data, not repo content** (F-E). Slack may
   select; it may never author.

### 3.2 Profile — `ERRORTA_HOME/liverun/profiles/<name>.yaml`

Loaded by `errorta_liverun/profile.py`, validated **fail-closed at load**.
Invalid profile → the run never starts and the reason is posted. Schema
(YAML; `name` is the filename stem):

```yaml
version: 1
created_by: operator                # literal; anything else rejected
hosts:
  senditai: {ssh_host: senditai}    # alias resolved via ~/.ssh/config; known_hosts required
tunnels:
  - id: reverse
    host: senditai
    reverse: [{remote_port: 8081, local_port: 8081}, {remote_port: 8082, local_port: 8082}]
launch:                             # ordered; each step must pass its check before the next
  - name: quit-old-client
    local: {argv: [/Users/OPERATOR/GitHub/senditai-ng/jagex-quit]}
    check: {pgrep_absent: "RuneLite.app/Contents/MacOS/RuneLite"}
    timeout_s: 30
  - name: rebuild-jar
    local: {argv: [./gradlew, :client:shadowJar, :client:microbotLatestJar], cwd: /Users/OPERATOR/GitHub/osrs-reaper}
    check: {file_mtime_newer: {path: /Users/OPERATOR/GitHub/osrs-reaper/runelite-client/build/libs/microbot-shaded.jar, than: step_start}}
    timeout_s: 900
  - name: launch-client
    local: {argv: [osascript, -e, 'tell application "Terminal" to do script "/Users/OPERATOR/GitHub/senditai-ng/jagex-play"']}
    check: {all: [{file_exists: /Users/OPERATOR/.runelite/.agent-token}, {http: {url: "http://127.0.0.1:8081/state", expect_status: 200}}]}
    timeout_s: 180
  - name: tunnel
    tunnel: reverse
    check: {tunnel_up: reverse}
    timeout_s: 30
  - name: brain
    remote: {host: senditai, argv: [...], detach: true, pidfile: ~/.senditai_ng/liverun.pid, stdin_file: /Users/OPERATOR/.runelite/.agent-token}
    check: {remote_pid_alive: {host: senditai, pidfile: ~/.senditai_ng/liverun.pid}}
    timeout_s: 60
watch:                              # polled concurrently; wall-clock
  - {id: client-state,  every_s: 15, stall_after_s: 60,  on_stall: stop, probe: {http: {url: "http://127.0.0.1:8081/state"}}}
  - {id: brain-alive,   every_s: 15, stall_after_s: 45,  on_stall: stop, probe: {remote_pid_alive: {host: senditai, pidfile: ~/.senditai_ng/liverun.pid}}}
  - {id: brain-log,     every_s: 30, stall_after_s: 180, on_stall: stop, probe: {remote_file_mtime_advancing: {host: senditai, path: ~/.senditai_ng/liverun.log}}}
  - {id: journal-seq,   every_s: 30, stall_after_s: 180, on_stall: stop, probe: {remote_stdout_advancing: {host: senditai, argv: [sqlite3, -readonly, "...", "select max(seq) from ..."]}}}
  - {id: feed-live,     every_s: 60, stall_after_s: 120, on_stall: stop, probe: {remote_stdout_matches: {host: senditai, argv: [...], regex: '"event_feed_live":\s*true'}}}
  - {id: session-clock, every_s: 60, stall_after_s: 0,   on_stall: stop, probe: {elapsed_lt_s: 3600}}
evidence:                           # run on every stop, bounded, redacted
  - {id: brain-log-tail, remote: {host: senditai, argv: [tail, -n, "200", ~/.senditai_ng/liverun.log]}}
  - {id: watcher,        remote: {host: senditai, argv: [...osrs-watcher, --last, "20"]}}
  - {id: client-window,  window_shot: {pgrep: "RuneLite.app/Contents/MacOS/RuneLite"}}
  - {id: client-state,   http: {url: "http://127.0.0.1:8081/state"}}
teardown:                           # ordered; always; each sub-step records evidence
  - {name: kill-marker,   remote: {host: senditai, argv: [...senditai_ng.cli, kill, --session, "$SESSION_ID"]}, timeout_s: 20}
  - {name: logoff-wait,   check: {http_json: {url: "http://127.0.0.1:8081/state", path: gameState, not_equals: LOGGED_IN}}, timeout_s: 45, evidence_literal: logoff_verified}
  - {name: brain-stop,    remote_signal: {host: senditai, pidfile: ~/.senditai_ng/liverun.pid, signal: TERM, grace_s: 15, then: KILL}}
  - {name: quit-client,   local: {argv: [/Users/OPERATOR/GitHub/senditai-ng/jagex-quit]}, timeout_s: 30}
  - {name: tunnel-down,   tunnel_close: reverse}
caps:                               # may only be lowered below defaults
  max_launches_per_hour: 2
  min_launch_gap_s: 900
  max_launches_per_day: 8
  max_consecutive_failed_cycles: 2
ban_signals:                        # regexes over any evidence/log text; match → paused_awaiting_human
  - 'Account is banned'
  - 'Login failed'
```

The OSRS profile above is **data**; the schema and supervisor are generic. The
exact remote argv (brain invocation, `sqlite3` query, watcher path) and the
`/state` field that proves logoff are filled in when the profile is authored
(§7 unverified items), not in this spec.

**Validator rules (fail-closed, all tested):**

- `created_by == "operator"`; `version == 1`; unknown keys rejected.
- Every `local.argv[0]` is an **absolute path** or one of an allowlist
  (`osascript`, `pgrep`, `./gradlew` only with an absolute `cwd`) — CLAUDE.md §7
  records relative-path launches silently failing five times.
- Every step argv is validated as a list of strings; no shell, no globbing,
  no `$VAR` expansion except the supervisor-injected `$SESSION_ID` and
  `$RUN_ID` tokens, which are substituted **after** validation and only from
  supervisor-generated values.
- Banned tokens anywhere in any argv: `--ignore-risk-budget`,
  `--no-safety-plane`. A `remote` launch step whose argv contains
  `senditai_ng.cli` and `run` must also contain `--max-session-seconds`,
  `--receipt-id`, and `--require-live-feed`.
- `caps` values may not exceed the defaults listed above.
- `teardown` must contain at least one sub-step with
  `evidence_literal: logoff_verified`.
- Each `host` must already be in `~/.ssh/known_hosts` (checked at load via
  `ssh-keygen -F`); the profile load reports `host_unknown` otherwise.
- The profile file must be owned by the current user with mode `0600`/`0644`
  and live under `ERRORTA_HOME/liverun/profiles/`; symlinks rejected.

### 3.3 Step primitives — `errorta_liverun/steps.py`

One function per primitive, each returning a `StepResult{ok, started_at,
ended_at, exit_code?, stdout_tail, stderr_tail, evidence_refs, timed_out}`
with previews capped and passed through `runtime_process.redact_log_line`.

| Primitive | Implementation |
|---|---|
| `local` | New `spawn_tracked()` in `errorta_tools/runner/preview.py` style: `start_new_session=True`, **no sandbox**, no synthetic HOME, stdout/stderr to evidence files, `os.killpg` on timeout. The pgid is recorded in the run state before the process is awaited. |
| `remote` | `RemoteToolRunner` (§3.4). `detach: true` runs `setsid nohup <argv> > <log> 2>&1 & echo $! > <pidfile>` **constructed server-side from validated argv via `shlex.join`**, never from a string. `stdin_file` streams a local file to the remote command's stdin (the agent token never appears in argv or `ps`). |
| `remote_signal` | `kill -<SIG> $(cat pidfile)` with grace then `KILL`. |
| `tunnel` / `tunnel_close` / `tunnel_up` | `TunnelManager` with new `reverse_forwards` (§3.5). |
| `http`, `http_json` | `probe_http`-style GET with 5 s timeout; `http_json` reads a dotted path. |
| `file_exists`, `file_mtime_newer`, `pgrep`, `pgrep_absent` | local stat / `pgrep -f`. |
| `remote_pid_alive`, `remote_file_mtime_advancing`, `remote_stdout_advancing`, `remote_stdout_matches` | one short `RemoteToolRunner` call each; "advancing" compares against the probe's last observed value in run state. |
| `elapsed_lt_s` | wall-clock since `launching` completed. |
| `window_shot` | `capture_app_window(pids=pgrep(...))` → PNG under evidence. |

Invariant (tested): the step executor refuses any argv that is not
byte-identical to the validated profile's argv after token substitution.

### 3.4 `RemoteToolRunner` — `errorta_tools/runner/remote.py`

Replaces the stub. Builds a fixed argv:

```
ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes
    -o ServerAliveInterval=15 -o ServerAliveCountMax=3
    [-p PORT] [-i KEY] [-l USER] <host> -- <shlex.join(remote_argv)>
```

- Host/user/key tokens validated with `errorta_tunnels.manager._validate_token`
  (rejects leading `-`); `StrictHostKeyChecking=yes`, not `accept-new`.
- Spawned with `start_new_session=True`; `os.killpg` on timeout; returns
  `ToolRunnerResult` with capped/redacted previews and `timed_out`.
- Inherits the real user env (`HOME`, `SSH_AUTH_SOCK`) — it is a supervisor
  egress primitive, not a member tool. It is **not** registered in the member
  tool gateway; `errorta_council` never imports it (existing import-lint tests
  stay green).
- Policy: each step is evaluated through `PolicyEngine` with
  `phase=REMOTE_EGRESS` (new constant) or `CODE_EXEC`, `egress_class` from
  `execution_location`, and `policy={"action":"allow"}` — justified solely by
  the profile being operator-authored (§3.2 invariant). The audit record is
  written either way.

### 3.5 Reverse tunnels — `errorta_tunnels/manager.py`

- `TunnelSpec` gains `reverse_forwards: tuple[tuple[int,int], ...] = ()`
  (remote_port, local_port); `remote_port` becomes optional when
  `reverse_forwards` is non-empty. Argv emits `-R 127.0.0.1:<rp>:127.0.0.1:<lp>`
  per pair. `ExitOnForwardFailure=yes` already makes a stale remote bind fail
  loud; `_watch`'s reconnect/backoff is reused unchanged.
- `_Child.kill` switches to `os.killpg` on the child's pgid (the child is
  already `start_new_session=True` — verify; if not, make it so).

### 3.6 Supervisor — `errorta_liverun/supervisor.py`

One daemon thread per live run; at most one live run per profile and one per
bound project. State machine:

```
idle → launching(step i) → watching → stopping(reason) → stopped
                 │                │                     ↘ paused_awaiting_human
                 └── step failed ─┴──────────────────────→ failed
boot recovery of any non-terminal run ───────────────────→ lost_on_restart
```

- **launching:** run each launch step; poll its `check` every 2 s until pass
  or `timeout_s`; a failed step → `stopping("launch_step_failed:<name>")`.
  Record the pgids/pids/tunnel ids/remote pidfiles we own *before* spawning
  each child (so recovery can reach them).
- **watching:** each probe runs on its own cadence in the supervisor thread's
  select loop (no thread per probe); `last_ok_at` per probe persisted. When
  `now - last_ok_at > stall_after_s`: `on_stall: warn` posts once per stall
  episode and keeps watching; `on_stall: stop` → `stopping("stall:<id>")`.
- **stopping:** collect `evidence` (bounded, concurrent, each with its own
  timeout), then run `teardown` sub-steps in order; each sub-step's result and
  `evidence_literal` (if its check passed) is appended to the event log. Then
  `stopped` (or `paused_awaiting_human` if any ban signal matched any evidence
  text, or caps are exhausted).
- **Caps** are checked in `idle → launching` against a persisted launch ledger
  `ERRORTA_HOME/liverun/launches.jsonl` (survives restart, like the brain's
  budget). Exceeded → `paused_awaiting_human`, one Slack post, no retries. A
  brain exit code 3 (`REFUSED:`) is terminal for the cycle and counts as a
  failed cycle.
- **Persistence:** `ERRORTA_HOME/liverun/runs/<run_id>/state.json` written
  atomically (tmp + rename) after every transition, plus `events.jsonl` with a
  monotonic `seq`, plus `evidence/` files. `RuntimeProfileStore` is the
  pattern (`runtime.py:320-335`).
- **Recovery:** on sidecar boot, next to `reap_all_persisted_orphans`
  (`server.py:296-325`): every run not in a terminal state gets the **full
  teardown** run against whatever is still alive (pids/pgids/pidfiles/tunnels
  from its state file), is marked `lost_on_restart` with that evidence, and is
  never resumed (F-H). Teardown of all live runs is registered in the lifespan
  `finally` next to `_tunnels.teardown()` (`server.py:484-497`). The spec
  states plainly: an overnight run requires the Errorta desktop app to stay
  open, because the sidecar dies with it (F-G).
- **Stop is never gated:** `stop()` from any caller flips to `stopping`
  immediately.

### 3.7 Slack — verbs and progress

Catalog additions in `errorta_slack/tools.py`, implemented through new
`ToolDeps` seams (`liverun_list_fn`, `liverun_start_fn`, `liverun_stop_fn`,
`liverun_status_fn`, `liverun_resume_fn`), defaulting to the real supervisor
and injected with fakes in tests:

| Verb | Trust | Notes |
|---|---|---|
| `list_live_profiles` | R | names + validation status + last run summary |
| `start_live_run(profile)` | **C** | staged confirmation; autopilot auto-fires it (autopilot spec §3.4) |
| `stop_live_run` | R | must never wait on approval |
| `live_status` | R | phase, elapsed, per-probe `last_ok` ages, cap headroom |
| `resume_live_run` | **C, human-only** | clears `paused_awaiting_human`; **autopilot never auto-fires this verb** — the first per-verb carve-out in `_handle_staged_confirmations` (`connection.py:749-815`) |

- A bare "stop"/"cancel"/"abort" in a channel with an active live run
  (`_CANCEL_TEXTS`, `connection.py:108`) calls `stop_live_run` **in addition
  to** the existing `stop_runtime` behaviour.
- Progress: a fifth item source in `outbound._current_items` reading the run's
  `events.jsonl` with markers `liverun:<run_id>:<seq>`. Posted: each launch
  step result, each stall warning, the stop reason, every teardown sub-step
  with its literal (and `logoff_verified: ABSENT` when it did not pass), cap
  exhaustion, ban-signal pause. Stop/pause/lost events are **mandatory**
  (post even when muted), like run-state items (`outbound.py:267`). The
  `client-window` PNG is attached on stop.
- Fix F-I while there: forward `interval_s`/`timeout_minutes` from config in
  `_start_outbound`.
- `auth.is_allowed` fails closed on empty allowlists (`connection.py:243`);
  live-run verbs inherit the same allowlist.

### 3.8 Safety summary (what is never autonomous)

1. Authoring or editing a profile — operator, on disk, only.
2. `resume_live_run` after any ban-class signal or cap exhaustion.
3. Any argv composition by a model.
4. Reading absence as health: a missing `logoff_verified` literal is posted as
   absent.
5. Evidence hygiene: redaction via `redact_log_line` before anything reaches
   the ledger or Slack; the agent token travels by stdin only; no game chat
   text is ever posted (it is untrusted content — CLAUDE.md §7).

## 4. Testing

Coverage shape first (memory: every prior code review found its Critical on a
path with no test). The three zero-coverage paths Slice 1 creates are remote
execution, reverse tunnels, and boot-recovery-with-live-children; each gets a
test before any OSRS profile is written.

- **Profile validator:** table-driven accept/reject for every rule in §3.2,
  including banned tokens, relative argv, cap raising, missing
  `logoff_verified`, unknown host, symlinked file.
- **Step primitives:** each primitive against local fakes; `local` timeout
  kills the whole process group (spawn a child that forks); `remote` with a
  fake `ssh` binary on `PATH` asserting the exact argv and `--` separator;
  `stdin_file` reaches the fake's stdin and never its argv; argv-identity
  invariant.
- **Tunnels:** argv emission for `-R` pairs; `_Child.kill` kills the group.
- **Supervisor state machine (fake clock):** launch step failure → teardown;
  probe stall at exactly `stall_after_s` → stop; `warn` posts once per
  episode; caps exhausted → `paused_awaiting_human` with one event; ban regex
  on evidence → pause; brain rc 3 → failed cycle, no relaunch; `stop()` during
  `launching` interrupts and tears down.
- **Recovery:** state file of a `watching` run with live fake children → boot
  reconcile tears them down, marks `lost_on_restart`, does not relaunch.
- **Slack:** catalog entries, trust classes, `resume_live_run` not auto-fired
  under autopilot, bare "stop" reaches `stop_live_run`, outbound markers
  dedupe and mandatory posting when muted, PNG attachment on stop.
- **Acceptance (fake profile, in `tests/acceptance/`):** a local Python
  "client" serving `/state` on a loopback port and a local "brain" script
  (via `ssh localhost` when a key is present, else a `local` step) that stops
  writing its log after N seconds. Assert: stall detected within
  `stall_after_s + every_s`; teardown sub-steps run in order; final state
  `stopped` with `logoff_verified` present; outbound item stream contains
  exactly the expected markers; a simulated sidecar restart mid-run yields
  `lost_on_restart` with teardown evidence and no relaunch.

## 5. Out of scope (Slice 2 / 3)

- Evidence bundle → `add_task(role="dev", detail=<fenced brief>)` → run with
  `dev_repo_read` → staged C-class accept → per-repo `deploy` (jar rebuild
  locally; rsync accepted tree to the box) → relaunch under caps; deterministic
  evidence classifier → PM triage → human; supervisor-side run-idle detector
  (> 600 s). All Slice 2.
- Any model seeing a screenshot. Slice 3, deferred until a stall class is
  shown to be invisible to journal + state + log-mtime probes.

## 6. Success criteria

1. With the fake profile, a stall is detected and torn down with
   `logoff_verified` within the declared window, and Slack shows every step —
   no human action, no polling by a human or an LLM session.
2. Killing the sidecar mid-run and restarting it leaves no orphan child on
   either host and posts `lost_on_restart`.
3. No code path lets a model-composed string reach a `local` or `remote` argv
   (grep + test).
4. The supervisor refuses to start when the brain's budget or Errorta's caps
   say no, and says which.

## 7. Unverified — resolve while authoring the OSRS profile, not in code

- Whether `osascript`/`cliclick` succeed when spawned from the sidecar
  (Accessibility is per-app; CLAUDE.md §7 reports error `-25211` from agent
  shells). The `launch-client` step above routes through `Terminal.app` for
  that reason; the profile validator reports, never assumes.
- The exact `/state` JSON field that proves "logged out" on `:8081`.
- Whether `osrs-watcher --json` is cheap enough at a 30 s cadence, or a direct
  read-only `sqlite3` query is preferable for `journal-seq`.
- Whether `senditai_ng.cli kill --session` resolves the same `--base-dir` the
  run used (`cli.py:606-609`).
