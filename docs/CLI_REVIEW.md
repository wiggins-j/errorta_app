# CLI code review — recommendations

A code-quality review of the `errorta_cli` package (~11,200 lines, plus ~7,200
lines of tests in `python/tests/cli/`), 2026-07-23. Overall assessment: strong
for an alpha — the hard-to-retrofit parts (process lifecycle safety, exit-code
contracts, dual-surface parity) are the best-engineered parts. The
recommendations below are incremental fixes, not architectural changes.

## What's working well (keep doing this)

- **Single command registry** (`errorta_cli/registry.py`): argv and REPL
  front-ends dispatch through one `dispatch()` path, so surface parity is a
  structural property, locked by `test_registry_parity`.
- **Stable exit-code contract** (`errorta_cli/errors.py`): twelve documented
  codes, each backed by a typed exception, with a contract test. Run-outcome
  classification (`runstream.py:classify_exit`) is an allowlist that fails
  closed — unknown stop reasons exit non-zero, never false success — and
  `test_every_engine_stop_reason_is_triaged` locks it against engine drift.
- **Sidecar lifecycle** (`errorta_cli/sidecar.py`): cross-process `flock`
  around discover-or-spawn, retry-before-declaring-dead health probes,
  pid-identity (not just port) checks closing a TOCTOU window, kill-and-reap on
  failed spawn, and a consistent refuse-rather-than-race posture. Blip-tolerant
  polling in `runstream.py` detaches gracefully instead of failing a live run.
- **Deliberate test seams** (`probe_healthz`, `_launch`, injectable
  `sleep`/`emit`/`transport`) used consistently across modules.

## Recommendations

### 1. Harden the hand-rolled argument parsing (highest priority)

Typer/Click parsing is deliberately bypassed for registry commands
(`allow_extra_args` + `ignore_unknown_options`), so the registry's own parser
is the *only* parser. It currently under-enforces its own schema:

- `registry.py:resolve_args` does no type coercion and never enforces
  `Param.required`. A `--name` option with no following value silently becomes
  `True` instead of erroring.
- Unknown tokens are preserved under `_extra`, but nothing forces commands to
  check `_extra`, so "nothing silently lost" holds only by convention.
- `app.py:_extract_post_globals` consumes global-looking tokens (`--home`,
  `--json`, …) anywhere in the argument tail, so a positional argument that
  happens to match a global name gets eaten.

**Fix:** make `resolve_args` enforce the `Param` schema (required params,
value-options must have values, optional typed coercion), add a shared helper
that rejects or warns on unconsumed `_extra`, and only strip post-subcommand
globals at positions a positional argument cannot occupy (or require globals
before the subcommand).

### 2. Add quote-aware parsing to the REPL

`registry.py:split_slash` still splits on whitespace; the docstring defers
quoted-argument handling, but multi-word prompts (`/pm ask "fix the login
bug"`) are a core interaction. **Fix:** use `shlex.split` with a graceful
fallback on unbalanced quotes.

### 3. Put real authentication on the mutation surface

`client.py` documents it honestly: the static `x-errorta-origin: cli` header is
the *only* guard on coding/gateway mutations — no token, no crypto. The
sidecar's port is advertised in `${ERRORTA_HOME}/sidecar.json`, and any local
process can send the header to a sidecar that can start runs that execute real
commands. Acceptable for a single-user alpha; not before anything multi-user,
and worth re-checking against the mobile/tunnel surface. **Fix:** mint a
per-sidecar bearer token at spawn, store it 0600 next to `sidecar.json`, and
require it on mutating routes.

### 4. Deduplicate the `--watch` / dashboard arming logic

The arming + `SELF_STREAMING` special-casing is duplicated nearly verbatim in
`app.py:_run_registry_command` and `repl.py:run_repl` — a crack in the "one
code path" claim that will drift. **Fix:** extract a shared
`watch.maybe_run_watch(name, client, ctx, raw_args) -> handled: bool` used by
both front-ends.

### 5. Trim spec-coordinate commentary to what the code can carry

Docstrings lean heavily on external coordinates ("F147 §4.2", "S9b", "review
LOW-3") that mean nothing without the spec documents, and policy is often
restated in three places (module docstring, function docstring, inline
comment) — a drift liability. **Fix:** keep the *why*, drop or centralize the
spec cross-references (e.g. one mapping table in the package README), and let
tests — not prose — be the enforcement mechanism. Also: "un-triaged" is
rendered as "未-triaged" twice in `runstream.py`; clean up the stray character.

### 6. Make the Windows lock degradation loud

`sidecar.py:_home_lock` silently no-ops without `fcntl`, so on Windows the
double-spawn guard vanishes without a trace. Acknowledged as a v1 limitation,
but silence is the problem. **Fix:** emit a one-line stderr warning when the
lock degrades (and gate any future Windows build on a real `msvcrt`/`portalocker`
implementation).

### 7. Reduce import-time side effects and module-global state

`app.py` registers all argv commands at import time and stores globals in a
mutable module-level `_Globals` singleton; `registry.py` imports command
modules for their registration side effects. It works, but it makes the package
awkward to embed or test in isolation. **Fix (opportunistic):** move
registration behind an idempotent `ensure_registered()` called from `main()`
and REPL entry, and pass a context object instead of mutating `_G`.
