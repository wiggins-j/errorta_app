# Spec 13 — Foundation gate: recognize buildless web projects

**Source:** `docs/coding/RUN_ANALYSIS_GRAVITY_GOLF_2026-07-24.md` §6 **S2 (P0)**
**Target version:** v0.1 (engine)
**Status:** proposed (revised after a code-grounded review — see the **Δ review** notes)
**Owner:** wiggins-j

---

## Problem

The gravity-golf project is **deliberately buildless** — its North Star says it
opens directly in a browser with no build step. Its foundation *is* `index.html`
plus seven `<script src>` modules. It will never legitimately produce a
`package.json`; the only reason one was attempted at all is that the
dev-authored acceptance test wants jsdom.

The foundation gate does not know that shape exists. `foundation_ready`
(`runner.py:2212-2256`) classifies any `.js` on master as **manifest-bound**
(`_MANIFEST_BOUND_EXT`, `runner.py:2189-2193`) and then requires a
`_BUILD_MANIFESTS` entry (`runner.py:2175-2179`) before the project counts as
foundation-ready. The script-style relaxation added by F142 WS-B
(`_SCRIPT_EXT`, `runner.py:2198`, applied at `:2246-2252`) is explicitly gated on
there being **no** manifest-bound source, so it never applies to a `.js`
project.

Consequences, all observed in the run:

- `refresh_foundation_status` (`runner.py:2258`) persisted `foundation_status =
  "pending"` and never lifted it.
- `runtime_cap` (`autonomy.py:334-361`) therefore returned **1** for the entire
  run — *"`pending` -> 1 … ALWAYS, even when an explicit `max_parallel_workers`
  is set"*. The 3-dev fan-out the operator believed was happening never happened;
  turn timestamps show strictly serial execution end to end. The pipeline paid
  full multi-agent token overhead at zero concurrency benefit.
- `_account_foundation_stall` (`autonomy.py:719-756`) kept counting clamped
  iterations toward a `foundation_not_converging` alert that names a cause
  ("no build manifest + source entrypoint has merged to master") which is, for
  this project, unachievable by construction.
- The single PR that *would* have lifted the clamp (`t-99a011c7bc6e`, adding
  `package.json`) was rejected for a reason **unrelated to the manifest** — it
  was caught in the impossible "run the test and paste the output" scope
  (see [Spec 12](SPEC-12-in-loop-acceptance-gate.md)). One unrelated rejection
  held the concurrency clamp, and whatever runtime setup gates tester dispatch,
  hostage for the rest of the run.

## Why the existing relaxation didn't cover it

F142 WS-B correctly identified this failure mode once already — the pokemon
`game.py` North Star that produces no manifest and never should. Its fix was
scoped to *interpreted script languages*, because at the time the motivating
counter-example was the reddit-look-a-like run: three devs fanned out onto a
near-empty master for a Next.js app, which genuinely needs `package.json` to
resolve imports and run.

Both are right. The missing distinction is that **"web/JS" is not one ecosystem**:

| shape | manifest load-bearing? | example |
|---|---|---|
| bundled / framework web | **yes** — imports resolve through node_modules | Next.js, Vite, anything with bare-specifier imports |
| buildless web | **no** — the browser resolves `<script src>` itself | gravity-golf, a static SPA with relative ES modules |

The gate keys on file extension, which cannot tell these apart. It needs to key
on **how the entrypoint resolves its dependencies**.

## Goals

- A buildless web project on master — `index.html` whose script/style graph
  resolves entirely against files on master — is **foundation-ready without a
  manifest**, so the clamp lifts and devs fan out.
- The reddit-look-a-like protection is **unchanged**: any bare-specifier import,
  JSX/TSX, or framework signal still requires a manifest.
- A foundation-unlocking PR cannot be held hostage indefinitely by a rejection
  that has nothing to do with the foundation.
- The `foundation_not_converging` alert, when it fires, names a cause the team
  can actually act on.

## Non-goals

- Not removing the foundation clamp, or weakening it for bundled projects.
- Not a module-graph resolver. Detection is a bounded textual scan of the
  entrypoint plus one level of referenced sources — cheap, conservative, and
  fail-closed (unsure → keep requiring a manifest).
- Not auto-merging a foundation PR, or bypassing review. Item 2 changes
  *escalation and visibility*, never the merge gate.
- No change to `runtime_cap`'s ramp behavior (`autonomy.py:359-361`) once
  `merged` is reached.

---

## Item 1 — Buildless-web recognition in `foundation_ready`

**Design.** A new predicate `_buildless_web_ready(files, read)` in `runner.py`,
consulted inside `foundation_ready` (`runner.py:2212-2256`) **before** the
`has_manifest` requirement, and only when the manifest-bound source is
**web-only** (`.js`/`.mjs`/`.cjs`/`.css`/`.html` — never `.go`/`.rs`/`.java`/…,
which stay manifest-bound unconditionally).

True iff **all** of:

1. `index.html` exists on master;
2. every `<script src="…">` and `<link rel="stylesheet" href="…">` with a
   **relative** URL resolves to a file present on master (an absolute/CDN URL is
   an external dependency and disqualifies — it is not self-contained, and it is
   also the shape most likely to be a half-scaffolded framework app);
3. no **bundler-required** signal appears in any referenced source: a bare
   specifier import (`import x from "react"` — as opposed to `"./mod.js"` /
   `"/mod.js"`), a `require(` call, JSX/TSX syntax, or a `.ts`/`.tsx`/`.vue`/
   `.svelte` file anywhere on master;
4. at least one referenced script exists (an `index.html` alone is a stub, not a
   foundation).

Anything unreadable or ambiguous → **False** (keep the clamp). The predicate
reuses `workspace.list_files(scope="master")`, already fetched at
`runner.py:2242`, plus bounded reads of the referenced files.

Because `refresh_foundation_status` re-derives from git on every call
(`runner.py:2258-2274`), the classification **self-heals**: the moment someone
adds `import React from "react"`, the project flips back to requiring a manifest
and the clamp re-engages. That property is what makes this safe to relax.

**Acceptance.** A master tree of `index.html` + `src/*.js` referenced by relative
`<script src>` is foundation-ready with no manifest. Adding a bare-specifier
import to any of those files flips it back to not-ready. A Next.js-shaped tree
(`.tsx`, bare imports, no `package.json`) stays not-ready — the reddit-clone
regression lock. An `index.html` referencing a CDN URL stays not-ready. An
`index.html` with no resolvable scripts stays not-ready.

## Item 2 — Foundation-unlocking PRs can't be held hostage

**Design.** While `run_state.foundation_status == "pending"`, a PR whose branch
adds a missing foundation element (a `_BUILD_MANIFESTS` filename, or the first
source entrypoint) is a **foundation-unlocking PR**. Mark it on the PR record at
open time (`record_pr`, `ledger.py:997-1012`, gains `unlocks_foundation: bool`),
derived from the same `changed_paths` capture F159 already computes.

Then, at the reviewer-rejection branch (`runner.py:3455-3485`, reached through
the shared `_handle_review_rejection` seam the batch plan's prep PR extracts):

- classify the rejection's **scope**. **Δ review — an empty path set must mean
  *unknown*, not *off-scope*.** Findings carry an optional `path` and reviewers
  routinely emit none (that is the run analysis's own evidence), so an
  intersection test alone would classify *every* pathless rejection as off-scope
  and spawn a PM escalation on top of the revise task every round — a second task
  per cycle feeding the very churn detectors this batch depends on. So: **at
  least one finding must carry a path** before scope is decided. All-pathless →
  `unknown` → no escalation.
- if the rejected PR is foundation-unlocking, at least one finding carries a
  path, and no such path intersects the foundation files it adds, the rejection
  is **out-of-scope for the blocker**. Record a decision
  (`choice="foundation_pr_rejected_offscope"`) and, alongside the normal
  `revise:` task, raise a PM escalation task naming the situation: *the clamp is
  held at 1 by a PR rejected for reasons unrelated to the foundation*. The
  escalation is **deduped per PR lineage** — mirroring the
  `contract_owner_task_id` pattern (`runner.py:2346`) — so a repeated rejection
  produces one escalation, not one per round.
- count consecutive such rejections; at 2, raise a non-blocking attention Alert
  (mirroring `raise_tests_skipped_alert`, `attention.py:722`) so the operator
  sees the deadlock rather than inferring it from a serial turn log.

Additionally, `_account_foundation_stall`'s rationale (`autonomy.py:741-750`)
becomes shape-aware: for a web-only tree it says which of the four Item-1
conditions is failing (e.g. *"index.html references `src/main.js`, absent on
master"*) instead of the generic manifest sentence. A cause the PM can act on is
the difference between a stall and a stall it can fix.

**Acceptance.** A foundation-unlocking PR rejected on a finding whose path is not
one of the foundation files records the off-scope decision and spawns exactly one
PM escalation; a repeat of the same rejection spawns none (lineage dedup); two
such rejections raise one deduped alert. A rejection whose findings are **all
pathless** produces no escalation and no alert. A rejection that *does* target
the foundation files behaves exactly as today. The stall alert on a
buildless-web tree names the failing condition.

---

## Implementation notes

- **`runner.py`** — `_buildless_web_ready` next to `_SCRIPT_EXT` (`:2198`);
  called from `foundation_ready` (`:2212`) before the `has_manifest` line
  (`:2253`). Keep the existing early returns (`existing` target, unreadable
  workspace → fail closed) untouched.
- **`ledger.py`** — `unlocks_foundation` on the PR record (`record_pr`,
  `:1017`); additive, no migration (absent → falsy). **Δ review:** land this in
  the same commit as [Spec 14](SPEC-14-grounded-reviewer.md)'s review-grounding
  fields — both add keys to one dict literal, so splitting them conflicts inside
  the same branch.
- **`runner.py`** — set it where the PR is opened (`:3360-3390`), from the
  pre-merge `changed_paths` capture pattern (`:3106-3112`); consume it in the
  shared rejection seam (`:3455-3485`).
- **`autonomy.py`** — shape-aware rationale in `_account_foundation_stall`
  (`:741-750`); no new policy knob (the existing `foundation_stall_limit`
  governs cadence).
- **`attention.py`** — one new alert raiser mirroring
  `raise_tests_skipped_alert` (`:722`), deduped per project.

## Edge cases

- **Buildless now, bundled later.** Self-healing by construction — status is
  re-derived from git each call, so adding a bare import re-clamps.
- **A `package.json` that exists only for jsdom/dev-deps** (exactly
  gravity-golf's case): irrelevant after Item 1 — the project is already
  foundation-ready, so the manifest PR is ordinary work, not a blocker.
- **`index.html` referencing a script via a template/loader** (`document.
  createElement('script')`): not matched by the scan → not recognized → clamp
  stays. Conservative and correct; the fallback is today's behavior.
- **Multiple HTML entrypoints**: recognition requires `index.html` specifically;
  others are ignored. A multi-page buildless site still needs `index.html` to be
  self-resolving, which is a reasonable bar for "coherent base".
- **An `existing` (imported) target**: unchanged — `foundation_ready` returns
  True at `runner.py:2236-2238` before any of this runs.
- **Interaction with F159**: while `foundation_status == "pending"` the cap is
  already 1, so hot-file serialization is a no-op; lifting the clamp earlier just
  means F159 starts doing its job earlier, which is the intent.

## Testing

- **Item 1 (unit, on a fixture file list + reader)**: buildless tree →
  ready; bare-specifier import → not ready; `require(` → not ready; JSX → not
  ready; `.tsx` present → not ready; CDN `<script src>` → not ready;
  `index.html` with no resolvable script → not ready; unreadable file → not
  ready. Explicit regression lock: the reddit-look-a-like fixture from
  `test_f142_*` stays not-ready.
- **Item 1 (integration)**: a run whose first merge lands `index.html` +
  `src/*.js` flips `foundation_status` to `merged` and `runtime_cap` returns the
  static base at the next iteration — asserted against today's behavior (stays 1)
  as the regression the spec fixes.
- **Item 2**: an off-scope rejection of a foundation-unlocking PR records the
  decision + spawns one PM escalation; an in-scope rejection does neither; an
  **all-pathless** rejection does neither (the escalation-storm lock); a repeated
  off-scope rejection on one lineage still spawns exactly one escalation; two
  off-scope rejections raise exactly one alert (dedup honored).
- **Item 2 (stall text)**: the buildless-web stall rationale names the failing
  condition, not the manifest sentence.
- Full coding suite — the foundation-gate tests are `test_f139_part_b.py`
  (`foundation_ready` / `refresh_foundation_status` / `_account_foundation_stall`)
  and `test_f142_foundation_script.py` (Δ review: `test_concurrency_foundations.py`
  is unrelated — it covers asyncio/ledger concurrency plumbing) — plus `ruff`.

## Documentation

- `docs/coding/PM_REFERENCE.md`: what counts as a foundation per ecosystem —
  add the buildless-web row next to the existing manifest-bound and script-style
  rows; note that a foundation-unlocking PR rejected off-scope now escalates.
- `docs/CLI.md`: the `foundation_not_converging` alert now names the specific
  missing element for web projects.

## Out of scope / follow-ups

- Recognizing buildless shapes in other ecosystems (a plain `.php` tree behind a
  built-in server, a Deno project with URL imports).
- Import-map / `<script type="importmap">` support — an importmap that maps bare
  specifiers to relative files is genuinely buildless, but parsing it correctly
  is more than v1 needs; today it falls into "unsure → keep the clamp".
- Making the clamp per-path instead of global (fan out on files the foundation
  doesn't touch while it lands).
