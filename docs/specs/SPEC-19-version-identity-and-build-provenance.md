# Spec 19 — Version identity and build provenance

**Source:** the `0.1.0-alpha.11` CLI recut (2026-07-25) — the released binary
self-reported `0.1.0-alpha.10`, and its `/healthz.build.commit` was `null`.
**Target version:** v0.1 (CLI + sidecar + release tooling)
**Status:** proposed
**Owner:** wiggins-j

---

## Problem

`brew upgrade errorta` installed `0.1.0-alpha.11`. The freshly-spawned sidecar
reported:

```
sidecar: errorta-sidecar v0.1.0-alpha.10 (python 3.14.5)
```

The **code was correct** — the live policy from that same binary carried
`web_probe`, `strict_file_partition`, `revise_chain_limit`, `gate_bootstrap`,
i.e. every Spec 12–18 + GL01–05 knob. Only the *string* was stale.

That is not cosmetic. During the post-release smoke run it produced a false
diagnosis: an unrelated `500` was attributed to "the CLI is still running old
code", which sent the investigation down a wrong path (restarting sidecars,
killing processes, hand-running `errorta __serve__` — which then squatted the
desktop app's port and wedged the store behind the co-drive guard). The version
string is the **one** signal an operator uses to answer *"did my upgrade take
effect?"*, and it lied.

Second, related defect: `/healthz.build` on the released binary is

```json
"build": {"commit": null, "built_at": null, "dirty": false, "source": "unknown"}
```

`build_info.py`'s own docstring calls this "the backbone of the *is my app
stale?* check" and warns that without a stamped commit "the only symptom is
confusing downstream failures". Exactly that happened: the CLI's co-drive guard
compares builds and **refused to run** —

> refusing to co-drive a sidecar whose build could not be confirmed to match
> (it advertises commit 'None', this CLI is 'None')

— so an unstamped release binary cannot be co-driven at all once a sidecar it
did not spawn is present.

## Why the existing machinery didn't catch it

There is no single source of truth. **Four** in-repo declarations, plus a fifth
value in installed metadata, all drift independently:

| Declaration | Value at the alpha.11 recut | Who reads it |
|---|---|---|
| `python/pyproject.toml:3` | **`0.1.0-alpha.11`** | `scripts/release-cli.sh:202` (sed) → git tag, tarball name, Homebrew formula url + `version` |
| `python/errorta_app/__init__.py:9` | **`0.1.0-alpha.10`** | `/healthz` (`server.py:635`), `/version` (`:664`), the FastAPI app `version=` (`:450`) |
| `python/errorta_cli/__init__.py:14` | **`0.1.0-alpha.10`** | the CLI's own self-report |
| `python/.venv/…/errorta_app-0.1.0a0.dist-info` | **`0.1.0a0`** | anything using `importlib.metadata` |
| `package.json:4` / `src-tauri/Cargo.toml:3` / `src-tauri/tauri.conf.json:4` | `0.1.0-alpha.0` | the desktop app lineage (separate release train) |

`release-cli.sh` reads **only** `pyproject.toml` and writes **nothing** back.
So bumping the release version is a one-line edit that silently leaves the
runtime self-report a release behind. Nothing — no test, no preflight — asserts
the three Python declarations agree.

Build provenance has the same shape: `build_info.py` resolves
`_build_info.json` (bundled) → `ERRORTA_BUILD_COMMIT` (env) → live `git` →
`unknown`. The **app** pipeline (`scripts/build-sidecar.sh`) writes
`_build_info.json`; the **CLI** pipeline (`scripts/release-cli.sh`) does not,
and `_from_git` deliberately returns `None` when `sys.frozen` — so every
released CLI binary lands on `unknown`.

This is the identical failure shape Spec 17 Phase 4 fixed for `dev_repo_read`
("four statements, two values"), and the repo already has the idiom to prevent
it: an anti-drift canary test (`test_f145_pm_reference.py` asserts
`PM_REFERENCE.md`'s embedded JSON equals `policy_to_dict(CodingAutonomyPolicy())`;
`test_spec12_18_prep` greps `autonomy.py` for exact strings). Version identity
simply never got one.

## Goals

- **One** canonical version for the Python lineage; every other declaration is
  derived from it or asserted equal to it by a test that fails the build.
- A released binary **self-reports the version it was released as** — `/healthz`,
  `/version`, and `errorta status` all agree with the git tag, the tarball name,
  and the Homebrew formula.
- A released binary **carries its commit**, so the staleness check,
  `scripts/app-doctor.sh`, and the co-drive guard can all function.
- Drift is caught **at release time** (a preflight refusal), not by a user
  reading a wrong number weeks later.

## Non-goals

- Not unifying the **desktop app** lineage (`package.json` / `Cargo.toml` /
  `tauri.conf.json`) with the Python lineage. They ship on separate trains and
  `release-macos.sh` owns that one; this spec covers the sidecar + CLI and
  explicitly *names* the app lineage as out of scope so the next reader doesn't
  assume it was missed.
- Not adopting `importlib.metadata` as the runtime source. The editable install
  in the build venv reports `0.1.0a0` — a fifth value — and metadata is exactly
  what goes stale in a frozen bundle. A literal that a test locks is more honest.
- Not a version-bump *policy* (when to go alpha.N+1 vs beta). Purely mechanical
  identity.
- Not changing `build_info.py`'s resolution order — it is correct; the CLI
  pipeline just never populated tier 1 or 2.

---

## Item 1 — One canonical version, mirrors asserted by test

**Design.** `python/pyproject.toml`'s `version` stays the canonical value: it is
what `release-cli.sh:202` already reads, and what names the tag, the tarball,
and the formula. The two Python literals become **mirrors**:

- `python/errorta_app/__init__.py:9` — `__version__`
- `python/errorta_cli/__init__.py:14` — `__version__`

Add a drift-lock test (`python/tests/test_version_identity.py`) asserting all
three are byte-equal, in the established canary style. The test names the fix in
its failure message (*"run `scripts/bump-version.sh X.Y.Z`"*), because a bare
assertion failure on a version literal is otherwise a puzzle.

**Δ note — why not `dynamic = {version = {attr = ...}}`.** setuptools can derive
`pyproject`'s version from `errorta_app.__version__`, which would collapse this
to one literal with no test. Rejected: `release-cli.sh` reads the version by
**sed on `pyproject.toml`** (`:202`), and a `dynamic` version leaves no literal
there to read — the release pipeline would silently lose its version source. A
mirror + lock keeps both readers working and is a smaller blast radius.

**Acceptance.** With the three values equal the test passes; changing any one
alone fails it with a message naming the bump script. `pyproject.toml` remains
sed-readable by `release-cli.sh` (assert the exact regex at `:202` still matches).

## Item 2 — `scripts/bump-version.sh`

**Design.** One script, the only supported way to change the version:

```
bash scripts/bump-version.sh 0.1.0-alpha.12
```

It rewrites the three declarations from Item 1, prints a diff, and exits
non-zero if the value is malformed or if the tree is dirty in those files for an
unrelated reason. It does **not** commit or tag — the operator does, so a bump
is reviewable like any other change.

**Acceptance.** After running it, the Item 1 drift-lock passes and
`release-cli.sh --check` reports the new version. A malformed version is
refused. Running it twice is a no-op.

## Item 3 — Release preflight refuses to build on drift

**Design.** `release-cli.sh`'s preflight (the `--check` block that already
validates pyinstaller / `gh` auth / signing identity) gains one check:
`pyproject.toml`, `errorta_app.__version__`, and `errorta_cli.__version__` must
agree. On mismatch it **dies** with the offending values and the bump-script
command.

This is the load-bearing item: Items 1–2 make the values *correct*, this one
makes them *impossible to ship wrong*. Preflight already runs on every release
(and standalone via `--check`), so the gate costs nothing.

**Acceptance.** `release-cli.sh --check` on a drifted tree exits non-zero naming
all three values; on an agreeing tree it reports `OK version identity` alongside
the existing checks. `--dry-run` refuses on drift too (a dry-run that would
produce a mislabeled artifact is not a useful dry-run).

## Item 4 — Stamp build provenance into the CLI binary

**Design.** `release-cli.sh` writes `python/errorta_app/_build_info.json` before
invoking pyinstaller and removes it afterward (it is a build artifact, never
committed — add to `.gitignore`), with the shape `build_info._from_bundle()`
already expects:

```json
{"commit": "<git rev-parse HEAD>", "built_at": "<ISO-8601>",
 "dirty": <git status --porcelain non-empty>, "source": "release-cli"}
```

`python/cli.spec` must include it as bundled data so `_from_bundle()` finds it
under `sys._MEIPASS/errorta_app/` (`build_info.py:33-35`). The env-var path
(`ERRORTA_BUILD_COMMIT`, tier 2) is the fallback if the spec change proves
awkward — but tier 1 is preferred because it survives however the binary is
launched.

**Releasing from a dirty tree** stamps `dirty: true` rather than refusing —
provenance should describe reality, and the operator sometimes has a legitimate
local patch. A **warning** is printed.

**Acceptance.** A released binary's `/healthz.build` carries the real commit and
`source: "release-cli"`, not `null`/`unknown`. `scripts/app-doctor.sh` can
compare it to repo HEAD. The CLI's co-drive guard has a commit to match against
instead of refusing on `'None'` vs `'None'`. A source-checkout run is unchanged
(tier 3 `git` still fires; the frozen guard at `build_info.py:78` is untouched).

---

## Implementation notes

- **`python/errorta_app/__init__.py`** (`:9`) and **`python/errorta_cli/__init__.py`**
  (`:14`) — mirrors; no behaviour change, they are already read everywhere.
- **`scripts/release-cli.sh`** — preflight check (Item 3) next to the existing
  `OK`/`n/a` lines; `_build_info.json` write + cleanup around the pyinstaller
  invocation (Item 4). The version read at `:202` is untouched.
- **`python/cli.spec`** — bundle `errorta_app/_build_info.json` as data.
- **`.gitignore`** — `python/errorta_app/_build_info.json`.
- **New:** `scripts/bump-version.sh`, `python/tests/test_version_identity.py`.
- **No engine change.** Nothing in `errorta_council/` is touched.

## Edge cases

- **A source-checkout run** (`dev-errorta`, `pip install -e`): no bundled
  `_build_info.json`, so tier 3 `git` answers — unchanged, still accurate.
- **A dirty tree at release**: stamps `dirty: true` + warns; does not refuse.
- **The desktop app's sidecar** (`build-sidecar.sh`) already writes tier 1 — this
  spec must not double-write or change that path; the two pipelines write the
  same filename from different scripts and never run in the same build.
- **A stale editable install** in the build venv (`0.1.0a0` today): irrelevant
  once nothing reads `importlib.metadata` for identity — but the Item 1 test
  should **not** assert against dist-info, or a stale venv fails an unrelated CI.
- **Homebrew formula already published** at a version whose binary self-reports
  older: this spec prevents recurrence; the already-shipped `alpha.11` is fixed
  by the next recut (no retroactive repair — the artifact is immutable).

## Testing

- **Item 1**: the three declarations are byte-equal; mutating one fails with the
  bump-script message; `release-cli.sh:202`'s sed regex still extracts the value.
- **Item 2**: bump writes all three; malformed input refused; idempotent.
- **Item 3**: a drifted tree fails `--check` **and** `--dry-run` naming all three
  values; an agreeing tree passes. (Shell-level test or a documented manual
  check, matching how the rest of `release-cli.sh` is verified.)
- **Item 4**: with a stamped `_build_info.json`, `build_info()` returns
  `source: "release-cli"` + the commit and `commit_short`; with the file absent
  and `sys.frozen` faked, it falls back to `unknown` without raising (the
  fail-open lock).
- **The regression lock**: a test asserting `/healthz.version` equals the
  `pyproject` version — the exact symptom that opened this spec.

## Documentation

- `docs/BUILD_AND_RELEASE.md`: bumping is `scripts/bump-version.sh X.Y.Z`, never
  a hand-edit; preflight refuses on drift; released binaries carry their commit.
- `docs/CLI.md`: `errorta status`' version line is authoritative — it is the
  released version, and a mismatch against `brew list --versions errorta` is a
  bug worth reporting.

## Out of scope / follow-ups

- Unifying the desktop-app version lineage with the Python one (a real question:
  should the app and the CLI share a version at all?).
- Publishing `_build_info.json`'s commit in `errorta status` output (today it is
  `/healthz`-only, surfaced by `app-doctor.sh`).
- A CI check for version identity — this repo releases from a maintainer's
  machine by design (GitHub Actions is off), so the preflight gate is the
  enforcement point; a CI job would be belt-and-braces if Actions ever come back.
