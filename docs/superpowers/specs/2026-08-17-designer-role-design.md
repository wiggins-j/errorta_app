# Designer Role for the Coding Team

**Date:** 2026-08-17
**Status:** Design approved, not yet implemented
**Delivery:** Three slices (1: contract, 2: visual review, 3: change flow + anti-sameness), shipped independently in order, with an evaluation checkpoint on a real project after Slice 1.

## Problem

errorta teams produce quality code and awful UI. A single-shot Opus run in claude CLI outperforms an entire errorta team on visuals for the same app prompt. Diagnosis (confirmed against the codebase, not just vibes):

1. **No design direction exists anywhere.** Zero styling/visual/aesthetic guidance in any role prompt (`errorta_council/coding/runner.py` DEV prompt at :3944 is purely mechanical: PORT binding, base64 binaries, full-file rewrites). Generic output is guaranteed, not incidental.
2. **The UI is fragmented across agents with no shared visual contract.** Single-shot Opus holds the whole app in one context with one aesthetic voice; the team splits it into tasks, each agent seeing only its slice.
3. **Nobody ever sees pixels.** The review chain (DEV → REVIEWER code review → TESTER → merge gate) contains no visual step. The GL01 web probe captures a screenshot (`probe_screenshot` on the PR record) but SPEC-14 Item 6 (`review_screenshot`) was withdrawn because the member seam is text-only and the reviewer's tools are jailed to the PR worktree (see the withdrawal note at `coding/autonomy.py:275-291`).
4. **Structural blandness:** generated projects are buildless static web where external scripts/stylesheets are a hard build failure (`runner.py:5069`) and the sandbox is network-off — so every project ships in system fonts with hand-rolled CSS.

Failure mode is all three of: inconsistent screens, generic defaults, and actually-broken layouts. (1)–(2) are fixed by a design contract; (3) requires pixels in the loop; (4) requires vendored assets.

## Decision summary

- **Full Designer role** (not contract-only, not reviewer-extension), staged in three slices. Rationale: broken layouts are unfixable without a visual reviewer, code-review and design-review conflated in one agent is the status quo that failed, and the contract needs a dedicated author and enforcement owner. Slice 1 alone ≈ the cheap contract-only approach with a better author; later slices are paid for only after Slice 1 proves out.
- **The contract is the source of truth.** The app is brought to the contract, never the reverse.
- **Full UI/UX scope** (screen inventory + layout intent), not tokens-only — a review can only fail a broken layout if the contract says what the layout should be.
- **Hard merge gate** with the existing override and breaker escapes.
- **Curated host-side asset library** (OFL fonts + icons) to break the system-font ceiling.
- **Anti-sameness is enforced host-side**, not requested via prompt.

## 1. Role & team wiring

New role constant `DESIGNER = "designer"` in `coding/topology.py`, wired through every existing touchpoint. Three anti-drift mechanisms fail the build until wiring is complete and therefore enumerate the work: the `_MINIMAL_INTENT_EXAMPLES` table (`coding/schemas.py:538`), the golden prompt-segment tests (`tests/coding/test_prompt_segments_golden.py`), and the `PM_REFERENCE.md` machine-parsed contract block (canary test). Known touchpoints:

| Concern | Location |
|---|---|
| Role constants + worker priority | `coding/topology.py:26-33` |
| `coding_role_of` whitelist | `coding/topology.py:828` |
| Ledger `_VALID_ROLES` | `coding/ledger.py:47` |
| Role literal + intent schema + minimal example | `coding/schemas.py:25,77,520-582` |
| `_ROLE_TOOLS` | `coding/turn_controller.py:27-36` |
| Capability manifest | `coding/capabilities.py:65` |
| `ROLE_SKILLS` / `SKILL_DIRECTIVES` | `coding/skills.py:17-60` |
| Control-actions role set | `coding/control_actions.py:38` |
| Team log projection | `coding/team_log.py:170` |
| HTTP role order | `errorta_app/routes/coding.py:2270` |
| CLI team builder / renderers | `errorta_cli/teamdraft.py`, `commands/task.py`, `commands/team.py`, `render/__init__.py` |
| UI role picker + labels | `src/features/rooms/CouncilRoomEditor.tsx:194`, `src/features/coding/index.tsx:45-69` |
| PM reference contract block | `docs/coding/PM_REFERENCE.md` |

Specifics:

- **Worker priority:** `(TESTER, REVIEWER, DESIGNER, DEV)` — design verdicts redirect dev work, so they run before new dev tasks.
- **Tools:** the Designer gets read tools and artifact authoring only — **no `code_write`**. Code changes it wants happen via dev tasks (tool discipline preserved; only DEV writes to the worktree).
- **Modality gating:** recipes (`coding/recipes.py`) add 1 Designer only for UI modalities (`static`, `desktop`, `server`). `cli`, `binary`, `container` projects get no Designer and none of the behavior in this spec. A `server` project that turns out to have no web UI still runs the one design-authoring turn (modality is all the charter records); the UI-touch detection then makes design review inert for it — one wasted turn is the accepted cost, not a special case.
- **Route:** `studio_default_team` (`errorta_slack/config.py:48-63`) gains a designer entry defaulting to `claude_cli.opus`, configurable like the other routes. The route must be tool-capable and multimodal (Slice 2 reads PNGs via claude_cli's `Read`).
- **Prompts:** new `_designer_prompt` / `_designer_prompt_segments` in `coding/runner.py` following the existing PromptSegment pattern; new dispatch arm in the role switch (`runner.py:7025` area); parsed by `parse_coding_turn`.

## 2. The design contract (`design_spec` artifact)

New `ArtifactKind: "design_spec"` in the governance store (`coding/governance.py:44-51`), using the existing `draft → under_review → changes_requested → awaiting_approval → approved` state machine, scheduler, and status projection.

Authored by the Designer in a dedicated turn immediately after charter approval. Content:

- **`body_markdown`** (for humans and for prompt rendering): aesthetic direction and rationale, do/don'ts, per-screen layout intent in prose.
- **`body_json`** (machine-readable, the enforcement surface):
  - `direction_matrix`: explicit picks per axis — typography personality, color strategy, density, shape language, motion, era/mood.
  - `tokens`: palette, type scale, spacing scale, radii, shadows.
  - `assets`: chosen font families + icon set (ids from the asset-library manifest).
  - `screens`: `[{screen, purpose, layout, hierarchy, key_states}]`.
  - `components`: inventory with usage rules.

**Governance gate:** a new phase blocks the PM from dispatching UI dev tasks until the `design_spec` is `approved`. Non-UI tasks (backend logic, tests, chores) are not blocked.

## 3. Asset library (host-side)

Vendored directory `python/errorta_council/coding/assets/design_library/`:

- 10–15 **OFL-licensed** font families spanning the personality axes (geometric sans, humanist sans, slab, display serif, mono, etc.), each with its LICENSE file committed alongside. OFL only — errorta_app is public; license files are part of the acceptance criteria.
- One stroke-icon set (permissively licensed), same treatment.
- `manifest.json`: per family — id, weights, personality tags, license, file paths.

The Designer's authoring prompt receives the manifest; the contract's `assets` block picks from it. The materialize task (§4) copies the chosen files into the project repo. Nothing touches the network at any point — this is what makes non-system typography possible in the network-off sandbox.

## 4. Materialization & dev-side enforcement

- On `design_spec` approval, the reconciler (`coding/topology.py:769-798`) spawns one **"materialize design system"** DEV task: generate `tokens.css` + `base.css` from `body_json`, copy chosen font/icon files into the repo, wire `@font-face`. This task precedes all other UI dev tasks.
- Every DEV and REVIEWER turn on a project with an approved `design_spec` gets a new **`design_contract` prompt segment**, rendered from the artifact: token summary, the current task's relevant screen layout intent, do/don'ts. Devs are instructed to consume tokens and never invent raw values; the reviewer checklist gains token-compliance items.
- The golden prompt-segment tests are byte-locked; adding the segment is a **deliberate one-time golden update**, called out here so it isn't mistaken for drift.

## 5. Visual review & the merge gate (Slice 2)

Two review surfaces:

- **Per-task (code-level):** when a completed dev task touches web paths (`task_touched_paths` from `coding/paths.py` intersected with the `_WEB_ONLY_EXT` extension set, `runner.py:5034`), the reconciler spawns a `design review:` task for the Designer alongside the code review. The Designer reviews the diff against the contract: tokens used, layout intent followed.
- **Integrated (pixels):** at existing gate checkpoints (quiescent-point gate runs, `gate_min_merge_interval`), the GL01 probe's `probe_screenshot` PNG is **copied from the ledger dir into the Designer's accessible worktree** before its turn, so the claude_cli multimodal `Read` can open it — the exact gap named in the SPEC-14 withdrawal note (`autonomy.py:275-291`). No member-seam changes; the seam stays `Callable[[dict, str], str]`.

**Gate:** `design_approved` becomes a new independent blocker in `evaluate_merge_gate` (`coding/diff_review.py:174-215`). `allow_override` remains true (human force-merge). A design rejection spawns `revise:` tasks through existing machinery and counts toward the existing `revise_chain_limit` breaker → PM escalation, so the Designer cannot deadlock the team.

**Fail-open rules** (house style, matching the web probe):
- No screenshot available → code-level review only; recorded as a warning on the PR record, never a red gate.
- Designer member unavailable → gate records `design_review_unavailable`, raises an attention signal (`coding/attention.py`), does not block the merge.

## 6. Design change flow (Slice 3)

User-initiated design changes route through the PM, from either front door:

1. **Intake:** desktop `interject`/`pm-ask` (`routes/coding.py:1699,1863`), or a new Slack verb `request_design_change` in the concierge catalog (`errorta_slack/tools.py`) — catalog canary (`tests/slack/test_catalog_canary.py`) updated deliberately. Non-C-class (it stages artifact work, not spend/publish); if it is later promoted to C-class, the autopilot design's global flag covers it automatically.
2. **Routing:** the PM creates a design task; the Designer produces a **new artifact version** through the normal `draft → approved` cycle.
3. **Diff:** on approval, the host computes a contract diff (`body_json` old vs new: changed tokens, changed/added/removed screens).
4. **Rework:** the diff is injected into the PM's next plan turn, which spawns rework tasks for affected areas. The artifact remains the single source of truth.

## 7. Anti-sameness

Both mechanisms live in the authoring turn (Slice 3; the direction matrix *fields* exist from Slice 1):

- **Direction matrix commitment:** the Designer must record explicit picks per axis in `body_json.direction_matrix`. A deterministic hash of the project id selects a *suggested* starting corner of the matrix, so identical app prompts don't converge on the model's aesthetic mean.
- **Cross-project must-differ, host-enforced:** the host collects `direction_matrix` picks from other projects' `design_spec` artifacts under the delivery root and injects them into the authoring prompt as "recent picks — differ on ≥ 2 axes." Validation is host-side code, not prompt hope: a draft violating the constraint is bounced to `changes_requested` with the violating axes named.

## 8. Error handling summary

| Failure | Behavior |
|---|---|
| Probe screenshot missing/unreadable | Code-level design review only; warning on PR record; never a red gate |
| Designer route unavailable | `design_review_unavailable` recorded; attention signal; merge not blocked |
| Design rejection loop | Existing `revise_chain_limit` breaker → PM escalation |
| `body_json` invalid/missing fields | Artifact bounced to `changes_requested` with named fields; schema-validated on append |
| Must-differ violation | Bounced to `changes_requested` with violating axes named |
| Non-UI modality | No Designer in team; every path in this spec inert |

## 9. Test surface (named up front — the untested path is the broken one)

- **Role wiring:** the three canaries (minimal-intent table, golden prompt segments, PM_REFERENCE contract block) fail until wiring is complete — free coverage. Add: `coding_role_of("designer")`, ledger role validation, `_ROLE_TOOLS` has no `code_write` for designer, capability manifest.
- **Slice 1:** governance phase blocks UI dev dispatch until approval (and does *not* block non-UI tasks); materialize task spawns exactly once on approval; `design_contract` segment renders (golden update); modality gating — a `cli` project spawns no Designer and no design phase; asset manifest schema + every referenced file exists with a LICENSE.
- **Slice 2:** `design_approved` blocker blocks / overrides / fail-opens per §8; screenshot copy path (present, absent, unreadable); UI-touch detection spawns design review on web paths and not on backend-only diffs; rejection feeds the revise-chain breaker.
- **Slice 3:** contract diff correctness (tokens changed, screens added/removed); diff → PM plan → rework tasks spawned; Slack verb catalog canary updated; must-differ validation including the bounce path; project-id hash determinism.

## Out of scope

- Per-screen HTML mockups (expensive, go stale; revisit only if Slice 2 review quality disappoints).
- Multimodal member-seam surgery (the worktree screenshot copy makes it unnecessary).
- Relaxing the buildless external-resource checker (the network-off sandbox makes it moot; the asset library is the fix).
- Curated inspiration library beyond the font/icon assets (content-maintenance burden).
