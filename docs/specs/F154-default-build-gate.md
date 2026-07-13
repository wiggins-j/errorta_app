# F154 — Default compile/typecheck gate for test-less runnable projects

## Problem

Third gap in the F152 family (audit G3). A greenfield project starts with an
**empty test-command registry** (`ledger.get_test_commands()` → `{}`,
ledger.py:1200-1203). Two consequences:

- **Per-PR merge** (`_set_mergeable_if_ready`, runner.py:2317-2318):
  `tests_ok = p.get("tests_passed") is True OR not store.get_test_commands()` — with
  no registered commands the tests gate is vacuously satisfied, so **every PR merges
  into master on a single reviewer model-approval, with zero compilation ever run**.
- **`project_done`** (`delivery_review`, runner.py:3635-3673): empty registry →
  `tests_passed=True` unconditionally; and a **non-runnable** project →
  `_delivery_launch_evidence` returns clean vacuously. So a test-less project can
  reach `done` with nothing ever executed or compiled.

F152 + F153 catch a *runnable* app that fails to **serve or start**. They do **not**
catch a compile/type error on a code path that is never requested at launch (a deep
route, an unused export, a type error the dev server tolerates lazily). The
deterministic way to catch *all* of those is a real **build/typecheck** — which no
project gets by default.

## Goal

When a project has **no registered test commands**, the delivery gate runs an
**auto-derived build/verify command** appropriate to the detected stack, and treats
its failure like a failed test: block `done`, file a dev finding. This gives every
project a zero-config compile floor (`next build` / `tsc --noEmit` / `py_compile`)
without the user configuring anything, and without weakening the existing behavior
when tests *are* registered.

## Non-goals

- Not changing behavior when test commands ARE registered (the strict
  reviewer-AND-tests gate is unchanged; no build is injected on top).
- Not gating **per-PR** merges on the build. Mid-run PRs are partial and may not
  build until integrated; the build runs at the **integrated delivered head**
  (delivery review), mirroring how `delivery_review` already runs the full registry
  deterministically as the anti-gaming backstop. (A per-PR build gate is a possible
  later refinement, explicitly out of scope here.)
- Not a package/dependency resolver — it reuses the runtime profile's existing
  `setup` step to install deps.

## Design

The default-build runs **inside the delivery review**, reusing the launch probe's
setup (deps install) so ordering is correct.

### 1. Derive a verify command from the detected project (`_default_verify_command`)

A new resolver (near `runtime_resolve`) inspects the delivered master root and
returns `(argv, cwd)` or `None`:

| Detected | Verify command | Notes |
|---|---|---|
| `package.json` with a `build` script | `npm run build` | Next.js/Vite/CRA full build — fails on any compile/type error |
| `package.json`, no build script, `tsconfig.json` present | `npx --no-install tsc --noEmit` | typecheck only |
| `package.json`, neither | `None` | nothing safe to run |
| `pyproject.toml`/`setup.py`/any `*.py`, no faster option | `python -m compileall -q <root>` | syntax-level compile of all modules; no deps needed |
| `Cargo.toml` | `cargo build --quiet` | |
| `go.mod` | `go build ./...` | |
| none of the above | `None` | skip (vacuously clean, as today) |

Keep the table small and conservative — a `None` result preserves exactly today's
behavior (no false gate on stacks we can't safely build).

### 2. Run it in the delivery review when the registry is empty

In `delivery_review` step 2 (runner.py:3631-3659), when `registry` is empty:

- compute `verify = _default_verify_command(store, workspace)`;
- if `verify is None` → unchanged (no tests, nothing to run here — F152/F153 launch
  probe still gates a runnable project);
- else ensure deps are installed (reuse the launch probe's setup gate —
  `_setup_pending_venv_missing` equivalent, or run the profile's `setup` once), then
  run `verify` via the existing sandboxed command runner (`run_test_commands`-style,
  `require_sandbox` honored), bound to `head`;
- record a `delivery build` decision + a synthetic test-run; on non-zero exit set
  `tests_passed=False` and `tests_failed_detail` = the build output tail. The
  existing fail-closed path (runner.py:3706-3712) then files a **"fix delivery
  build"** dev task and blocks `done`.

### 3. Dependency-install ordering (the one real wrinkle)

`npm run build` / `cargo build` need deps present. The delivery launch probe
(step 3) already stands up the per-project env via the profile `setup`
(`_setup_pending_venv_missing` → `setup()` for Python; the Node profile's `setup`
is `npm install`). Two clean options — pick during implementation:

- **(A, preferred)** factor the launch probe's setup gate into a shared
  `_ensure_setup(profile) -> ok|cannot_verify` and call it before both the build
  (step 2') and the launch (step 3). A setup failure is `cannot_verify` (blocks
  `done`, files no code finding — same as today's launch setup failure), never a
  false build failure.
- **(B)** run the default build as an extra check *inside* `launch_probe` (which
  already does setup) rather than in delivery_review step 2. Simpler ordering,
  but conflates "build" with "launch"; (A) keeps the delivery review's
  review→tests→launch structure.

If setup cannot run (no deps installable, sandbox refused), the build is
`cannot_verify`, not a failed build — fail-closed but no phantom code finding.

## Edge cases

- **Monorepo / app in a subdir** (the CLI delivers into a subdir): derive `cwd`
  from where `package.json`/manifest actually lives (the runtime profile already
  resolves a working dir — reuse `_resolve_working_dir`).
- **Build is slow** (`next build` ~30s–2min): bounded by the test-run timeout and
  **cached once per unchanged head** (delivery review is head-keyed), so it runs at
  most once per delivered head, not per iteration.
- **`npm run build` needs network for a first `npm install`**: the runtime sandbox
  already allows network for setup (dev servers install + bind); the build reuses
  that posture.
- **A project that legitimately has no build** (static HTML): `None` → skipped;
  F152/F153 still gate it if it's served, and a pure static site has no compile step
  to fail.
- **False floor**: `compileall` only catches *syntax* errors, not type/name errors,
  for Python — an honest, cheap minimum. Projects wanting more register real test
  commands (documented).

## Testing

`python/tests/coding/` (new `test_f154_default_build_gate.py`):

- `test_node_build_failure_blocks_done` — empty registry, `package.json` with a
  failing `build` script → delivery review runs `npm run build`, it fails →
  `tests_passed=False`, "fix delivery build" task filed, `done` blocked.
- `test_node_build_success_allows_done` — passing build → gate clean.
- `test_tsc_noemit_when_no_build_script` — `tsconfig.json`, no build script →
  derives `tsc --noEmit`.
- `test_python_compileall_catches_syntaxerror` — a `.py` with a syntax error →
  `compileall` non-zero → blocked.
- `test_no_derivable_build_is_noop` — a stack with no rule → `None` → unchanged
  (today's vacuous-clean for a test-less project preserved, gated only by
  F152/F153 if runnable).
- `test_registered_commands_skip_default_build` — when the registry is non-empty the
  default build is NOT added (no behavior change).
- `test_setup_failure_is_cannot_verify_not_build_fail` — deps can't install →
  `cannot_verify`, no phantom finding.

## Documentation

- `docs/CLI.md`: a test-less project now gets a default compile/build check at
  delivery; register real commands with `errorta test-commands set` to go beyond it.

## Status / sequencing

**Fast-follow** relative to F152/F153/F155. Reason: the dependency-install ordering
(§3) is the one genuinely non-trivial piece, and rushing it risks *false* build
findings (a build that fails only because deps weren't installed). It is specced in
full here and implemented immediately after the low-risk launch-probe + round-cap
changes land and are verified, in the **same PR** if time allows or the very next.

## Out of scope

- Per-PR (mid-run) build gating.
- Language-server / lint-level checks beyond compile/typecheck/build.
