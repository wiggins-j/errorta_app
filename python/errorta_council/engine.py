"""Council engine — single orchestrator constructing scheduler dependencies.

Phase 3 (Task 12): the engine now wires ContextRouter (F031-05) — and
its transform pipeline + manifest store — into the scheduler instead
of LocalContextBuilder. The router is the sole producer of
ContextManifests on disk; the scheduler stamps CONTEXT_BUILT events
with the resulting manifest_id so the /inspection endpoint can
project manifests back per turn (invariants 4, 5, 11).
"""
from __future__ import annotations

from typing import Any, Protocol

from errorta_council.context.aiar_retrieval_adapter import AiarRetrievalAdapter
from errorta_council.context.engine_adapter import RouterContextAdapter
from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.context.retrieval import RetrievalSeam
from errorta_council.context.router import ContextRouter
from errorta_council.context.tokens import TokenCalibrationStore
from errorta_council.context.transforms.pipeline import TransformPipeline
from errorta_council.context.transforms.redaction import (
    REDACTION_VERSION,
    RedactionPipeline,
)
from errorta_council.context.transforms.store import TransformStore
from errorta_council.context.transforms.summarization import SummaryPipeline
from errorta_council.control import RunControl
from errorta_council.gateway_local import LocalGateway
from errorta_council.limits import SchedulerPolicy, validate_runnable
from errorta_council.paths import council_root, token_calibration_path
from errorta_council.resources import LocalResourceGuard
from errorta_council.run_store import RunStore
from errorta_council.scheduler import TurnScheduler
from errorta_council.schema import EventType, RunMeta
from errorta_council.topologies.consensus import ConsensusDeliberationTopology
from errorta_council.topologies.round_robin import RoundRobinTopology


class _GatewayMeta(Protocol):
    async def is_reachable(self) -> bool: ...
    async def list_installed_models(self) -> list[str]: ...


def _build_snapshot_loader(
    *,
    run_store: RunStore,
    run_meta: RunMeta,
):
    """Closure returning the dict run-snapshot ContextRouter expects.

    The router calls this once per ``build()`` to resolve effective
    policy + visibility. Reading fresh on every call keeps the
    visibility view consistent with the just-appended transcript
    events (the scheduler appends MEMBER_MESSAGE before requesting
    the next member's context).
    """
    room = dict(run_meta.room_snapshot or {})

    def _loader(run_id: str) -> dict[str, Any]:
        try:
            _, events = run_store.read_run(run_id)
        except Exception:
            events = []
        event_dicts = [e.to_dict() for e in events]
        members: list[dict[str, Any]] = []
        for m in room.get("members", []):
            d = dict(m)
            # The router + visibility resolver key on ``member_id``; the
            # room snapshot persists ``id``. Normalize without losing
            # either field so downstream consumers can use whichever.
            d.setdefault("member_id", d.get("id"))
            d.setdefault("role", d.get("role") or "member")
            d.setdefault(
                "provider_class",
                "fake" if (d.get("provider") == "fake") else "local",
            )
            members.append(d)
        snapshot: dict[str, Any] = {
            "run_id": run_id,
            "events": event_dicts,
            "members": members,
            "room": {
                "id": room.get("id") or run_meta.room_id,
                "context_access_ceiling": room.get(
                    "context_access_ceiling", "full_context"
                ),
                "transcript_access_ceiling": room.get(
                    "transcript_access_ceiling", "all_messages"
                ),
                "allow_full_context": room.get("allow_full_context", True),
                "policy": room.get("policy", {}),
                "context_efficiency": dict(room.get("context_efficiency") or {}),
                "finalization_policy": dict(room.get("finalization_policy") or {}),
                "steward_policy": dict(room.get("steward_policy") or {}),
            },
            "topology": {
                "context_access_ceiling": room.get(
                    "context_access_ceiling", "full_context"
                ),
                "transcript_access_ceiling": room.get(
                    "transcript_access_ceiling", "all_messages"
                ),
            },
            "corpus_policy": dict(
                room.get("corpus_policy")
                or {"max_egress_class": "remote_eligible"}
            ),
        }
        # Residency / egress scope.
        #
        # When the room pins a residency (the F-INFRA-12 data-residency
        # lockdown or an explicit per-room override), honor it ROOM-WIDE —
        # every member's payload is built under that scope. But when the room
        # does NOT pin one, do NOT force a room-wide ``local`` default: that
        # flattened every member onto local egress and made remote/CLI members
        # (``anthropic.*`` / ``openai.*`` / ``codex_cli.*`` / ``claude_cli.*``)
        # fail closed with ``payload_route_mismatch`` in a mixed local+remote
        # room. Omitting the key lets the ContextRouter fall back to each
        # member's route-derived ``destination_scope`` (see
        # ``_default_destination_scope_for``), so a local member gets local
        # egress and a remote member gets remote egress in the SAME room.
        room_residency = room.get("residency")
        if room_residency:
            snapshot["residency"] = dict(room_residency)
        return snapshot

    return _loader


def _build_context_router(
    *,
    run_store: RunStore,
    run_meta: RunMeta,
    gateway: LocalGateway,
    summarizer_route_id: str = "local.summary",
) -> ContextRouter:
    """Build the Phase 3 ContextRouter wired to a real transform pipeline.

    ``summarizer_route_id`` defaults to ``local.summary`` — a placeholder
    that does not correspond to a real Ollama model. The SummaryPipeline
    falls back to extractive when the gateway raises FatalError
    (model_not_found), so the demo still terminates on any machine
    regardless of the user's installed models. Callers wanting an
    abstractive summary should thread an actually-installed Ollama model
    here (e.g. ``llama3.2:3b``) from the room or settings.
    """
    root = council_root()
    manifest_store = ContextManifestStore(root=root / "context-manifests")
    transform_store = TransformStore(root=root / "transforms")
    redaction = RedactionPipeline(version=REDACTION_VERSION)
    summary = SummaryPipeline(
        gateway=gateway,
        route_id=summarizer_route_id,
        allow_extractive_fallback=True,
    )
    transforms = TransformPipeline(
        redaction=redaction, summary=summary, store=transform_store,
    )
    # F031-RETRIEVAL: real retrieval through the F001-SEAM. The adapter
    # returns [] when AIAR is absent (StubPipeline) so demo + no-AIAR dev
    # paths both work without code change.
    retrieval = RetrievalSeam(pipeline=AiarRetrievalAdapter())
    loader = _build_snapshot_loader(run_store=run_store, run_meta=run_meta)
    return ContextRouter(
        retrieval=retrieval,
        transforms=transforms,
        manifest_store=manifest_store,
        run_snapshot_loader=loader,
        calibration_store=TokenCalibrationStore(token_calibration_path()),
    )


async def build_and_run(
    *,
    run_store: RunStore,
    run_meta: RunMeta,
    policy: SchedulerPolicy,
    gateway_meta: _GatewayMeta,
    hardware_scan_present: bool,
    ollama_base_url: str | None = None,
    gateway: LocalGateway | None = None,
    context_router: ContextRouter | None = None,
    tool_gateway: Any | None = None,
) -> RunMeta:
    """Build scheduler dependencies and drive the run to terminal.

    ``gateway`` and ``context_router`` are optional overrides for the
    sole Council egress (invariant 3) and the Phase 3 context router
    (F031-05). Tests use them to inject capture-the-payload fakes or
    custom retrieval. Production callers leave both ``None`` and
    accept the defaults.

    ``ollama_base_url`` previously defaulted to a hardcoded
    ``127.0.0.1:11434`` which bypassed the LocalGateway resolver. Now
    ``None`` (the default) lets ``LocalGateway()`` use its own
    resolution order (ERRORTA_OLLAMA_HOST env > errorta_ollama
    settings.host > localhost). Tests that need a specific Ollama host
    can still pass it explicitly.
    """
    validate_runnable(policy)
    control = RunControl(run_store=run_store, run_id=run_meta.id)
    if gateway is None:
        gateway = (
            LocalGateway(base_url=ollama_base_url)
            if ollama_base_url is not None
            else LocalGateway()
        )
    guard = LocalResourceGuard(
        gateway=gateway_meta,
        hardware_scan_present=hardware_scan_present,
    )
    if context_router is None:
        router = _build_context_router(
            run_store=run_store, run_meta=run_meta, gateway=gateway,
        )
    else:
        router = context_router
    adapter = RouterContextAdapter(
        router=router,
        room_id=str((run_meta.room_snapshot or {}).get("id") or run_meta.room_id),
    )
    # QA 2026-06-12: pick the topology from room.topology.kind. Default
    # round_robin keeps existing rooms unchanged. consensus_deliberation
    # enables the "round 1 blind, refine until consensus" flow.
    room = run_meta.room_snapshot or {}
    topology_raw = dict(room.get("topology") or {})
    kind = str(topology_raw.get("kind") or "round_robin")
    if kind == "consensus_deliberation":
        threshold_raw = topology_raw.get("consensus_threshold")
        threshold = int(threshold_raw) if isinstance(threshold_raw, int) and threshold_raw > 0 else None
        topology_impl = ConsensusDeliberationTopology(consensus_threshold=threshold)
    elif kind == "build_review":
        from errorta_council.topologies.build_review import BuildReviewTopology

        iters_raw = topology_raw.get("max_iterations")
        max_iters = int(iters_raw) if isinstance(iters_raw, int) and iters_raw > 0 else None
        topology_impl = BuildReviewTopology(max_iterations=max_iters)
    elif kind == "credibility":
        from errorta_council.topologies.credibility import CredibilityTopology

        topology_impl = CredibilityTopology()
    else:
        topology_impl = RoundRobinTopology()

    scheduler = TurnScheduler(
        run_store=run_store,
        run_meta=run_meta,
        topology=topology_impl,
        context_builder=adapter,
        resource_guard=guard,
        gateway=gateway,
        tool_gateway=tool_gateway,
        control=control,
        policy=policy,
    )
    return await scheduler.run()


__all__ = [
    "build_and_run",
    "EventType",
    "_build_snapshot_loader",
    "_build_context_router",
]
