# Spec 13 — Implementation plan (foundation gate: buildless web)

Spec: [SPEC-13-foundation-gate-buildless-web.md](SPEC-13-foundation-gate-buildless-web.md).

**Owner:** Engineer A · **Branch:** `feat/spec-13-buildless-foundation`
**Base:** `chore/spec-12-18-prep` (merged) · **PR into:** `main`
**Lands first in the batch** — smallest P0, biggest immediate effect (the
concurrency clamp lifts), and every later observation is less confounded once
runs stop being forcibly serial.

The spec's **Δ review** note that shapes this plan: an intersection test over
finding paths would classify *every* pathless rejection as off-scope and spawn a
PM escalation each round — an escalation storm feeding the churn detectors. Scope
is `unknown` unless at least one finding carries a path.

## Phase 0 — spec + plan (no code)

Branch off the merged prep PR; commit the spec + this plan.

## Phase 1 — buildless-web recognition (pure, unit-tested first)

The whole value of the spec is here, and it is a pure function — build and test
it before wiring.

1. `_buildless_web_ready(files, read)` next to `_SCRIPT_EXT`
   (`runner.py:2198`). True iff **all** of:
   - `index.html` on master;
   - every **relative** `<script src>` / `<link rel=stylesheet href>` resolves to
     a file on master (an absolute/CDN URL disqualifies — not self-contained, and
     the shape most likely to be a half-scaffolded framework app);
   - **no bundler-required signal** in any referenced source: a bare-specifier
     import (`from "react"` vs `"./mod.js"` / `"/mod.js"`), `require(`, JSX/TSX
     syntax, or any `.ts`/`.tsx`/`.vue`/`.svelte` anywhere on master;
   - at least one referenced script exists.
   Unreadable or ambiguous → **False** (keep the clamp).
2. Wire into `foundation_ready` (`runner.py:2212-2256`): consult it **before**
   the `has_manifest` line (`:2253`), and **only** when the manifest-bound source
   is web-only (`.js`/`.mjs`/`.cjs`/`.css`/`.html`) — compiled ecosystems stay
   manifest-bound unconditionally. Reuse the `files` list already fetched at
   `:2242`; bounded reads for the referenced sources.

No new state: `refresh_foundation_status` (`:2258-2274`) re-derives from git each
call, so classification self-heals the moment someone adds a bare import.

**Tests** (`test_spec13_buildless_foundation.py`, new — table-driven):
buildless tree → ready; bare-specifier import → not ready; `require(` → not
ready; JSX → not ready; a `.tsx` anywhere → not ready; CDN `<script src>` → not
ready; `index.html` with no resolvable script → not ready; unreadable file → not
ready. **Regression lock:** the reddit-look-a-like fixture from `test_f142_*`
stays not-ready.

## Phase 2 — clamp-lift integration

**Tests only** — Phase 1 is the whole mechanism.

`test_f139_part_b.py`: a run whose first merge lands `index.html` + `src/*.js`
flips `foundation_status` to `merged`, and `runtime_cap` (`autonomy.py:334-361`)
returns the static base at the next iteration. Assert the same fixture on `main`
stays clamped at 1 — that contrast is the regression this spec fixes.

## Phase 3 — foundation-unlocking PR flag

1. `ledger.py:1017` — `unlocks_foundation` on `record_pr` (additive, absent →
   falsy). **Land this in the same commit as
   [Spec 14](SPEC-14-plan.md)'s review-grounding fields** — same dict literal,
   otherwise the two conflict inside Engineer A's own branch.
2. `runner.py:3360-3390` (PR open) — set it while
   `run_state.foundation_status == "pending"` and the branch adds a
   `_BUILD_MANIFESTS` filename or the first source entrypoint, derived from the
   `changed_paths` capture pattern at `:3106-3112`.

**Tests.** A PR adding `package.json` on a pending-foundation project is flagged;
one adding a feature file is not; an `existing`-target project never flags.

## Phase 4 — off-scope rejection escalation

In the shared `_handle_review_rejection` seam (from the prep PR; originally
`runner.py:3455-3485`) — **inputs side only; Engineer B owns the outputs side**:

1. Classify scope: **at least one finding must carry a `path`**, else `unknown` →
   no escalation. This is the storm guard.
2. If the PR is foundation-unlocking, at least one path exists, and none
   intersects the foundation files it adds → record
   `foundation_pr_rejected_offscope` and spawn a PM escalation **deduped per PR
   lineage** (mirror `contract_owner_task_id`, `runner.py:2346`), alongside the
   normal revise task.
3. Count consecutive such rejections; at 2 raise one deduped alert (mirror
   `raise_tests_skipped_alert`, `attention.py:722`).

**Tests.** Off-scope → one decision + one escalation; in-scope → neither;
**all-pathless → neither** (the storm lock); a repeat on one lineage → still
exactly one escalation; two off-scope rejections → exactly one alert.

## Phase 5 — shape-aware stall text

`_account_foundation_stall`'s rationale (`autonomy.py:741-750`) names which
Item-1 condition is failing for a web-only tree (e.g. *"index.html references
`src/main.js`, absent on master"*) instead of the generic manifest sentence. No
cadence change — `foundation_stall_limit` still governs.

**Tests.** The buildless-web stall rationale names the failing condition; a
non-web tree keeps today's text.

## Phase 6 — docs

- `docs/coding/PM_REFERENCE.md` — the buildless-web row alongside manifest-bound
  and script-style; off-scope foundation rejections now escalate.
- `docs/CLI.md` — `foundation_not_converging` names the specific missing element
  for web projects.

## Definition of done

Full coding suite + `ruff` green. The two locks explicit: the reddit-look-a-like
fixture stays not-ready, and an all-pathless rejection spawns nothing. Foundation
tests are `test_f139_part_b.py` + `test_f142_foundation_script.py` — note
`test_concurrency_foundations.py` is unrelated (asyncio/ledger plumbing).
