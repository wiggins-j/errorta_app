"""F143-01 Slice F — prompt-builder segmentation preserves prompt bytes (invariant 7).

THE critical safety test for the segmented-builder refactor. Each coding-team prompt
builder was refactored to assemble an ordered list of labeled ``PromptSegment``s that
``join_segments`` concatenates verbatim. If that concatenation ever drifts from the
pre-refactor prompt string — an added/dropped separator, a reordered segment — the
model receives different bytes and its behavior changes. These tests lock byte-
identity by reconstructing the ORIGINAL prompt from an inlined literal copy of the
pre-refactor concatenation (using the module's own dynamic helpers for the runtime
parts) and asserting it equals ``join_segments(<builder>_segments(...))`` exactly.

They also assert the two structural properties that make the join trustworthy:
* ``join_segments`` adds NO separators (the joined string equals the public builder's
  output — the builder IS the segments joined);
* the category tokens of the composition sum to ``sent_total``.
"""
from __future__ import annotations

import json
from pathlib import Path

from errorta_council.coding.gate_state import latest_gate_text
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _composition_from_segments,
    _dev_prompt,
    _dev_prompt_segments,
    _grounding_packet_text,
    _latest_context_response_text,
    _model_assignment_prompt,
    _orientation_text,
    _pm_boot_text,
    _pm_prompt,
    _pm_prompt_segments,
    _review_pr_prompt,
    _review_pr_prompt_segments,
    _skill_line,
    _test_prompt,
    _test_prompt_segments,
    join_segments,
)
from errorta_council.coding.topology import DEV, PM, REVIEWER, TESTER
from errorta_council.coding.turn_controller import tool_catalog_text

# --------------------------------------------------------------------------- #
# Spec 12-18 prep (P0.5) — segment-ownership seam.
#
# Two engineers add prompt segments to these same four prompts in parallel
# branches, and this file byte-locks all of them. To keep their edits on
# different lines, the reference builders below CALL the two renderers instead of
# inlining their strings, at the positions the batch plan fixes:
#
#   dev:      … repo_snapshot, gate_output(Spec 12), tool_guidance(Spec 17), …
#   reviewer: … pr_diff, gate_output(Spec 12), trunc_note, tool_guidance(Spec 17), …
#   tester:   … project_context, gate_output(Spec 12), tool_guidance(Spec 17), …
#
# `latest_gate_text` returns "" for every fixture here (none records a test run),
# so placing it now is a NO-OP that pre-books Spec 12's insertion point. Spec 17
# likewise only widens the existing `tool_catalog_text` call.
#
# The empty-string behaviour is itself the contract: a gate-less project's
# prompts must stay byte-identical to today, which is why the renderer returns
# "" rather than an empty labelled block.
# --------------------------------------------------------------------------- #


def _project(pid: str, *, north: str = "build a game",
             dod: str = "it runs") -> LedgerStore:
    store = LedgerStore(pid)
    store.create_project(north_star=north, definition_of_done=dod,
                         target="new", repo_path=None)
    return store


# --- _pm_prompt ----------------------------------------------------------------

def _old_pm_prompt(store: LedgerStore) -> str:
    """Inlined copy of the pre-refactor ``_pm_prompt`` final concatenation. ``pin``
    and ``done_gate`` recompute exactly as the live builder does, so this is a faithful
    byte-for-byte reference independent of the new segment split."""
    from errorta_council.coding.completion import (
        pending_completion_work,
        summarize_open_items,
    )
    from errorta_council.coding.ledger import format_focus_lines

    pending = store.list_unconsumed_interjections()
    pin = ""
    if pending:
        lines = "\n".join(f"- {p.get('message', '')}" for p in pending)
        pin = (
            "AUTHORITATIVE USER DIRECTION (higher weight than your own judgment — "
            f"follow it):\n{lines}\n\n"
        )
    try:
        active_focuses = store.active_focuses()
    except Exception:
        active_focuses = []
    if active_focuses:
        pin = (
            "CURRENT FOCUS — the team's operative scope right now. Plan ONLY these, "
            "in order:\n" + "\n".join(format_focus_lines(active_focuses)) + "\n"
            "The North Star is REFERENCE ONLY — a guardrail for HOW to build, not a "
            "list of things to build now. Do NOT expand scope beyond the Current "
            "Focus. Create and order DEV tasks per focus; when one focus (or task) "
            "depends on another, order the tasks and their PRs so the dependency "
            "merges first; independent focuses may interleave by priority.\n\n"
        ) + pin
    else:
        try:
            work_request = (store.get_project().work_request or "").strip()
        except Exception:
            work_request = ""
        if work_request:
            pin = (
                f"CURRENT FOCUS — right now, work on this: {work_request}\n"
                "Scope your tasks to this focus; do not rewrite unrelated parts of "
                "the project.\n\n"
            ) + pin
    try:
        from errorta_project_grounding.context_packets import ensure_pm_working_memory
        ensure_pm_working_memory(store)
    except Exception:
        pass
    done_gate = ""
    open_items = pending_completion_work(store)
    if open_items:
        done_gate = (
            "You may NOT declare the project done — these items are still open: "
            f"{summarize_open_items(open_items)}. Finish them. If an item is "
            "obsolete, identify it in a decision for the operator to drop; the "
            "current PM plan schema has no cancel intent. An item marked "
            "(human-required) — a "
            "blocked task or a conflicted PR — cannot be auto-closed; leave it and "
            "the run will surface it for the human.\n"
        )
    return (
        f"{pin}{done_gate}{_skill_line(PM)} You are the PM of an autonomous coding team.\n"
        f"{_model_assignment_prompt(store)}"
        f"Project state: {_orientation_text(store)}\n"
        f"{_pm_boot_text(store) or _grounding_packet_text('pm', store)}"
        "Plan the next batch of DEV tasks only — each task is a unit of code a "
        "developer implements. Review, testing, and merge happen AUTOMATICALLY "
        "for every task (each opened PR is reviewed, tested, and merged into "
        "master), so do NOT create reviewer/tester/merge tasks. Keep tasks small "
        "and ordered (use depends_on by title when one builds on another). "
        "If any task uses third-party packages, have the foundation task also add "
        "the matching dependency manifest (e.g. requirements.txt / package.json). "
        "Make each task easy for a weaker model: one self-contained responsibility "
        "per task, with the acceptance criteria and the exact files/interfaces in "
        "scope written in its detail — the more you specify, the less the worker "
        "has to guess. Reply "
        "with ONLY a coding_turn.v1 envelope: "
        '{"schema_version": "coding_turn.v1", "role": "pm", "intent": '
        '{"kind": "plan", "done": false, "tasks": [{"title": "...", '
        '"role": "dev", "detail": "Acceptance criteria... In-scope files...", '
        '"depends_on": [], "task_type": "implementation", '
        '"difficulty_tier": "mid", "preferred_member_id": "m-dev", '
        '"preferred_route_id": "provider.model", '
        '"assignment_rationale": "why this is the cheapest capable route"}]}}. '
        'Set done=true ONLY when the North Star is fully met and nothing remains '
        "(then include a non-empty \"completion_summary\" and omit tasks)."
    )


def test_pm_prompt_segments_byte_identical_plain(tmp_errorta_home: Path) -> None:
    _project("pm0")
    ref = _old_pm_prompt(LedgerStore("pm0"))
    # The public builder returns the joined segments — must equal the old reference.
    assert _pm_prompt(LedgerStore("pm0")) == ref
    # And the join of the segment list is byte-identical to that reference too.
    segs = _pm_prompt_segments(LedgerStore("pm0"), pin="", done_gate="")
    # (pin/done_gate default empty for the plain project — matches the reference)
    assert join_segments(segs) == ref


def test_pm_prompt_segments_byte_identical_with_work_request(
        tmp_errorta_home: Path) -> None:
    store = _project("pm1")
    store.set_work_request("wire the webhook")
    ref = _old_pm_prompt(LedgerStore("pm1"))
    assert _pm_prompt(LedgerStore("pm1")) == ref
    assert "CURRENT FOCUS" in ref and "wire the webhook" in ref


# --- _dev_prompt ---------------------------------------------------------------

def _old_dev_prompt(task, store: LedgerStore, readback: str = "") -> str:
    existing = (f"Current files in the worktree (EXTEND these — do not drop "
                f"existing code; code_write replaces the whole file so include "
                f"all of it):\n{readback}\n" if readback
                else "The worktree is empty; create the files from scratch.\n")
    return (
        f"{_skill_line(DEV)} You are a developer for task id {task.task_id!r}: "
        f"{task.title}. {task.detail}\n"
        f"Context: {_orientation_text(store)}\n"
        f"{_grounding_packet_text('dev', store, task=task)}"
        f"{_latest_context_response_text(store, task.task_id)}"
        f"{existing}"
        f"{latest_gate_text(store)}"          # Spec 12 inserts here
        f"{tool_catalog_text(DEV)} Do not request merge-back.\n"
        "Implement the task via tool-backed writes; preserve all prior functions. "
        "If you write a web server, read its listen port from the PORT environment "
        "variable (with a sensible default) instead of hardcoding one, so the "
        "runtime can bind a free port. "
        "A binary asset (an image, font, audio clip, or any non-text file — e.g. a "
        "PNG sprite or tileset) MUST be written as REAL bytes: emit code_write with "
        '{"path": "...", "content_base64": "<base64 of the actual file bytes>"} '
        "(never a text description or placeholder in a binary file body — an "
        "undecodable .png is not a valid image). "
        "Reply with ONLY a coding_turn.v1 envelope: "
        '{"schema_version": "coding_turn.v1", "role": "dev", "task_id": '
        f'"{task.task_id}", "intent": {{"kind": "tool_plan", "task_type": '
        '"implementation", "tool_calls": [{"tool": "code_write", "args": '
        '{"path": "rel/path", "content": "..."}}]}}.'
    )


def test_dev_prompt_segments_byte_identical(tmp_errorta_home: Path) -> None:
    store = _project("dev0")
    task = store.add_task(title="Add parser", role="dev", detail="Parse the input")
    for readback in ("", "print('hi')\n"):
        ref = _old_dev_prompt(task, LedgerStore("dev0"), readback)
        assert _dev_prompt(task, LedgerStore("dev0"), readback) == ref
        assert join_segments(
            _dev_prompt_segments(task, LedgerStore("dev0"), readback)) == ref


# --- _test_prompt --------------------------------------------------------------

def _old_test_prompt(task, store: LedgerStore) -> str:
    registry = store.get_test_commands()
    if registry:
        ids = ", ".join(sorted(registry.keys()))
        avail = (f"Available test command_ids (you MUST choose from these): {ids}.")
    else:
        avail = ("No test commands are configured for this project, so there is "
                 "nothing to run — reply with empty \"command_ids\": [] and "
                 "\"not_applicable\": true (the test gate is non-blocking).")
    return (
        f"{_skill_line(TESTER)} You are a tester for task id {task.task_id!r}: "
        f"{task.title}.\n"
        f"Context: {_orientation_text(store)}\n"
        f"{_grounding_packet_text('tester', store, task=task)}"
        f"{latest_gate_text(store)}"          # Spec 12 inserts here
        f"{avail} You CANNOT declare pass or fail — the verdict comes from the "
        "REAL exit code of the commands actually run.\n"
        "This PR implements ONE scoped task, not the whole product. If NO "
        "registered command meaningfully exercises THIS task's slice (e.g. the "
        "project is not yet runnable end-to-end and the full suite would only "
        "fail on not-yet-built modules), you MAY reply with an empty "
        '"command_ids": [] and "not_applicable": true plus a rationale; the '
        "test gate is then non-blocking for this slice. This is NOT a way to "
        "dodge a real failure: a command that runs and returns non-zero for a "
        "genuine in-scope defect still blocks — so if any registered command "
        "does exercise this slice, run it and let its exit code govern.\n"
        'Reply with ONLY a coding_turn.v1 envelope: {"schema_version": '
        f'"coding_turn.v1", "role": "tester", "task_id": "{task.task_id}", '
        '"intent": {"kind": "test_plan", "command_ids": ["<id>", ...], '
        '"scope": "full_project", "not_applicable": false, "rationale": '
        '"..."}}.'
    )


def test_test_prompt_segments_byte_identical(tmp_errorta_home: Path) -> None:
    store = _project("test0")
    task = store.add_task(title="Test it", role="dev")
    ref = _old_test_prompt(task, LedgerStore("test0"))
    assert _test_prompt(task, LedgerStore("test0")) == ref


# --- _review_pr_prompt ---------------------------------------------------------

def _gate_text_for_review() -> str:
    """The reviewer reference builder has no ``store`` in scope (the real prompt
    takes its project context pre-rendered), so the Spec 12 seam is a named stub
    here. It becomes ``latest_gate_text(store)`` when Spec 12 threads the store
    through — until then both sides are "", so the golden holds either way."""
    return ""


def _old_review_pr_prompt(task, pr, diff, project_context, scope_task=None) -> str:
    from errorta_council.coding.runner import (
        _REVIEW_DIFF_CAP,
        _filter_generated_from_diff,
        _task_is_governance_sourced,
    )

    diff = _filter_generated_from_diff(diff)
    cap = diff[:_REVIEW_DIFF_CAP]
    truncated = len(diff) > _REVIEW_DIFF_CAP
    trunc = " [diff truncated]" if truncated else ""
    trunc_note = (
        "The diff above was truncated to fit — code beyond the cut is NOT shown. "
        "This is a tooling limit, not evidence of a source-code defect, but review "
        "coverage is incomplete and unseen code cannot be approved. Set approved "
        "to false and include one finding asking the author to split or reduce the "
        "change so its complete diff can be reviewed. Do not speculate about defects "
        "in code that is not shown.\n"
        if truncated else ""
    )
    st = scope_task or task
    task_scope = f"{st.title}. {st.detail}".strip()
    if _task_is_governance_sourced(st):
        bar = ("This task is governance-sourced: its acceptance bar is the plan "
               "slice's done_when / review_focus in the Governance planning "
               "context above (fall back to the task scope if that is absent).")
    else:
        bar = ("Its acceptance bar is the task scope stated above.")
    example_findings = ([{
        "severity": "major",
        "title": "Diff exceeds review context",
        "body": "Split or reduce this change so the complete diff can be reviewed.",
    }] if truncated else [])
    verdict_example = json.dumps({
        "schema_version": "coding_turn.v1",
        "role": "reviewer",
        "task_id": task.task_id,
        "intent": {
            "kind": "review_verdict",
            "reviewed_head": pr.get("head"),
            "approved": not truncated,
            "findings": example_findings,
        },
    })
    return (
        f"{_skill_line(REVIEWER)} You are a reviewer for task id {task.task_id!r}. "
        f"Review this PR (branch {pr.get('branch')}) before it merges to master.\n"
        f"The scope of THIS PR is ONE task: {task_scope}\n"
        f"{project_context}"
        "This PR implements ONE scoped task, not the whole product. "
        f"{bar}\n"
        "REQUEST CHANGES (blocking) if EITHER holds:\n"
        "(a) the change does not correctly AND fully implement THIS task's own "
        "stated scope — a partial or incorrect implementation of THIS task (e.g. "
        "the task names three classes and only two are present) IS a defect and "
        "must be sent back; or\n"
        "(b) the change breaks or drops any code already on master, or introduces "
        "a contract mismatch — a type/signature/import inconsistent with the "
        "merged surface OR with an incompatible shared type another in-flight PR "
        "defines. When you see such a mismatch you MUST write a finding naming it "
        "(that is how the shared contract gets centralized).\n"
        "NOT a reason to request changes: the overall product being incomplete, "
        "or functionality that belongs to OTHER tasks being absent. Sibling tasks "
        "listed as in-flight or todo/backlog will deliver the rest — that is "
        "out-of-scope future work, not a defect in this PR. Distinguish 'missing "
        "part of THIS task's scope' (block) from 'missing another task's work' "
        "(fine). The North Star / Definition of Done are directional context only.\n"
        f"PR diff vs master{trunc}:\n```diff\n{cap}\n```\n"
        f"{_gate_text_for_review()}"          # Spec 12 inserts here
        f"{trunc_note}"
        f"The PR head you are reviewing is {pr.get('head')!r}; echo it verbatim as "
        '"reviewed_head".\n'
        "Reply with ONLY a coding_turn.v1 envelope: "
        f"{verdict_example}. "
        "If approved=false you MUST include at least one finding."
    )


def test_review_pr_prompt_segments_byte_identical(tmp_errorta_home: Path) -> None:
    store = _project("rev0")
    dtask = store.add_task(title="Impl feature", role="dev", detail="do the thing")
    pr = {"branch": "feat/x", "head": "abc123", "task_id": dtask.task_id}
    ctx = "North Star: build a game\n"
    # both a small (untruncated) diff and a large (truncated) diff exercise both
    # branches of the truncation logic.
    for diff in ("diff --git a/x.py b/x.py\n+print(1)\n", "X" * 60000):
        ref = _old_review_pr_prompt(dtask, pr, diff, ctx, scope_task=dtask)
        assert _review_pr_prompt(dtask, pr, diff, ctx, scope_task=dtask) == ref


# --- structural properties -----------------------------------------------------

def test_join_segments_adds_no_separators(tmp_errorta_home: Path) -> None:
    """join_segments must be a plain concatenation — the sum of segment lengths
    equals the joined length (no injected separator characters)."""
    store = _project("struct0")
    task = store.add_task(title="X", role="dev", detail="Y")
    segs = _dev_prompt_segments(task, LedgerStore("struct0"), "readback\n")
    joined = join_segments(segs)
    assert len(joined) == sum(len(s.text) for s in segs)
    assert joined == "".join(s.text for s in segs)


def test_composition_categories_sum_to_sent_total(tmp_errorta_home: Path) -> None:
    store = _project("struct1")
    task = store.add_task(title="X", role="dev", detail="Y")
    segs = _dev_prompt_segments(task, LedgerStore("struct1"), "readback\n")
    comp = _composition_from_segments(segs)
    assert comp["sent_total"] == sum(c["tokens"] for c in comp["categories"])
    assert comp["sent_total"] > 0
    # every category uses a taxonomy class + a positive token count
    for cat in comp["categories"]:
        assert isinstance(cat["class"], str) and cat["class"]
        assert isinstance(cat["tokens"], int) and cat["tokens"] > 0


def test_dev_prompt_carries_port_binding_guidance(tmp_errorta_home: Path) -> None:
    # F101-03: the coder is told to bind PORT (not a hardcoded port) so the
    # runtime can assign a free port and the demo/health probe matches the app.
    store = _project("devport")
    task = store.add_task(title="Add server", role="dev", detail="serve")
    assert "PORT environment variable" in _dev_prompt(task, store)
