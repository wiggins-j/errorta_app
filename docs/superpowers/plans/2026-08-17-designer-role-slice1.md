# Designer Role — Slice 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development to implement each task red/green/commit. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `designer` coding-team role that authors a governance `design_spec` (blocking UI dev dispatch until approved), backed by a host-side OFL asset library, and injects a `design_contract` prompt segment into DEV/REVIEWER turns — Slice 1 only (spec §1–§4).

**Architecture:** A new role constant `DESIGNER = "designer"` is threaded through every enumeration touchpoint (topology, ledger, schemas, turn_controller, capabilities, skills, control_actions, team_log, PM_REFERENCE canary, UI/CLI). Modality gating lives in `recipes.resolve_team` (a Designer is seated only for UI modalities). A new governance artifact kind `design_spec` reuses the existing draft→…→approved state machine; a design preflight in `topology` schedules the Designer's one authoring turn and blocks UI dev-task dispatch until the spec is approved; on approval the reconciler spawns exactly one "materialize design system" DEV task. DEV/REVIEWER prompts gain a `design_contract` segment rendered from the approved artifact (empty when none — golden byte-identity preserved for non-design projects).

**Tech Stack:** Python 3.14 (pydantic v2), pytest; React/TypeScript (UI); real venv at `python/.venv`.

## Global Constraints

- errorta_app is a PUBLIC repo: no tokens/keys/PII in code, tests, commits.
- Asset library is OFL / permissively-licensed ONLY; every file referenced by `manifest.json` MUST exist on disk with a committed LICENSE alongside. Never reference a font not vendored.
- The Designer NEVER gets `code_write` in `turn_controller._ROLE_TOOLS` (read tools + artifact authoring only).
- Modality gating must be provably inert for `cli`/`binary`/`container`: no Designer, no design phase.
- The three anti-drift canaries are the wiring checklist: `_MINIMAL_INTENT_EXAMPLES` (`coding/schemas.py`), `tests/coding/test_prompt_segments_golden.py`, `docs/coding/PM_REFERENCE.md` contract block (`tests/coding/test_f145_pm_reference.py`).
- The golden prompt-segment update is a DELIBERATE one-time change (§4), achieved by inserting a `design_contract` renderer call that returns "" for design-less fixtures (byte-identical).
- Verify in the real venv: `cd python && .venv/bin/python -m pytest -q`. ruff clean on changed Python.
- Do NOT build Slice 2 (§5/§8) or Slice 3 (§6/§7). Add `direction_matrix` fields to `body_json` schema now (Slice 1) but enforcement is Slice 3.

---

### Task 1: Role constant wiring (drives the 3 canaries)

**Files:**
- Modify: `python/errorta_council/coding/topology.py` (add `DESIGNER = "designer"`; worker priority `(TESTER, REVIEWER, DESIGNER, DEV)`; `coding_role_of` whitelist)
- Modify: `python/errorta_council/coding/ledger.py:47` (`_VALID_ROLES`)
- Modify: `python/errorta_council/coding/schemas.py:25` (`CodingRole` literal) + `_MINIMAL_INTENT_EXAMPLES` (designer example) + `_INTENT_BY_ROLE` + `_DEFAULT_INTENT_KIND`
- Modify: `python/errorta_council/coding/turn_controller.py:27` (`_ROLE_TOOLS[DESIGNER] = ()` — NO code_write)
- Modify: `python/errorta_council/coding/capabilities.py` (`_CAN_EXECUTE`, `_summary_for`, manifest builder include designer)
- Modify: `python/errorta_council/coding/skills.py:17` (`ROLE_SKILLS[DESIGNER]`, directives)
- Modify: `python/errorta_council/coding/control_actions.py:38` (`_TASK_ROLES`) — designer tasks are host-spawned, not PM-created; keep PMTask.role excluding designer
- Modify: `python/errorta_council/coding/team_log.py` (role projection/labels)
- Modify: `docs/coding/PM_REFERENCE.md` contract block `coding_roles` + the roles prose table
- Modify: `python/tests/coding/test_f145_pm_reference.py:58` (add DESIGNER to expected roles)
- Test: `python/tests/coding/test_designer_role_wiring.py` (new)

**Interfaces:**
- Produces: `topology.DESIGNER == "designer"`; `coding_role_of({"metadata":{"coding_role":"designer"}}) == "designer"`; `turn_controller.allowed_tools_for_role("designer") == ()`.

- [ ] Step 1: Write failing tests in `test_designer_role_wiring.py`:
  - `coding_role_of` returns "designer" for a designer member.
  - `ledger._VALID_ROLES` contains "designer" (and a `LedgerStore.add_task(role="designer")` succeeds).
  - `turn_controller.allowed_tools_for_role("designer") == ()` and `"code_write" not in allowed_tools_for_role("designer")`.
  - `capabilities.capability_manifest(...)` includes a designer entry with `can_execute is False` and no code_write tool.
  - `parse_coding_turn("designer", ...)` validates a `design_spec` authoring intent (see Task 2 for the intent).
- [ ] Step 2: Run — expect failures.
- [ ] Step 3: Add `DESIGNER = "designer"` to topology; update `_WORKER_PRIORITY = (TESTER, REVIEWER, DESIGNER, DEV)`; add DESIGNER to `coding_role_of`'s membership check. Add to ledger `_VALID_ROLES`. Add to schemas `CodingRole`, `_INTENT_BY_ROLE`, `_DEFAULT_INTENT_KIND`, and a `("designer","design_spec")` entry in `_MINIMAL_INTENT_EXAMPLES`. Add `_ROLE_TOOLS[DESIGNER] = ()`. Add DESIGNER to `_CAN_EXECUTE`, `_summary_for`, and the manifest role loop in capabilities. Add `ROLE_SKILLS[DESIGNER]` + directive in skills. Update team_log projection. Update PM_REFERENCE contract `coding_roles` list + the F145 canary test.
- [ ] Step 4: Run new tests + the three canaries (`test_f145_pm_reference.py`, `test_prompt_segments_golden.py`, `test_spec25_expressibility.py`) — all green.
- [ ] Step 5: Commit.

### Task 2: `design_spec` governance artifact + `body_json` schema

**Files:**
- Modify: `python/errorta_council/coding/governance.py` (`ArtifactKind` add `"design_spec"`; `_artifact_kind` allowed set)
- Create: `python/errorta_council/coding/design_spec.py` — pydantic `DesignSpecBody` validating `direction_matrix`, `tokens`, `assets`, `screens`, `components`; `validate_design_body(body_json) -> (ok, errors)`; the Designer authoring intent schema used by `parse_coding_turn`.
- Modify: `python/errorta_council/coding/schemas.py` — a `DesignerSpecIntent` (kind `"design_spec"`, carries `body_markdown` + `body_json`) added to the role-intent union for role "designer".
- Test: `python/tests/coding/test_design_spec_schema.py`

**Interfaces:**
- Produces: `design_spec.DIRECTION_AXES` (tuple: `typography`, `color`, `density`, `shape`, `motion`, `era_mood`); `design_spec.validate_design_body(dict) -> tuple[bool, list[str]]`; `schemas.DesignerSpecIntent`.

- [ ] Step 1: Failing tests: a valid body_json (all axes + tokens + assets + screens + components) validates; a body missing `direction_matrix` axes fails with the named axis; `parse_coding_turn("designer", None, <envelope>)` returns a `ParsedTurn` whose intent is `DesignerSpecIntent`.
- [ ] Step 2: Run — fail.
- [ ] Step 3: Implement `design_spec.py` + `DesignerSpecIntent`; add `"design_spec"` to governance ArtifactKind + `_artifact_kind`.
- [ ] Step 4: Run — pass.
- [ ] Step 5: Commit.

### Task 3: Host-side OFL asset library

**Files:**
- Create: `python/errorta_council/coding/assets/design_library/<family>/<font>.ttf` + `OFL.txt` per family (real vendored OFL fonts).
- Create: `.../design_library/icons/<set>/…` + LICENSE.
- Create: `.../design_library/manifest.json` (per family: id, weights, personality tags, license, file paths; icon set entry).
- Create: `python/errorta_council/coding/assets/__init__.py` + a loader `design_library.py` with `load_manifest() -> dict` and `library_root() -> Path`.
- Test: `python/tests/coding/test_design_asset_library.py`

**Interfaces:**
- Produces: `design_library.load_manifest()`, `design_library.library_root()`.

- [ ] Step 1: Failing test: manifest loads; every family has id/weights/tags/license/files; EVERY referenced file path exists on disk; every family/icon dir has a LICENSE/OFL.txt; families span ≥5 personality tags.
- [ ] Step 2: Run — fail.
- [ ] Step 3: Vendor real OFL fonts (geometric sans, humanist sans, slab, display serif, mono, + more as obtainable) each with OFL.txt; one stroke-icon set with LICENSE; write manifest.json referencing ONLY vendored files; write loader.
- [ ] Step 4: Run — pass.
- [ ] Step 5: Commit.

### Task 4: Modality gating in recipes (Designer only for UI modalities)

**Files:**
- Modify: `python/errorta_council/coding/recipes.py` (`resolve_team(recipe, routes, *, modality=None)` seats a designer for `static`/`desktop`/`server` only)
- Modify: `python/errorta_council/coding/project_factory.py` (pass `charter["modality"]`)
- Test: `python/tests/coding/test_designer_modality_gating.py`

**Interfaces:**
- Consumes: `recipes.resolve_team`.
- Produces: `resolve_team(recipe, routes, modality="static")` includes a designer member; `modality="cli"` (and None/binary/container) includes none.

- [ ] Step 1: Failing tests: `resolve_team(...,modality="static")` → a member with coding_role "designer"; `modality="cli"` → no designer; default (no modality) → no designer (back-compat).
- [ ] Step 2: Run — fail.
- [ ] Step 3: Add optional `modality` kwarg + `_UI_MODALITIES = {"static","desktop","server"}`; append a designer member (route = reviewer route) when UI. Thread modality through `create_project_from_charter`.
- [ ] Step 4: Run — pass, plus existing recipes/project_factory tests still green.
- [ ] Step 5: Commit.

### Task 5: Design scheduling — author turn, UI-dev block, review/approval, materialize-once

**Files:**
- Create: `python/errorta_council/coding/design_scheduler.py` — `next_design_action(ledger, by_role)` returning a `DesignPlan`/`DesignReview` or None; helpers `design_required(by_role)`, `ui_dispatch_blocked(ledger)`, `is_ui_task(task)`.
- Modify: `python/errorta_council/coding/topology.py` — add `DesignPlan(member_id)` dataclass + `DesignReview` (if needed); call design preflight in `decide_next`/`plan_next_batch` after governance preflight; filter UI dev tasks while blocked.
- Modify: `python/errorta_council/coding/topology.py` reconciler OR governance approval path — on `design_spec` approval spawn exactly one materialize DEV task (`design_materialize.py`).
- Create: `python/errorta_council/coding/design_materialize.py` — `spawn_materialize_task_if_needed(store, governance) -> bool` (idempotent: once).
- Modify: `python/errorta_council/coding/runner.py` — dispatch arm for `DesignPlan` (calls `_designer_prompt`, parses `parse_coding_turn("designer",...)`, appends the design_spec artifact under_review); design_spec review reuses governance review machinery or a dedicated `DesignReview` arm; on approval call `spawn_materialize_task_if_needed`.
- Test: `python/tests/coding/test_design_scheduling.py`

**Interfaces:**
- Consumes: `governance.GovernanceStore`, `topology.DESIGNER`.
- Produces: `topology.DesignPlan`; `design_scheduler.next_design_action`; `design_scheduler.is_ui_task`; `design_materialize.spawn_materialize_task_if_needed`.

- [ ] Step 1: Failing tests:
  - With a designer seated and no design_spec, `decide_next` returns a `DesignPlan(designer_id)`.
  - While design_spec not approved, a UI dev task (touches web paths, e.g. `index.html`) is NOT dispatched but a non-UI dev task (e.g. `server.py` backend) IS.
  - `cli` project (no designer): `decide_next` never returns DesignPlan and does not block dev dispatch (inert).
  - On approval, `spawn_materialize_task_if_needed` creates exactly one DEV task titled "materialize design system"; a second call creates none.
- [ ] Step 2: Run — fail.
- [ ] Step 3: Implement design_scheduler + DesignPlan + topology preflight + UI-dev filter + materialize idempotency; wire runner dispatch arm + `_designer_prompt`/`_designer_prompt_segments`; parse via `parse_coding_turn`.
- [ ] Step 4: Run — pass.
- [ ] Step 5: Commit.

### Task 6: `design_contract` prompt segment + golden update

**Files:**
- Modify: `python/errorta_council/coding/runner.py` — `_design_contract_text(store, task=None) -> str` (returns "" when no approved design_spec); insert a `PromptSegment("design_contract", ...)` into `_dev_prompt_segments` and `_review_pr_prompt_segments` at a fixed position.
- Modify: `python/tests/coding/test_prompt_segments_golden.py` — call `_design_contract_text` at the same insertion point in `_old_dev_prompt` and `_old_review_pr_prompt` reference builders (byte-identity: returns "" for fixtures).
- Test: `python/tests/coding/test_design_contract_segment.py`

**Interfaces:**
- Consumes: approved `design_spec` artifact `body_json`/`body_markdown`.
- Produces: `runner._design_contract_text`.

- [ ] Step 1: Failing tests: with an approved design_spec, `_dev_prompt(task, store)` contains a token summary / do-don'ts marker; without one, the prompt is byte-identical to today (golden still passes).
- [ ] Step 2: Run — fail.
- [ ] Step 3: Implement `_design_contract_text`; add the segment to both segment builders; update the golden reference builders to call it at the same spot.
- [ ] Step 4: Run — the golden test + new tests pass.
- [ ] Step 5: Commit.

### Task 7: UI + CLI role touchpoints

**Files:**
- Modify: `src/features/rooms/CouncilRoomEditor.tsx`, `src/features/coding/index.tsx` (role picker/labels/order/color add "designer")
- Modify: `python/errorta_cli/teamdraft.py`, `commands/task.py`, `commands/team.py`, `render/__init__.py` (role enumerations)
- Test: whatever CLI/TS check the repo has (run if feasible; else make minimal correct edits and note it).

- [ ] Step 1: Add "designer" to every role enumeration/label/order/color found by the Explore mapping.
- [ ] Step 2: Run any CLI role tests + TS typecheck if runnable.
- [ ] Step 3: Commit.

---

## Self-Review

- §1 role wiring → Task 1 + Task 7 (every touchpoint in the §1 table). Canaries → Task 1.
- §2 design_spec artifact + governance gate → Task 2 (artifact/schema) + Task 5 (dispatch-blocking phase).
- §2 direction_matrix fields in body_json (Slice 1) → Task 2.
- §3 asset library → Task 3.
- §4 materialize task + design_contract segment → Task 5 (materialize) + Task 6 (segment, golden update).
- §9 Slice-1 tests: role wiring (T1), governance blocks UI dispatch / not non-UI (T5), materialize once (T5), design_contract renders + golden (T6), cli modality inert (T4/T5), asset manifest + files exist w/ LICENSE (T3).
- Out of scope (Slice 2/3) not built. Confirmed.
