# F153 — Launch classifier hardening: exit-during-window is a crash; language-agnostic crash detection

## Problem

Sibling to F152. The F146 launch probe
(`runtime_process.py::launch_probe`) classifies a delivered head's launch as
`clean | crashed | cannot_verify | skipped`. Two blind spots in the *process-exit*
classification (distinct from F152's *never-makes-an-HTTP-request* gap) let a
broken app be reported **clean**:

### G1 — a long-running server that exits 0 during the startup window is "clean"

The classification branches (`runtime_process.py:994-1041`) are evaluated in this
order:

```
survived && !traceback            -> clean
survived && traceback             -> crashed
rc == 0                           -> clean          # <-- line 1009
rc < 0 (signal)                   -> crashed
traceback (nonzero exit)          -> crashed
long_running                      -> crashed         # <-- line 1026, unreachable for rc==0
else (one-shot nonzero, no trace) -> clean
```

The generic `rc == 0` → clean branch (:1009) is checked **before** the
`long_running` guard (:1026). So a `web`/`api`/`desktop` profile
(`_LONG_RUNNING_KINDS`, :80) that **exits 0 during the window** — a server that
fails to bind ("address already in use"), hits a config error and `sys.exit(0)`s,
or has the wrong entrypoint — is classified `clean, passed=True`. The
`long_running` branch that exists precisely to catch "it must keep running but
didn't" only fires for **non-zero** exits; exit 0 slips past it. A web server that
should stay up but exited cleanly is the definition of a failed launch, yet it
passes the delivery gate → `project_done`.

### G2 — crash detection is CPython-string-only; non-Python crashes are "clean"

`has_traceback = "Traceback (most recent call last)" in tail` (:985) matches only
CPython. The final `else` (:1034-1041) classifies any **non-long-running** kind
that exited non-zero *without that exact string* as `clean, passed=True` ("ran and
exited with no crash"). So a Node/Rust/Go/compiled CLI that panics or throws →
exits 1 with a non-Python stack → **clean**. The crash catcher is fundamentally
Python-specific; every other runtime's crash is invisible.

Note the `else` branch is **intentional** for a genuine one-shot CLI that exits
non-zero as normal usage/validation (matches `run_cli`'s non-finding treatment) —
so the fix must distinguish a *crash* from a *legitimate non-zero exit*, not blanket
non-zero → crash.

## Goal

- **G1:** for a long-running kind (`web`/`api`/`desktop`), *any* exit during the
  startup window — including exit 0 — is a `crashed` verdict. A server that must
  keep running but didn't is never clean.
- **G2:** replace the CPython-only `has_traceback` with a small, high-precision,
  **framework-agnostic** `_has_crash_signature(tail)` helper, so a non-Python crash
  is caught — while a legitimate non-zero CLI usage exit (no crash signature) stays
  clean. This helper is the shared primitive **F152 also consumes**.

## Non-goals

- Not changing the `cannot_verify` (spawn/sandbox/setup failure) or `skipped`
  (non-managed_local) paths.
- Not changing the survival window length (F152 handles the HTTP window). The CLI
  window stays 12s.
- Not a general log linter — the marker set is deliberately tiny and anchored to
  crash phrases, not the word "error".

## Design

All in `python/errorta_council/coding/runtime_process.py::launch_probe`
classification block (:994-1041) plus one module helper. `_delivery_launch_evidence`
and `delivery_review` are unchanged — they already turn `crashed` into a filed
finding that blocks `done`.

### 1. Reorder: long-running + exited = crash (G1)

Hoist the `long_running` check so it precedes the generic `rc == 0` clean branch.
New order once the process is known to have **exited** (`rc is not None`, i.e. not
`survived`):

```
survived && crash_signature   -> crashed            (unchanged intent, broadened marker)
survived (no signature)       -> clean              (server kept running — good)
# --- exited during the window: ---
long_running                  -> crashed   # ANY exit code: it had to stay up
rc < 0 (signal)               -> crashed
crash_signature (nonzero)     -> crashed
rc == 0                       -> clean     # a one-shot that finished successfully
else (nonzero, no signature)  -> clean     # legitimate CLI usage/validation exit
```

The single move is: `long_running` is now the **first** post-exit branch, so a
web/api/desktop exit of *any* code is a crash. `rc == 0` clean now applies only to
**one-shot** (non-long-running) kinds — a CLI/script that ran and exited 0, which
is the correct clean case.

### 2. Language-agnostic crash signature (G2)

Add a module helper:

```python
_CRASH_SIGNATURES = (
    "Traceback (most recent call last)",  # CPython
    "Failed to compile",                   # Next.js / webpack
    "Module not found",                    # webpack / node
    "Cannot find module",                  # node
    "SyntaxError",                         # node / babel
    "ReferenceError",                      # node
    "error TS",                            # tsc
    "ERROR in ",                           # webpack
    "panicked at",                         # Rust
    "goroutine ",                          # Go panic dump
    "Exception in thread",                 # JVM
)

def _has_crash_signature(tail: str) -> tuple[bool, str]:
    for sig in _CRASH_SIGNATURES:
        if sig in tail:
            return True, sig
    return False, ""
```

Replace `has_traceback` usage in the classifier with `has_sig, sig =
_has_crash_signature(tail)`; the `crashed` detail names the matched `sig` plus the
log tail. Keep the list short and phrase-anchored (not "error"/"fail" substrings)
to avoid false positives on apps that legitimately log those words.

This is the primitive **F152** consumes to enrich its HTTP-500 finding (once the
request triggers compilation, `Failed to compile` appears in the tail).

### Interaction with F152

F152 adds the HTTP-serve assertion (the "binds a port but 500s" case, where the
process never exits). F153 fixes the "process exited / crashed" classification.
They are complementary branches of the same function and are implemented together
(one cohesive rewrite of the classification block), but are specced separately
because they close different failure modes:

- F152: server up, process alive, serves 5xx  → crash (HTTP path).
- F153-G1: server exited during window (any code) → crash (exit path).
- F153-G2: process crashed with a non-Python stack → crash (signature path).

## Edge cases

- **A long-running server that legitimately exits 0 fast** (e.g. `--check` mode) —
  by definition a `web`/`api`/`desktop` profile's `start` is meant to serve, so an
  exit during the window is correctly a failure. A one-shot health/check command is
  a `cli` kind, not long-running, and keeps the `rc==0 → clean` path.
- **False-positive crash signatures**: a健全 app that prints "SyntaxError" as
  legitimate output would false-fail. Mitigation: the tail is the last 40 lines at
  the moment of teardown; the markers are startup-crash phrases; and this only
  affects the *delivery* gate (a false crash re-opens the run and the next clean
  head clears it — fail toward catching real crashes, matching the existing
  `survived && has_traceback` comment's stated bias). Acceptable and reversible.
- **G2 vs. the intentional CLI else-branch**: preserved — a non-zero CLI exit with
  **no** crash signature is still `clean` (usage/validation), only a signature makes
  it a crash.

## Testing

New cases in `python/tests/coding/test_f146_slice_c_launch.py`:

- **`test_long_running_exit_zero_is_crash`** (G1) — a `web` profile whose start
  command binds nothing and exits 0 within the window → `status="crashed"` (was
  `clean`); `delivery_review` files a finding, `done` blocked.
- **`test_long_running_exit_zero_detail_mentions_kind`** — detail says the runtime
  "exited during startup ... must keep running".
- **`test_one_shot_exit_zero_still_clean`** (regression) — a `cli` profile that
  exits 0 → `clean` (the reorder must not break one-shots).
- **`test_node_crash_no_python_traceback_is_crash`** (G2) — a non-long-running
  profile that prints a JS stack (`ReferenceError: x is not defined`) and exits 1 →
  `crashed` (was `clean`).
- **`test_cli_usage_nonzero_no_signature_is_clean`** (regression) — non-zero exit,
  no crash signature → `clean` (usage/validation preserved).
- **`test_crash_signature_helper`** — unit-test `_has_crash_signature` over each
  marker + a clean sample.
- **Regression**: the existing Slice-C suite (`test_launch_clean_allows_done`,
  `test_launch_probe_crash`, `test_launch_probe_cli_nonzero_no_traceback_is_clean`,
  `test_launch_probe_long_running_early_exit_is_crash`, …) stays green. Note
  `test_launch_probe_cli_nonzero_no_traceback_is_clean` asserts the *cli* case
  which the reorder preserves.

## Documentation

- `docs/CLI.md` delivery-review note: the launch probe now treats *any* exit of a
  web/API/desktop program during startup as a failed launch, and detects crashes
  across languages (not only Python).

## Out of scope

- The HTTP-serve assertion (F152).
- The default build gate (F154) — an orthogonal all-routes compile check.
