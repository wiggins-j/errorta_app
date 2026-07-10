"""F031-01 readiness validation (invariant 4 — fail closed).

Two levels:
1. **Schema validation** — duplicate ids, unknown enum values, dangling
   refs, malformed shape.
2. **Readiness validation** — every enabled member route is known to the
   gateway; budget can plausibly fund the topology; policy conflicts
   surface before a future run engine can schedule turns.

Errors block ``ready``; warnings do not but are surfaced. The derived
status follows the F031-01 vocabulary:
``draft | ready | needs_provider | blocked_by_policy | invalid``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .gateway_meta import GatewayMeta
from .schema import (
    CREDIBILITY_FALLBACKS,
    CREDIBILITY_STRICTNESS,
    CouncilRoom,
    CredibilityPolicy,
    StewardPolicy,
)
from .steward.policy import resolve_steward_policy

MODEL_MODES = frozenset({"single", "multi"})
PM_MODEL_MODES = frozenset({"single"})

_ALLOWED_TOPOLOGY_KINDS: frozenset[str] = frozenset({
    "parallel_answers", "round_robin", "free_council",
    "debate", "blind_review", "relay", "moderator_led",
    # QA 2026-06-12: ships with consensus_deliberation
    "consensus_deliberation",
    # F078 Credibility mode.
    "credibility",
    # F087 Coding Team build-review topology, set programmatically rather than
    # offered in the general room editor.
    "build_review",
})

_ALLOWED_CONTEXT_ACCESS: frozenset[str] = frozenset({
    "none", "prompt_only", "transcript_only", "summary_only",
    "redacted_summary", "retrieved_snippets", "redacted_snippets",
    "answer_context", "full_context",
})

_ALLOWED_TRANSCRIPT_ACCESS: frozenset[str] = frozenset({
    # Phase 1 vocabulary.
    "none", "own_messages", "local_member_messages", "all_messages",
    "summary_only",
    # F031-06 Phase 3 expanded vocabulary (kept aligned with
    # errorta_council/context/visibility.py _ACCESS_ORDER). Rooms
    # using these values must validate or the resolver never sees them.
    "user_only", "own_and_user", "previous_speaker", "role_scoped",
    "redacted_summary",
})

_ALLOWED_FINALIZATION_MODES: frozenset[str] = frozenset({
    "transcript_only", "summary", "single_finalizer",
    "vote_summary", "consensus_report", "judged_final_answer",
    # F078 Credibility mode.
    "credibility_report",
})

# F111: the subset of the ALLOWED sets the engine/scheduler ACTUALLY executes.
# A kind/mode in _ALLOWED but NOT here is "known but not implemented yet": it used
# to be accepted and then silently fall back (topology -> round_robin in
# engine.py; finalization -> transcript_only in scheduler.py), which made the room
# editor offer options that don't do what they say. Validation now rejects those
# so a saved room can never claim a behavior it won't get. The
# test_implemented_options_match_engine canary asserts these match the real
# dispatch in engine.py / scheduler.py, so the UI/validation can't drift ahead of
# the engine.
#
# Executed topologies: engine.py dispatches round_robin / consensus_deliberation /
#   credibility / build_review; everything else falls back to RoundRobinTopology.
#   (build_review is set programmatically for the Coding Team, not offered in the
#   room editor, but it IS executed — so it's valid.)
IMPLEMENTED_TOPOLOGY_KINDS: frozenset[str] = frozenset({
    "round_robin", "consensus_deliberation", "credibility", "build_review",
})
# Executed finalization modes: transcript_only (baseline), single_finalizer
#   (finalizer member writes the answer-of-record), consensus_report (synthesizer
#   turn on consensus), summary (F031-28: abstractive synthesizer turn on ANY
#   terminal reason, preserving disagreement), credibility_report (credibility
#   synthesis). vote_summary / judged_final_answer still have NO executed path ->
#   they behave like transcript_only, so they are not implemented.
IMPLEMENTED_FINALIZATION_MODES: frozenset[str] = frozenset({
    "transcript_only", "single_finalizer", "consensus_report", "summary",
    "credibility_report",
})

# Status-bucket sets. The room ``status`` is derived by testing whether the
# set of emitted error codes is a subset of one of these. Codes not listed
# here fall through to ``invalid``.
#
# COORDINATION (F037/F038): both features extend these sets. Add your codes
# here; do not reintroduce inline membership checks in ``validate_room``.
NEEDS_PROVIDER_CODES: frozenset[str] = frozenset({
    "unknown_gateway_route",
    "unknown_escalation_route",
    "steward_external_unknown_route",
})
BLOCKED_BY_POLICY_CODES: frozenset[str] = frozenset({
    "full_context_not_allowed",
    "remote_member_zero_budget",
    "remote_callout_zero_budget",
    "callout_full_context_not_allowed",
    "steward_remote_not_allowed",
    "steward_remote_zero_budget",
    # F078 Credibility mode — configured but blocked until tools/consent are on.
    "credibility_requires_web_search",
    "credibility_requires_web_fetch",
    "credibility_downgrade_requires_consent",
})


@dataclass(frozen=True)
class RoomValidationResult:
    room_id: str
    status: str   # draft | ready | needs_provider | blocked_by_policy | invalid
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    capabilities: dict[str, Any] = field(default_factory=dict)
    derived: dict[str, Any] = field(default_factory=dict)


def _err(code: str, path: str, message: str) -> dict[str, Any]:
    return {"code": code, "path": path, "message": message, "severity": "error"}


def _warn(code: str, path: str, message: str) -> dict[str, Any]:
    return {"code": code, "path": path, "message": message, "severity": "warning"}


def validate_room(room: CouncilRoom, gateway_meta: GatewayMeta) -> RoomValidationResult:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    # --- members ------------------------------------------------------------
    seen_ids: set[str] = set()
    enabled = [m for m in room.members if m.enabled]
    for idx, m in enumerate(room.members):
        path = f"$.members[{idx}]"
        if m.id in seen_ids:
            errors.append(_err("duplicate_member_id", f"{path}.id",
                               f"member id {m.id!r} is not unique"))
        seen_ids.add(m.id)
        if m.context_access not in _ALLOWED_CONTEXT_ACCESS:
            errors.append(_err("unknown_context_access", f"{path}.context_access",
                               f"unknown context_access {m.context_access!r}"))
        if m.transcript_access not in _ALLOWED_TRANSCRIPT_ACCESS:
            errors.append(_err("unknown_transcript_access", f"{path}.transcript_access",
                               f"unknown transcript_access {m.transcript_access!r}"))
        mode = str(getattr(m, "model_mode", "single") or "single")
        if mode not in MODEL_MODES:
            errors.append(_err("unknown_model_mode", f"{path}.model_mode",
                               f"unknown model_mode {mode!r}"))
        elif mode == "single":
            if m.enabled and not m.gateway_route_id:
                errors.append(_err("missing_gateway_route", f"{path}.gateway_route_id",
                                   "enabled Single member has no gateway_route_id"))
        else:
            pool = list(getattr(m, "model_pool", []) or [])
            coding_role = str((m.metadata or {}).get("coding_role") or m.role or "").lower()
            if coding_role == "pm" and mode not in PM_MODEL_MODES:
                errors.append(_err("pm_model_mode_multi", f"{path}.model_mode",
                                   "PM members must use Model: Single"))
            if m.enabled and not pool:
                errors.append(_err("empty_model_pool", f"{path}.model_pool",
                                   "enabled Multi member has an empty model_pool"))
            if len(pool) != len(set(pool)):
                errors.append(_err("duplicate_model_pool_route", f"{path}.model_pool",
                                   "model_pool contains duplicate route ids"))
            for pool_idx, route_id in enumerate(pool):
                if not isinstance(route_id, str) or not route_id.strip():
                    errors.append(_err(
                        "invalid_model_pool_route",
                        f"{path}.model_pool[{pool_idx}]",
                        "model_pool entries must be non-empty route ids",
                    ))

    # --- topology -----------------------------------------------------------
    if room.topology.kind not in _ALLOWED_TOPOLOGY_KINDS:
        errors.append(_err("unknown_topology_kind", "$.topology.kind",
                           f"unknown topology kind {room.topology.kind!r}"))
    elif room.topology.kind not in IMPLEMENTED_TOPOLOGY_KINDS:
        # F111: known but not executed — would silently fall back to round_robin.
        errors.append(_err(
            "topology_kind_unimplemented", "$.topology.kind",
            f"topology kind {room.topology.kind!r} is not implemented yet "
            f"(would silently run as round_robin); use one of "
            f"{sorted(IMPLEMENTED_TOPOLOGY_KINDS - {'build_review'})}",
        ))

    # F031-09 §Runnable config: caps must be present and positive. Don't
    # accept None and don't silently default at run-creation.
    if room.topology.max_rounds is None or room.topology.max_rounds < 1:
        errors.append(_err(
            "missing_topology_max_rounds", "$.topology.max_rounds",
            "topology.max_rounds must be a positive int (F031-09)",
        ))
    if room.topology.max_messages_per_member is None or room.topology.max_messages_per_member < 1:
        errors.append(_err(
            "missing_topology_max_messages_per_member",
            "$.topology.max_messages_per_member",
            "topology.max_messages_per_member must be a positive int (F031-09)",
        ))

    # F031-03 §MVP local-only required exactly 2 enabled members. F034
    # (2026-06-12) relaxes this to 2-8 so multi-provider Council rooms
    # (e.g. Claude + ChatGPT + Gemini + example-host local) can validate.
    # The scheduler/topology already handle N members; the runner was
    # never actually 2-only — this was a gate on the F031 Phase 1
    # acceptance test scope.
    if enabled and not (2 <= len(enabled) <= 8):
        errors.append(_err(
            "member_count_out_of_range", "$.members",
            f"Council rooms require 2-8 enabled members; got {len(enabled)}",
        ))

    # --- context policy: full_context guard --------------------------------
    if not room.context_policy.allow_full_context:
        for idx, m in enumerate(room.members):
            if m.context_access == "full_context":
                errors.append(_err(
                    "full_context_not_allowed", f"$.members[{idx}].context_access",
                    "member uses full_context but context_policy.allow_full_context is false",
                ))

    # --- finalization refs --------------------------------------------------
    if room.finalization_policy.finalizer_member_id is not None:
        if room.finalization_policy.finalizer_member_id not in seen_ids:
            errors.append(_err(
                "dangling_finalizer_member", "$.finalization_policy.finalizer_member_id",
                "finalizer_member_id does not match any member id",
            ))
    for jdx, jid in enumerate(room.finalization_policy.judge_member_ids):
        if jid not in seen_ids:
            errors.append(_err(
                "dangling_judge_member",
                f"$.finalization_policy.judge_member_ids[{jdx}]",
                "judge_member_id does not match any member id",
            ))
    if room.finalization_policy.mode not in _ALLOWED_FINALIZATION_MODES:
        errors.append(_err("unknown_finalization_mode",
                           "$.finalization_policy.mode",
                           f"unknown finalization mode {room.finalization_policy.mode!r}"))
    elif room.finalization_policy.mode not in IMPLEMENTED_FINALIZATION_MODES:
        # F111: known but not executed — would silently behave like transcript_only.
        errors.append(_err(
            "finalization_mode_unimplemented", "$.finalization_policy.mode",
            f"finalization mode {room.finalization_policy.mode!r} is not "
            f"implemented yet (would silently run as transcript_only); use one of "
            f"{sorted(IMPLEMENTED_FINALIZATION_MODES)}",
        ))

    # --- F080 neutral leader-judge -----------------------------------------
    jpol = room.judge_policy
    if jpol.enabled:
        if jpol.judge_member_id is not None and jpol.judge_member_id not in seen_ids:
            errors.append(_err(
                "dangling_judge_member", "$.judge_policy.judge_member_id",
                "judge_member_id does not match any member id",
            ))
        if jpol.judge_member_id is None and not any(
            str(getattr(m, "role", "")) == "judge" for m in room.members
        ):
            errors.append(_err(
                "judge_policy_no_judge", "$.judge_policy.judge_member_id",
                "judge_policy is enabled but no judge member is set "
                "(set judge_member_id or give a member role 'judge')",
            ))
        if jpol.start_round < 1:
            errors.append(_err(
                "judge_start_round_invalid", "$.judge_policy.start_round",
                "start_round must be >= 1",
            ))
        # Resolve the effective judge id (explicit, else a role=="judge" member).
        resolved_judge = jpol.judge_member_id or next(
            (m.id for m in room.members if str(getattr(m, "role", "")) == "judge"),
            None,
        )
        if resolved_judge is not None:
            # The judge is excluded from the speaker order, so it can't also be
            # the finalizer (that pick would be silently ignored at runtime).
            if room.finalization_policy.finalizer_member_id == resolved_judge:
                errors.append(_err(
                    "judge_is_finalizer", "$.judge_policy.judge_member_id",
                    "the judge member cannot also be the finalizer "
                    "(the judge never writes a member answer)",
                ))
            # A judge needs at least one OTHER enabled member to watch.
            others = [
                m for m in room.members
                if getattr(m, "enabled", True) and m.id != resolved_judge
            ]
            if not others:
                errors.append(_err(
                    "judge_has_no_members", "$.judge_policy.judge_member_id",
                    "the judge has no other enabled members to watch",
                ))

    # --- gateway readiness --------------------------------------------------
    unknown_routes: list[str] = []
    remote_members: list[int] = []
    for idx, m in enumerate(room.members):
        if not m.enabled:
            continue
        mode = str(getattr(m, "model_mode", "single") or "single")
        routes = (
            list(getattr(m, "model_pool", []) or [])
            if mode == "multi"
            else ([m.gateway_route_id] if m.gateway_route_id else [])
        )
        member_has_remote = False
        for route_idx, route_id in enumerate(routes):
            route = gateway_meta.get_route(route_id)
            path = (
                f"$.members[{idx}].model_pool[{route_idx}]"
                if mode == "multi"
                else f"$.members[{idx}].gateway_route_id"
            )
            if route is None:
                unknown_routes.append(route_id)
                errors.append(_err("unknown_gateway_route", path,
                                   f"gateway route {route_id!r} is not known"))
                continue
            member_has_remote = member_has_remote or route.get("kind") == "remote"
        if member_has_remote:
            remote_members.append(idx)

    # --- budget plausibility -----------------------------------------------
    if (
        room.budget_policy.max_total_model_calls is not None
        and room.budget_policy.max_total_model_calls < len(enabled)
    ):
        errors.append(_err(
            "impossible_budget", "$.budget_policy.max_total_model_calls",
            "max_total_model_calls is below the number of enabled members",
        ))

    # --- remote-member + zero-budget = blocked_by_policy -------------------
    blocked_remote = (
        remote_members
        and (room.budget_policy.max_remote_calls_per_run == 0)
    )
    if blocked_remote:
        for idx in remote_members:
            errors.append(_err(
                "remote_member_zero_budget",
                f"$.members[{idx}].gateway_route_id",
                "remote member configured but max_remote_calls_per_run is 0",
            ))

    # --- catalog drift warning ---------------------------------------------
    for idx, m in enumerate(room.members):
        if (
            m.catalog_version
            and gateway_meta.catalog_version
            and m.catalog_version != gateway_meta.catalog_version
        ):
            warnings.append(_warn(
                "provider_catalog_stale",
                f"$.members[{idx}].catalog_version",
                "member catalog snapshot differs from current gateway catalog",
            ))

    # --- derive status -----------------------------------------------------
    capabilities = {
        "has_remote_members": bool(remote_members),
        "has_local_members": any(
            (gateway_meta.get_route(m.gateway_route_id or "") or {}).get("kind") == "local"
            for m in enabled
        ),
        "requires_f030": bool(enabled),
        "requires_context_router": True,
        "requires_run_store": True,
    }
    derived = {
        "enabled_member_count": len(enabled),
        "remote_member_count": len(remote_members),
    }

    # --- F037 expert callouts ----------------------------------------------
    _validate_escalation(room, gateway_meta, errors, warnings, capabilities, derived)
    # --- F038 Council Steward ----------------------------------------------
    _validate_steward(room, gateway_meta, errors, warnings, capabilities, derived)
    # --- F078 Credibility mode ---------------------------------------------
    _validate_credibility(room, errors, warnings, capabilities, derived)

    codes = {e["code"] for e in errors}
    if not errors and not enabled:
        status = "draft"
    elif not errors:
        status = "ready"
    elif codes <= NEEDS_PROVIDER_CODES:
        status = "needs_provider"
    elif codes <= BLOCKED_BY_POLICY_CODES:
        status = "blocked_by_policy"
    else:
        status = "invalid"

    return RoomValidationResult(
        room_id=room.id, status=status, errors=errors, warnings=warnings,
        capabilities=capabilities, derived=derived,
    )


_ALLOWED_APPROVAL_MODES: frozenset[str] = frozenset({
    "auto", "ask_user", "moderator", "disabled",
})

_ALLOWED_STEWARD_ASSIGNMENT_MODES: frozenset[str] = frozenset({
    "member", "external",
})
_ALLOWED_STEWARD_PACKET_MODES: frozenset[str] = frozenset({
    "shared", "per_member", "hybrid",
})
_ALLOWED_STEWARD_CADENCES: frozenset[str] = frozenset({
    "after_each_message", "after_each_round", "before_each_turn",
    "before_finalization", "on_demand",
})
_ALLOWED_STEWARD_FALLBACKS: frozenset[str] = frozenset({
    "full_transcript", "stop",
})
_MIN_STEWARD_PACKET_TOKENS = 128


def _validate_steward(
    room: CouncilRoom,
    gateway_meta: GatewayMeta,
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    capabilities: dict[str, Any],
    derived: dict[str, Any],
) -> None:
    """F038 Steward policy validation.

    Disabled policies do not block runs. Once enabled, shape, assignment,
    provider readiness, remote egress opt-in, and steward-specific budget caps
    are fail-closed.
    """
    pol = resolve_steward_policy(room)
    assignment = pol.assignment
    route = None
    steward_remote = False

    if assignment.gateway_route_id:
        route = gateway_meta.get_route(assignment.gateway_route_id)
        steward_remote = bool(route and route.get("kind") == "remote")

    capabilities["has_steward"] = bool(pol.enabled)
    capabilities["steward_mode"] = assignment.mode
    capabilities["steward_remote"] = steward_remote
    capabilities["steward_packet_mode"] = pol.packet_mode
    capabilities["steward_requires_extra_model_calls"] = bool(
        pol.enabled and assignment.mode == "external"
    )
    derived["steward_recent_full_messages"] = pol.recent_full_messages
    derived["steward_max_packet_tokens"] = pol.max_packet_tokens

    if not pol.enabled:
        if pol != StewardPolicy():
            warnings.append(_warn(
                "steward_disabled_with_config", "$.steward_policy.enabled",
                "steward config is present but steward_policy.enabled is false",
            ))
        return

    if assignment.mode not in _ALLOWED_STEWARD_ASSIGNMENT_MODES:
        errors.append(_err(
            "unknown_steward_assignment_mode",
            "$.steward_policy.assignment.mode",
            f"unknown steward assignment mode {assignment.mode!r}",
        ))
    elif assignment.mode in {"external", "member"}:
        # Only deterministic (extractive) packets are wired at runtime today.
        # External/member modes that would call a steward MODEL route are
        # config-accepted but do not yet issue model calls, so the reserved
        # steward budget headroom is not actually consumed. Warn so the
        # operator is not misled about cost.
        warnings.append(_warn(
            "steward_model_route_unimplemented",
            "$.steward_policy.assignment.mode",
            f"steward assignment mode {assignment.mode!r} currently produces "
            "deterministic packets only; no steward model call is issued yet",
        ))

    if pol.packet_mode not in _ALLOWED_STEWARD_PACKET_MODES:
        errors.append(_err(
            "steward_packet_mode_unknown",
            "$.steward_policy.packet_mode",
            f"unknown steward packet_mode {pol.packet_mode!r}",
        ))

    if pol.cadence not in _ALLOWED_STEWARD_CADENCES:
        errors.append(_err(
            "steward_cadence_unknown",
            "$.steward_policy.cadence",
            f"unknown steward cadence {pol.cadence!r}",
        ))

    if pol.fallback_on_failure not in _ALLOWED_STEWARD_FALLBACKS:
        errors.append(_err(
            "steward_fallback_mode_unknown",
            "$.steward_policy.fallback_on_failure",
            f"unknown steward fallback_on_failure {pol.fallback_on_failure!r}",
        ))

    if pol.recent_full_messages < 0:
        errors.append(_err(
            "steward_recent_full_messages_negative",
            "$.steward_policy.recent_full_messages",
            "recent_full_messages must be zero or greater",
        ))

    if pol.max_packet_tokens < _MIN_STEWARD_PACKET_TOKENS:
        errors.append(_err(
            "steward_max_packet_tokens_too_low",
            "$.steward_policy.max_packet_tokens",
            f"max_packet_tokens must be at least {_MIN_STEWARD_PACKET_TOKENS}",
        ))

    if assignment.mode == "member":
        members = {m.id: m for m in room.members}
        member_id = assignment.member_id
        if member_id is None or member_id not in members:
            errors.append(_err(
                "steward_member_unknown",
                "$.steward_policy.assignment.member_id",
                "steward member_id does not match any room member",
            ))
        elif not members[member_id].enabled:
            errors.append(_err(
                "steward_member_disabled",
                "$.steward_policy.assignment.member_id",
                "steward member_id points at a disabled member",
            ))
        return

    if assignment.mode != "external":
        return

    if not assignment.gateway_route_id:
        errors.append(_err(
            "steward_external_missing_route",
            "$.steward_policy.assignment.gateway_route_id",
            "external steward has no gateway_route_id",
        ))
        return

    if route is None:
        errors.append(_err(
            "steward_external_unknown_route",
            "$.steward_policy.assignment.gateway_route_id",
            f"gateway route {assignment.gateway_route_id!r} is not known",
        ))
        return

    if route.get("kind") != "remote":
        return

    if not pol.remote_steward_allowed:
        errors.append(_err(
            "steward_remote_not_allowed",
            "$.steward_policy.remote_steward_allowed",
            "external steward route is remote but remote_steward_allowed is false",
        ))

    if (
        room.budget_policy.max_remote_calls_per_run == 0
        or room.budget_policy.max_remote_steward_calls_per_run == 0
    ):
        errors.append(_err(
            "steward_remote_zero_budget",
            "$.budget_policy.max_remote_steward_calls_per_run",
            "remote steward configured but remote steward budget is 0",
        ))


def _validate_escalation(
    room: CouncilRoom,
    gateway_meta: GatewayMeta,
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    capabilities: dict[str, Any],
    derived: dict[str, Any],
) -> None:
    """F037 escalation-roster + escalation-policy readiness (fail closed).

    Shape checks (duplicate ids, unknown access, route readiness, unsafe
    full context) run whenever a roster is configured. Policy/budget gating
    runs only when ``escalation_policy.enabled`` is true; a configured roster
    on a disabled policy is a warning, not an error.
    """
    pol = room.escalation_policy
    roster = room.escalation_roster

    remote_targets: list[int] = []
    seen: set[str] = set()
    for idx, t in enumerate(roster):
        path = f"$.escalation_roster[{idx}]"
        if t.id in seen:
            errors.append(_err("duplicate_escalation_target_id", f"{path}.id",
                               f"escalation target id {t.id!r} is not unique"))
        seen.add(t.id)
        if t.context_access not in _ALLOWED_CONTEXT_ACCESS:
            errors.append(_err("callout_context_access_unknown",
                               f"{path}.context_access",
                               f"unknown context_access {t.context_access!r}"))
        if t.transcript_access not in _ALLOWED_TRANSCRIPT_ACCESS:
            errors.append(_err("callout_transcript_access_unknown",
                               f"{path}.transcript_access",
                               f"unknown transcript_access {t.transcript_access!r}"))
        if not t.gateway_route_id:
            errors.append(_err("missing_escalation_gateway_route",
                               f"{path}.gateway_route_id",
                               "escalation target has no gateway_route_id"))
        else:
            route = gateway_meta.get_route(t.gateway_route_id)
            if route is None:
                errors.append(_err("unknown_escalation_route",
                                   f"{path}.gateway_route_id",
                                   f"gateway route {t.gateway_route_id!r} is not known"))
            elif route.get("kind") == "remote":
                remote_targets.append(idx)
        if t.context_access == "full_context" and not room.context_policy.allow_full_context:
            errors.append(_err("callout_full_context_not_allowed",
                               f"{path}.context_access",
                               "escalation target uses full_context but "
                               "context_policy.allow_full_context is false"))

    capabilities["has_callout_roster"] = bool(roster)
    capabilities["has_remote_callout_targets"] = bool(remote_targets)
    capabilities["requires_callout_approval"] = bool(
        pol.enabled and pol.approval_mode in {"ask_user", "moderator"}
    )
    derived["callout_target_count"] = len(roster)
    derived["remote_callout_target_count"] = len(remote_targets)

    if not pol.enabled:
        if roster:
            warnings.append(_warn(
                "escalation_disabled_with_roster", "$.escalation_policy.enabled",
                "escalation roster configured but escalation_policy.enabled is false",
            ))
        return

    if pol.approval_mode not in _ALLOWED_APPROVAL_MODES:
        errors.append(_err("callout_approval_mode_unknown",
                           "$.escalation_policy.approval_mode",
                           f"unknown approval_mode {pol.approval_mode!r}"))
    elif pol.approval_mode == "moderator":
        # The moderator-led topology (F031-23) is not implemented yet; at
        # runtime "moderator" approval is handled identically to "ask_user"
        # (the user approves). Warn so the operator is not misled.
        warnings.append(_warn(
            "callout_approval_moderator_unimplemented",
            "$.escalation_policy.approval_mode",
            "approval_mode 'moderator' currently behaves like 'ask_user' "
            "(moderator topology not implemented)",
        ))

    # Requester modes beyond user_only/any_member have no runtime enqueue path
    # yet (member-requested callouts are a later slice). Warn rather than fail
    # so rooms stay loadable; the manual user path still works.
    if pol.requester_mode in {"member_allowlist", "role_allowlist", "quorum",
                              "topology", "steward"}:
        warnings.append(_warn(
            "callout_requester_mode_unimplemented",
            "$.escalation_policy.requester_mode",
            f"requester_mode {pol.requester_mode!r} is config-accepted but has "
            "no runtime trigger yet; only manual user callouts execute",
        ))

    member_ids = {m.id for m in room.members}
    if pol.requester_mode == "member_allowlist":
        for j, mid in enumerate(pol.requester_member_ids):
            if mid not in member_ids:
                errors.append(_err("callout_requester_member_unknown",
                                   f"$.escalation_policy.requester_member_ids[{j}]",
                                   f"requester member id {mid!r} matches no member"))
    if pol.requester_mode == "role_allowlist" and not pol.requester_roles:
        errors.append(_err("callout_requester_role_empty",
                           "$.escalation_policy.requester_roles",
                           "role_allowlist requester_mode requires at least one role"))

    auto_triggers = bool(pol.allow_topology_triggers or pol.auto_after_no_consensus_rounds)
    if auto_triggers and not roster:
        errors.append(_err("callout_auto_trigger_without_roster",
                           "$.escalation_policy",
                           "automatic callout triggers configured but roster is empty"))

    no_remote_budget = (
        room.budget_policy.max_remote_calls_per_run == 0
        or pol.max_remote_callouts_per_run == 0
    )
    if remote_targets and no_remote_budget:
        for idx in remote_targets:
            errors.append(_err("remote_callout_zero_budget",
                               f"$.escalation_roster[{idx}].gateway_route_id",
                               "remote escalation target configured but remote "
                               "callout budget is 0"))

    # Total model-call budget must leave headroom for callouts on top of the
    # ordinary member-turn floor. Mirrors CouncilRoomEditor.computeBudgetFloor.
    enabled_count = len([m for m in room.members if m.enabled])
    max_rounds = room.topology.max_rounds or 1
    needed = enabled_count * max_rounds + pol.max_callouts_per_run
    if (
        room.budget_policy.max_total_model_calls is not None
        and room.budget_policy.max_total_model_calls < needed
    ):
        errors.append(_err("callout_total_budget_impossible",
                           "$.budget_policy.max_total_model_calls",
                           "max_total_model_calls leaves no headroom for the "
                           "configured callouts"))


def _validate_credibility(
    room: CouncilRoom,
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    capabilities: dict[str, Any],
    derived: dict[str, Any],
) -> None:
    """F078 Credibility-mode validation (invariant 4 — fail closed).

    Disabled policies do not block runs. Once Credibility mode is signalled
    (policy enabled, ``topology.kind == "credibility"``, or finalization mode
    ``credibility_report``), the topology / finalization / tool / budget /
    member / leader shape is fail-closed.
    """
    pol = room.credibility_policy
    is_topo = room.topology.kind == "credibility"
    is_final = room.finalization_policy.mode == "credibility_report"
    signalled = pol.enabled or is_topo or is_final

    capabilities["has_credibility"] = bool(pol.enabled)
    capabilities["credibility_strictness"] = pol.strictness

    if not signalled:
        # Fully off. A stray non-default policy with no topology is a no-op;
        # surface it as a warning so the user isn't surprised it does nothing.
        if pol != CredibilityPolicy():
            warnings.append(_warn(
                "credibility_disabled_with_config", "$.credibility_policy.enabled",
                "credibility_policy carries non-default config but the room is "
                "not in Credibility mode (set topology.kind=credibility)",
            ))
        return

    # Coherence: the three signals must agree.
    if not is_topo:
        errors.append(_err(
            "credibility_requires_topology", "$.topology.kind",
            "Credibility mode requires topology.kind == 'credibility'",
        ))
    if is_topo and not is_final:
        errors.append(_err(
            "credibility_finalization_mode_mismatch", "$.finalization_policy.mode",
            "Credibility topology requires finalization mode 'credibility_report'",
        ))

    if pol.strictness not in CREDIBILITY_STRICTNESS:
        errors.append(_err(
            "credibility_strictness_unknown", "$.credibility_policy.strictness",
            f"unknown strictness {pol.strictness!r}",
        ))

    # Required internet tools (F039). Fail closed so a Credibility room cannot
    # run as a knowledge-only room dressed up with citations.
    if pol.require_search and not room.tool_policy.web_search.enabled:
        errors.append(_err(
            "credibility_requires_web_search", "$.tool_policy.web_search.enabled",
            "Credibility mode requires tool_policy.web_search.enabled",
        ))
    if pol.require_fetch and not room.tool_policy.web_fetch.enabled:
        errors.append(_err(
            "credibility_requires_web_fetch", "$.tool_policy.web_fetch.enabled",
            "Credibility mode requires tool_policy.web_fetch.enabled",
        ))

    # Research budgets must be able to fund at least one search/fetch.
    if pol.require_search and pol.max_searches_per_member < 1:
        errors.append(_err(
            "credibility_search_budget_too_low",
            "$.credibility_policy.max_searches_per_member",
            "max_searches_per_member must be >= 1 when require_search is true",
        ))
    if pol.require_fetch and pol.max_fetches_per_member < 1:
        errors.append(_err(
            "credibility_fetch_budget_too_low",
            "$.credibility_policy.max_fetches_per_member",
            "max_fetches_per_member must be >= 1 when require_fetch is true",
        ))

    # Need at least two members so a claim can be reviewed by a non-author.
    enabled_members = [m for m in room.members if m.enabled]
    if len(enabled_members) < 2:
        errors.append(_err(
            "credibility_member_count_too_low", "$.members",
            "Credibility mode needs >= 2 enabled members (author + reviewer)",
        ))

    member_ids = {m.id for m in room.members}
    if pol.leader_member_id is not None and pol.leader_member_id not in member_ids:
        errors.append(_err(
            "credibility_unknown_leader_member", "$.credibility_policy.leader_member_id",
            f"leader_member_id {pol.leader_member_id!r} names no member",
        ))

    if pol.max_repair_passes < 0:
        errors.append(_err(
            "credibility_repair_passes_negative",
            "$.credibility_policy.max_repair_passes",
            "max_repair_passes must be >= 0",
        ))

    # An impossible source policy: fetch required but zero sources demanded, or
    # primary required while every concrete source class is disallowed.
    if pol.require_fetch and pol.min_fetched_sources_per_member < 1:
        errors.append(_err(
            "credibility_source_policy_impossible",
            "$.credibility_policy.min_fetched_sources_per_member",
            "require_fetch with min_fetched_sources_per_member < 1 is contradictory",
        ))

    if pol.fallback_on_tool_failure not in CREDIBILITY_FALLBACKS:
        # Reuse the source-policy code rather than invent an unlisted one.
        errors.append(_err(
            "credibility_source_policy_impossible",
            "$.credibility_policy.fallback_on_tool_failure",
            f"unknown fallback_on_tool_failure {pol.fallback_on_tool_failure!r}",
        ))
    elif pol.fallback_on_tool_failure == "downgrade_to_normal" and not pol.allow_downgrade_consent:
        errors.append(_err(
            "credibility_downgrade_requires_consent",
            "$.credibility_policy.allow_downgrade_consent",
            "fallback 'downgrade_to_normal' requires allow_downgrade_consent=true",
        ))

    derived["credibility_max_cycles"] = pol.max_credibility_cycles
    derived["credibility_max_repair_passes"] = pol.max_repair_passes
