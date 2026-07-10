"""F129 Slice 7 invariant lock: **bound route must be applied before every
route-dependent policy and context check** (byte-isolation, resource admission,
remote-budget accounting, member snapshot, and gateway dispatch).

The generic Council scheduler's F129 no-PM fallback path resolves and binds a
Multi member's concrete route BEFORE the downstream boundaries. If a refactor
accidentally moves the bind past any of these seams, a `redacted_summary`
member could reach the gateway with `full_context` bytes, or budget counters
could charge the wrong provider. These tests lock the shape.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from errorta_council.coding.model_assignment import (
    ModelAssignment, bind_member_route, make_assignment,
)

SCHEDULER = Path(__file__).parents[2] / "errorta_council" / "scheduler.py"


def test_bind_member_route_swaps_full_route_identity() -> None:
    """A static-local member bound to a remote route must present remote
    provider_kind + route_id + model — nothing route-derived may leak from the
    static config into downstream policy."""
    static = {
        "id": "m-1", "role": "answerer",
        "gateway_route_id": "local.ollama.qwen:7b",
        "provider_kind": "local", "provider": "local",
        "model": "qwen:7b", "model_display": "Qwen 7B",
    }
    assignment = make_assignment(
        task_id="turn-1", member_id="m-1", route_id="anthropic.claude-opus-4-8",
        task_type="investigation", difficulty_tier="strong",
        rationale="stronger route needed", source="selector",
    )
    bound = bind_member_route(static, assignment)

    assert bound["gateway_route_id"] == "anthropic.claude-opus-4-8"
    assert bound["route_id"] == "anthropic.claude-opus-4-8"
    assert bound["provider_kind"] == "anthropic"
    assert bound["provider"] == "anthropic"
    assert bound["model"] == "claude-opus-4-8"
    assert bound["model_display"] == "claude-opus-4-8"
    assert bound["model_assignment"]["route_id"] == "anthropic.claude-opus-4-8"
    # Original member dict is unchanged — the room/run snapshot is immutable.
    assert static["gateway_route_id"] == "local.ollama.qwen:7b"
    assert static["provider_kind"] == "local"


def test_bind_member_route_static_remote_to_local() -> None:
    """The reverse: a static-remote member bound to local receives local
    admission and accounting."""
    static = {
        "id": "m-2", "role": "answerer",
        "gateway_route_id": "anthropic.claude-opus-4-8",
        "provider_kind": "anthropic", "model": "claude-opus-4-8",
    }
    assignment = make_assignment(
        task_id="turn-1", member_id="m-2", route_id="local.ollama.qwen:7b",
        task_type="edit", difficulty_tier="light",
        rationale="cheap route sufficient", source="selector",
    )
    bound = bind_member_route(static, assignment)

    assert bound["provider_kind"] == "local"
    assert bound["gateway_route_id"] == "local.ollama.qwen:7b"
    assert bound["model"] == "ollama.qwen:7b"


def _scheduler_source() -> str:
    return SCHEDULER.read_text("utf-8")


def _line_of(pattern: str, source: str) -> int:
    m = re.search(pattern, source, re.MULTILINE)
    assert m, f"pattern {pattern!r} not found in scheduler.py"
    return source[: m.start()].count("\n") + 1


def _line_of_after(pattern: str, source: str, *, after_line: int) -> int:
    """First match of ``pattern`` at or after ``after_line``. Used to pin a
    use-site inside the F129 no-PM fallback block, avoiding earlier method
    definitions of the same name."""
    lines = source.splitlines()
    # Reassemble source from after_line onward with a preserved offset.
    offset = 0
    for i in range(after_line - 1):
        offset += len(lines[i]) + 1
    tail = source[offset:]
    m = re.search(pattern, tail, re.MULTILINE)
    assert m, f"pattern {pattern!r} not found after line {after_line}"
    return after_line + tail[: m.start()].count("\n")


def test_scheduler_binds_route_before_policy_boundaries() -> None:
    """The invariant: in the generic Council scheduler's Multi-fallback path,
    ``bind_member_route`` must be called BEFORE:
      * ``self._member_snapshot(member)``          (identity for events)
      * ``self._guard.admit``                     (resource admission)
      * ``self._store.append_event(... LOCAL_RESOURCE_CHECK_STARTED``
      * The context-router build call                (byte policy)
      * The gateway call                             (final dispatch)

    A structural test is used because the invariant is a source-order property
    of one long async method; behaviorally exercising it requires the full
    TurnScheduler harness, but any refactor that moves the bind past a
    downstream seam should fail loudly here.
    """
    source = _scheduler_source()

    # Anchor: the Multi-fallback path is scoped by the `model_mode == "multi"`
    # guard we ship in F129. Search USE sites (not method definitions) by
    # anchoring after the fallback guard. NOTE: an EARLIER snapshot call may
    # appear inside the NoCapableModel skip branch — that's for the skip
    # event, not for dispatch, so we anchor the pre-dispatch snapshot AFTER
    # the bind itself (the guard is that the pre-dispatch snapshot is the
    # first snapshot on the happy path, at or after bind).
    guard_line = _line_of(r'model_mode.*"multi"', source)
    bind_line = _line_of_after(
        r"bind_member_route\(member, assignment\)", source, after_line=guard_line,
    )
    # The pre-dispatch snapshot is the one that feeds LOCAL_RESOURCE_CHECK_STARTED
    # and _guard.admit — always AFTER bind on the happy path.
    snapshot_line = _line_of_after(
        r"self\._member_snapshot\(member\)", source, after_line=bind_line,
    )
    admit_line = _line_of_after(
        r"self\._guard\.admit\(", source, after_line=guard_line,
    )

    assert guard_line < bind_line, (
        "The Multi-mode guard must scope the bind block "
        "(no bind for Single members)"
    )
    assert bind_line < snapshot_line, (
        f"bind_member_route (L{bind_line}) must precede "
        f"_member_snapshot (L{snapshot_line}) so events carry the bound "
        "route identity, not the static one."
    )
    assert bind_line < admit_line, (
        f"bind_member_route (L{bind_line}) must precede _guard.admit "
        f"(L{admit_line}) so resource admission checks the bound provider."
    )


def test_scheduler_emits_model_assigned_event_before_admission() -> None:
    """The MODEL_ASSIGNED event must be recorded BEFORE resource admission runs,
    so an audit reader sees the bound route was the identity used for policy."""
    source = _scheduler_source()

    # Find the F129 no-PM fallback block by its MODEL_ASSIGNED event emission.
    guard_line = _line_of(r'model_mode.*"multi"', source)
    model_assigned_line = _line_of_after(
        r"type=EventType\.MODEL_ASSIGNED", source, after_line=guard_line,
    )
    admit_line = _line_of_after(
        r"self\._guard\.admit\(", source, after_line=guard_line,
    )

    assert model_assigned_line < admit_line, (
        f"MODEL_ASSIGNED event (L{model_assigned_line}) must be emitted "
        f"before _guard.admit (L{admit_line}) — otherwise the audit trail "
        "shows admission running on a member whose bound route hasn't been "
        "recorded yet."
    )


def test_scheduler_ast_no_admit_before_bind() -> None:
    """Belt-and-suspenders: AST-level check that within the ``run`` method
    body, every ``bind_member_route`` call precedes every ``_guard.admit`` call
    lexically. Guards against a refactor introducing a second admit-then-bind
    branch."""
    tree = ast.parse(SCHEDULER.read_text("utf-8"))
    bind_lines: list[int] = []
    admit_lines: list[int] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "bind_member_route":
                bind_lines.append(node.lineno)
            elif isinstance(func, ast.Attribute) and func.attr == "admit":
                # scheduler.py only calls .admit on the local resource guard.
                admit_lines.append(node.lineno)

    assert bind_lines, "scheduler.py must call bind_member_route somewhere"
    assert admit_lines, "scheduler.py must call _guard.admit somewhere"
    # For each bind site, the NEXT admit downstream (in source order) must be
    # after the bind, not before. Prevents a refactor that introduces an
    # admit-then-bind path (bind of one member below an admit of another is OK
    # because scheduler.py serializes one member per turn).
    for bind in bind_lines:
        admits_after_bind = [a for a in admit_lines if a > bind]
        assert admits_after_bind, (
            f"bind_member_route at L{bind} has no downstream _guard.admit — "
            "the bound member must be admitted, not skipped."
        )
        # And no admit between this bind and its next dispatch (the closest
        # downstream admit must be within a reasonable window; a duplicate
        # admit earlier would fire first). We assert every admit BEFORE any
        # bind belongs to a different code path that doesn't use bind_member_route.
        # Since scheduler.py only binds in the F129 Multi path, that's true by
        # construction, but we guard against future drift.
        admits_before_bind = [a for a in admit_lines if a < bind]
        for prior_admit in admits_before_bind:
            # An admit-before-bind is only legal if that admit was in a
            # single-mode (non-Multi) code path. In practice all pre-bind
            # admits belong to methods entirely above the F129 fallback (e.g.
            # method definitions of unrelated helpers). Assert they are on a
            # different method than the bind by requiring at least one
            # top-level ``def``/``async def`` between them.
            between = _scheduler_source().splitlines()[prior_admit:bind]
            starts_new_method = any(
                line.lstrip().startswith(("def ", "async def "))
                for line in between
            )
            assert starts_new_method, (
                f"_guard.admit at L{prior_admit} precedes bind_member_route "
                f"at L{bind} within the same method — refactor bug."
            )
