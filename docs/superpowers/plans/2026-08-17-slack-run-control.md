# Slack Run Control (Slice 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** From a project's Slack channel, tell the PM to start / stop the coding team and see run status.

**Architecture:** Extend the per-project tool surface (`errorta_slack/tools.py`) with `start_run` (C-class, spends), `stop_run` (R), and a run-lifecycle-state addition to `project_status`. Start reaches the app's real `_start_run` via an injectable lazy seam; stop/status use the existing `ledger_factory`.

## Global Constraints
- **Injection guard:** `start_run` executes ONLY with `confirmed_via="block_actions"` (verified button); never from text. (Spec §3.)
- **One real seam, lazily imported:** `start_run_fn` default wraps `errorta_app.routes.coding._start_run(pid, {}, continue_=True)` with the import INSIDE the wrapper — `tools.py` must NOT import `errorta_app.routes` at module load (optionality). (Spec §3.)
- **Don't** call `CodingRunner.run` directly or use `RunControl`/`run_store`. (Spec §2.)
- No real run in tests (inject `start_run_fn`, `ledger_factory` fakes). PUBLIC repo; placeholders.
- Verb names distinct from `launch_runtime`/`stop_runtime` (those are the preview process, not the run).
- Run: `( cd python && .venv/bin/python -m pytest tests/slack -q )`; ruff clean.

---

## Task 1: run-control verbs in `tools.py` (+ injection guard)

**Files:** Modify `python/errorta_slack/tools.py`; Test `python/tests/slack/test_tools.py`.

**Interfaces / Produces:**
- `TOOL_CATALOG` gains `start_run`(trust "C"), `stop_run`(trust "R"); `project_status` result gains a `run_status` key.
- `ToolDeps` gains `start_run_fn: Callable[[str], dict] | None = None` (default → lazy `_start_run` wrapper).
- `dispatch`: `start_run` with `confirmed_via != "block_actions"` → stage confirmation (`needs_confirmation`), does NOT call `start_run_fn`; with `"block_actions"` → `deps.start_run_fn(project_id)` → `{"status":"started"}` (swallow a benign "already in progress" 409 → `{"status":"already_running"}`; member-health/other errors → clean `{"status":"error","detail":...}`, no uncaught raise). `stop_run` → `deps.ledger_factory(project_id).set_run_state(cancel_requested=True)` → `{"status":"stopping"}` (or friendly no-op if run_state status is idle). `project_status` → add `deps.ledger_factory(project_id).get_run_state().get("status") or "idle"` to the returned dict.

- [ ] **Step 1: Failing tests** —
  - Injection: `dispatch("start_run", {}, channel_id=.., thread_ts=.., confirmed_via=None, deps=..)` → `needs_confirmation`, fake `start_run_fn` call-count 0; `confirmed_via="block_actions"` → `start_run_fn` called with the project id, returns `status=="started"`.
  - `start_run_fn` raising a 409-shaped "already in progress" → `status=="already_running"`; raising an arbitrary `Exception` → `status=="error"`, no crash.
  - `stop_run` → fake `ledger_factory(pid).set_run_state` called with `cancel_requested=True`; returns stopping.
  - `project_status` includes `run_status` from a fake `get_run_state`.
  - Trust map: `TOOL_CATALOG["start_run"]["trust"]=="C"`, `["stop_run"]["trust"]=="R"`.
- [ ] **Step 2–4:** Run FAIL → implement (mirror the existing C-class staging + the injectable-seam pattern used by `launch_runtime`) → Run PASS.
- [ ] **Step 5: Commit** `feat(slack): run control verbs (start_run[C]/stop_run) + status`.

---

## Task 2: docs + canary + gate

**Files:** Modify `docs/SLACK_STUDIO.md` (or the relevant Slack doc) to describe run control; Test: the existing `tests/slack/test_catalog_canary.py` must stay green with the new verbs; add a trust-class assertion if not covered by Task 1.

- [ ] **Step 1:** Run `tests/slack/test_catalog_canary.py` — confirm the per-project catalog canary still passes (advertised verbs == dispatch verbs) now that `start_run`/`stop_run` exist. If it fails, reconcile (the prompt renders from the catalog, so it should auto-include them).
- [ ] **Step 2:** Docs: add a short "Run control" section — in a project's channel, "start building" (Approve button, spends model calls up to the iteration cap) / "stop" (graceful) / "how's it going" (idle/running/stopped + progress). Note it's the coding team, distinct from `launch_runtime` (preview). Placeholders only.
- [ ] **Step 3:** Full gate: `( cd python && .venv/bin/python -m pytest tests/slack -q )` green; `ruff check errorta_slack` clean; `import errorta_app.server` still doesn't pull in `errorta_slack`.
- [ ] **Step 4: Commit** `docs(slack): run control + canary`.

## Self-Review
- Spec §3 verbs → T1; §3 injection → T1 test; §4 error handling → T1 (409/arbitrary); §5 testing → T1/T2; §6 deferred → not built. Types: `start_run_fn`, `run_status` key, trust classes consistent.
