# Trusted unsandboxed gate — design

**Date:** 2026-08-23
**Status:** approved for planning (autonomous session; the trust-model decision is recorded below and is the one a human may want to revisit)
**Scope:** `errorta_council/coding/{trusted_gate.py (new), testing.py, evidence.py, gate_state.py, ledger.py}`, `errorta_liverun/fixloop.py` (gate label only), `errorta_slack/outbound.py` (label only), `errorta_cli` (one read-only command), docs.

## Why

`osrs-reaper` ships `fixable: false` in the live-run profile because it has no acceptance gate: the gate executor (`testing._run_one`) runs every registered command under seatbelt with network off, a synthetic `HOME`/`TMPDIR`, and a five-variable environment (`PATH`, `HOME`, `TMPDIR`, `TMP`, `TEMP`). Gradle cannot run there for four independent reasons: the wrapper distribution lives in `$HOME/.gradle` (synthetic, empty, and un-downloadable with the network off); dependency resolution needs the network; `GRADLE_USER_HOME`/`JAVA_HOME` are structurally unreachable; and a `gradlew` red has no `cannot_verify` escape hatch, so it wedges as a hard red. The README has said since Slice 2 that lifting this needs its own slice — this is it.

The operator already runs exactly this build, unsandboxed, with a full environment, from a trusted file: the live-run profile's `rebuild-jar` step (`./gradlew :client:shadowJar …`, `timeout_s: 900`). The trust model that makes that acceptable is the one this slice reuses.

## Non-goals

- No change to the sandboxed registry path (`set_test_commands` → `_run_one`): every project without a trusted gate file behaves exactly as today.
- No model-reachable way to declare, edit, or select a trusted gate. Not a registry field, not a route body flag, not a Slack verb argument.
- No loosening of the merge gate, the human-only verbs, or the live-run caps.
- No Gradle-in-sandbox attempt (approach C) — recorded as future hardening.

## Trust model (the decision)

A **trusted gate** is an operator-authored file at `$ERRORTA_HOME/gates/<project_id>.yaml`, validated with the *same* provenance guard as a live-run profile (`errorta_liverun/profile.py::_file_guard` semantics, reimplemented locally so the council package does not import the liverun package): not a symlink, resolves inside the gates directory, owned by the current uid, mode ∈ {0600, 0640, 0644}, `version: 1`, `created_by: operator`. The engine never writes to that directory; the desktop app and Slack never write to it; there is a grep test that no code path under `errorta_council`, `errorta_app`, `errorta_slack`, `errorta_liverun` opens a file under `gates/` for writing.

Consequence: a trusted gate can only exist because a human put it there with the right mode. That is the same bar the live-run supervisor already sets for launching real commands on a real host, and a gate that builds a jar is strictly less powerful than a profile that launches the game client.

The file declares commands with the live-run argv hygiene: argv is a list of strings; `$`, `` ` ``, `|`, `;`, `&`, `<`, `>` rejected in any element; `argv[0]` absolute, or `./gradlew` / `./mvnw` with an absolute or worktree-relative `cwd`; banned tokens as in liverun. Nothing is substituted into argv. Unknown keys fail the load.

## The file

```yaml
version: 1
created_by: operator
project_id: osrs-reaper            # must equal the filename stem
commands:
  - id: compile
    argv: ["./gradlew", ":client:compileJava", "--offline", "--console=plain", "-q"]
    cwd: "."                       # worktree-relative (no leading /, no ..)
    timeout_seconds: 900           # 1..1800
    scope: unit                    # unit | acceptance (same meaning as the registry)
  - id: unit-tests
    argv: ["./gradlew", ":client:runUnitTests", "--offline", "--console=plain", "-q"]
    cwd: "."
    timeout_seconds: 1500
    scope: unit
env:                               # names copied from the operator's environment; values never in the file
  passthrough: [PATH, HOME, JAVA_HOME, GRADLE_USER_HOME, GRADLE_OPTS, LANG, LC_ALL, TMPDIR]
```

Validation rules beyond the guard: `commands` non-empty, ≤ 8; `id` through `safe_segment`; `timeout_seconds` integer in `[1, 1800]`; `scope` ∈ {unit, acceptance}, default unit; `env.passthrough` names match `^[A-Z][A-Z0-9_]*$`, ≤ 32, and must not match the secret-name patterns `errorta_tools/runner/env.py` already strips (`*_TOKEN`, `*_SECRET`, `*_KEY`, `*PASSWORD*`, `AWS_*`…) — a trusted gate can see the JDK, not the keychain. `HOME` is the operator's real home when listed (that is the point: `~/.gradle`), else synthetic. Values are read from the sidecar's `os.environ` at run time; nothing in the file carries a value.

## Execution

New module `errorta_council/coding/trusted_gate.py`:

- `load_trusted_gate(project_id) -> TrustedGate | None` — `None` when no file; raises `TrustedGateError(code)` when a file exists but fails the guard or validation (a broken trusted file is a loud failure, never a silent fallback to the sandboxed tier).
- `run_trusted_gate(gate, *, workspace_root, head, should_cancel) -> list[TestRunRecord-shaped dicts]` — runs each command with `subprocess.Popen(argv, cwd=<workspace_root>/<cwd>, env=<passthrough>, start_new_session=True)` (no shell, no sandbox wrapper), captures stdout/stderr with the same 2 MB / 4000-char caps as `testing._run_one`, enforces the timeout with `SIGTERM` then `SIGKILL` on the process group, and returns records with `sandbox: "trusted"` and `network_allowed: True` so the evidence trail says what ran where.

`testing.run_test_commands(...)` gains one branch at the top: if `load_trusted_gate(project_id)` returns a gate, run it and **return its records without running the sandboxed registry** — a project has one gate tier, chosen by the operator's file, never merged with the other. The existing `require_sandbox` project setting, when true, refuses a trusted gate (`sandbox_required_by_project`) — the two operator switches cannot contradict each other silently.

`evidence._tests_required` (gate availability) becomes `bool(store.get_test_commands()) or trusted gate present or runnable runtime`. `gate_state.latest_gate_text` renders `sandbox: trusted` records with the prefix `[trusted, unsandboxed]`. `fixloop._gate_label` / the Slack `fix_task` and `fix_accept_staged` lines show the label so a human reading the channel knows what verified the diff. `merge_review` needs no change: it reads test runs by head, and trusted records carry the head.

`_missing_build_dep` (runner.py:4640) gains `gradlew`/`mvnw` markers so a *sandboxed* project whose registry somehow names Gradle degrades to `cannot_verify` instead of a hard red — the pre-existing wedge the README warns about, fixed in passing because this slice is the one touching the question.

## Operator surface

- `errorta gate show [<project>]` (read-only): prints the trusted gate file's validation verdict and commands, or "none". No write command — the whole point is that only an editor in the operator's hands writes that file.
- `docs/liverun/README.md` "Why osrs-reaper ships as fixable: false" is rewritten: how to author the file, the `--offline` recommendation (the worktree is a copy of a repo whose `~/.gradle` cache is warm; `--offline` keeps the gate deterministic and keeps the plain-HTTP Nexus out of the gate), the `HOME` passthrough rationale, and the instruction to flip the profile's `fixable` to true once `errorta gate show` is green.
- `docs/gates/example-trusted-gate.yaml`: the osrs-reaper skeleton with `# FILL:` lines, invalid by construction like the live-run example.

## Worktree note (plan must verify)

The fix cycle now seeds a missing worktree by copying the repo (`ApplyWorkspace.ensure`). For osrs-reaper the copy must skip `build/`, `.gradle/`, `cache/` and the 82 MB shaded jars or the seed takes minutes and the gate rebuilds from nothing; the plan verifies what `_copy_ignore` excludes and adds those patterns if absent. A cold `compileJava` on the copied tree with a warm `~/.gradle` is expected to fit in 900 s; the first live run measures it.

## Error handling

| Situation | Outcome |
|---|---|
| no file | sandboxed tier as today |
| file fails guard/validation | `TrustedGateError` → gate run records `status: blocked, reason: trusted_gate_invalid:<code>`; `gate_available` stays True so the project does not silently lose its gate; the tester/PM see the reason |
| `require_sandbox` true and file present | `blocked, reason: sandbox_required_by_project` |
| command times out | `timed_out` record, process group killed, later commands skipped |
| non-zero exit | `failed` with captured tail — a real red |

## Testing

- `tests/council/test_trusted_gate.py`: guard (symlink, outside dir, wrong owner via monkeypatched `os.getuid`, bad mode), validation (each rule), env passthrough filtering (secret names refused; `HOME` real when listed), `run_trusted_gate` with real subprocesses (`/bin/sh` is not allowed as argv0 — use `/usr/bin/true`, `/usr/bin/false`, and a `sleep` for the timeout/kill path), record shape (`sandbox: trusted`, `head`, caps).
- `tests/coding/test_trusted_gate_tier.py`: `run_test_commands` takes the trusted branch and does not run the registry; `require_sandbox` refuses; `_tests_required` sees the file; `latest_gate_text` prefix; `_missing_build_dep` gradlew marker.
- Grep test: nothing under the four packages writes to `gates/`.
- `tests/cli/test_gate_show.py`: read-only command output for none / valid / invalid.
- Live: author `~/.errorta/gates/osrs-reaper.yaml`, run `errorta gate show osrs-reaper`, seed the worktree, run one coding run and confirm a `sandbox: trusted` test run records for a head; then flip `fixable: true` and let the next reaper-class stall exercise the fix loop.

## Decisions taken in this session (flag if you disagree)

- Trust by operator-owned file (A), not by registry flag (B) or sandbox widening (C).
- Trusted gate replaces, never merges with, the sandboxed registry for that project.
- `HOME` passthrough allowed by explicit listing; secret-pattern names refused even when listed.
- Timeout ceiling 1800 s for trusted commands (registry stays 600).
