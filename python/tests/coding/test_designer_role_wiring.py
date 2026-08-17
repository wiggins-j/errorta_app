"""Designer role (Slice 1 §1) — role-constant wiring across every touchpoint.

The three anti-drift canaries (minimal-intent table, golden prompt segments,
PM_REFERENCE contract block) cover most of the wiring; these tests lock the rest
named in the spec's §9: ``coding_role_of``, ledger role validation, the
``_ROLE_TOOLS`` no-``code_write`` invariant, and the capability manifest.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding import capabilities
from errorta_council.coding.ledger import _VALID_ROLES, LedgerStore
from errorta_council.coding.schemas import (
    _DEFAULT_INTENT_KIND,
    _INTENT_BY_ROLE,
    _MINIMAL_INTENT_EXAMPLES,
    parse_coding_turn,
)
from errorta_council.coding.topology import DESIGNER, coding_role_of
from errorta_council.coding.turn_controller import allowed_tools_for_role


def test_designer_constant() -> None:
    assert DESIGNER == "designer"


def test_coding_role_of_resolves_designer() -> None:
    assert coding_role_of({"metadata": {"coding_role": "designer"}}) == "designer"
    assert coding_role_of({"coding_role": "designer"}) == "designer"


def test_ledger_accepts_designer_role(tmp_errorta_home: Path) -> None:
    assert "designer" in _VALID_ROLES
    store = LedgerStore("designer-ledger")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    task = store.add_task(title="Author the design spec", role="designer")
    assert task.role == "designer"


def test_designer_never_gets_code_write() -> None:
    """The load-bearing tool-discipline invariant: the Designer authors artifacts
    and reads the repo — it MUST NOT be able to write to the worktree."""
    tools = allowed_tools_for_role("designer")
    assert "code_write" not in tools
    assert tools == ()


def test_capability_manifest_includes_designer(tmp_errorta_home: Path) -> None:
    store = LedgerStore("designer-cap")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    manifest = capabilities.capability_manifest(store)
    assert "designer" in manifest
    designer = manifest["designer"]
    assert designer.can_execute is False
    assert "code_write" not in designer.tools
    # SPEC-26 closure drift lock: every manifest role has a closure story.
    assert "designer" in capabilities.CLOSURE_TABLE


def test_designer_intent_wired_into_parse() -> None:
    assert "designer" in _INTENT_BY_ROLE
    assert _DEFAULT_INTENT_KIND["designer"] == "design_spec"
    assert ("designer", "design_spec") in _MINIMAL_INTENT_EXAMPLES
    envelope = (
        '{"schema_version": "coding_turn.v1", "role": "designer", '
        '"intent": {"kind": "design_spec", '
        '"body_markdown": "Aesthetic direction: warm, editorial.", '
        '"body_json": {"direction_matrix": {"typography": "humanist sans"}}}}'
    )
    parsed = parse_coding_turn("designer", None, envelope)
    from errorta_council.coding.schemas import DesignerSpecIntent, TurnParseError
    assert not isinstance(parsed, TurnParseError), parsed
    assert isinstance(parsed.intent, DesignerSpecIntent)
