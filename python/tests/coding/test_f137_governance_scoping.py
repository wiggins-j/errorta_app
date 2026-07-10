"""F137 — the core fix: governance drafting/review prompts scope to the Current
Focus set, with the North Star demoted to reference-only.

Regression lock for the 2026-07-02 failure where a one-line "make the Council
rooms panel collapsible" request produced an 8-feature "Errorta v1.0" spec because
the governance prompt only ever saw the whole-product North Star.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.governance import GovernanceStore
from errorta_council.coding.governance_prompts import (
    _project_context,
    build_governance_review_prompt,
    build_pm_governance_prompt,
)
from errorta_council.coding.ledger import LedgerStore

_WHOLE_PRODUCT_NS = (
    "Errorta is the local-and-remote AI workbench: Judge, Council, Coding Team, "
    "corpus catalog, desktop + iOS apps, and a Service API."
)


def _project(project_id: str = "proj", *, work_request: str = "") -> LedgerStore:
    store = LedgerStore(project_id)
    store.create_project(north_star=_WHOLE_PRODUCT_NS, definition_of_done="v1.0",
                         target="existing", repo_path="/tmp/x",
                         work_request=work_request)
    return store


def test_project_context_scopes_to_focus_and_demotes_north_star(
        tmp_errorta_home: Path) -> None:
    store = _project()
    store.add_focus(title="Make the Council rooms panel collapsible")
    ctx = _project_context(store)
    # the focus is present and framed as the scope
    assert "CURRENT FOCUS" in ctx
    assert "Make the Council rooms panel collapsible" in ctx
    assert "scope this artifact to ONLY" in ctx
    # the North Star is explicitly demoted to reference-only
    assert "REFERENCE ONLY" in ctx
    assert "do NOT expand" in ctx
    # ordering: the focus/scope framing precedes the North Star text
    assert ctx.index("CURRENT FOCUS") < ctx.index(_WHOLE_PRODUCT_NS)


def test_zero_focus_context_is_unchanged(tmp_errorta_home: Path) -> None:
    store = _project()
    ctx = _project_context(store)
    assert ctx == (
        f"North Star: {_WHOLE_PRODUCT_NS}\n"
        "Definition of done: v1.0\n"
        "Status: active\n"
    )


def test_drafting_prompt_is_scoped_to_focus(tmp_errorta_home: Path) -> None:
    store = _project()
    store.add_focus(title="Make the Council rooms panel collapsible")
    governance = GovernanceStore.for_ledger(store)
    prompt = build_pm_governance_prompt(
        store=store, governance=governance, phase="drafting_spec")
    assert "Make the Council rooms panel collapsible" in prompt
    assert "REFERENCE ONLY" in prompt
    assert "plan just this focus set" in prompt or "scope this artifact" in prompt


def test_review_prompt_inherits_the_same_scope(tmp_errorta_home: Path) -> None:
    """The review prompt shares _project_context, so scoping the drafting side
    also scopes the reviewer — the reviewer won't demand whole-product coverage."""
    store = _project()
    store.add_focus(title="Make the Council rooms panel collapsible")
    governance = GovernanceStore.for_ledger(store)
    artifact = governance.append_artifact(
        kind="spec", title="Collapsible rooms panel",
        body_markdown="Make the rooms list collapse.", body_json={},
        source_refs=[])
    prompt = build_governance_review_prompt(
        store=store, governance=governance, artifact=artifact)
    assert "CURRENT FOCUS" in prompt
    assert "Make the Council rooms panel collapsible" in prompt
    assert "REFERENCE ONLY" in prompt


def test_migrated_work_request_scopes_governance(tmp_errorta_home: Path) -> None:
    # a legacy imported project (work_request only, no focus ledger) still scopes
    store = _project(work_request="Add a keyboard shortcut to the composer")
    ctx = _project_context(store)
    assert "Add a keyboard shortcut to the composer" in ctx
    assert "REFERENCE ONLY" in ctx
