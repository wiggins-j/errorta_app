"""Council-scoped test fixtures.

Reuses the top-level ``tmp_errorta_home`` fixture (set in
``python/tests/conftest.py``) for residency isolation. Adds Council-only
sample factories.
"""
from __future__ import annotations

from typing import Any

import pytest

from errorta_council.schema import (
    FORMAT_VERSION,
    BudgetPolicy,
    ContextPolicy,
    CouncilMember,
    CouncilRoom,
    FinalizationPolicy,
    TopologyPolicy,
)


def _now() -> str:
    return "2026-06-11T00:00:00Z"


def _member(mid: str, *, enabled: bool = True, role: str = "answerer",
            gateway_route_id: str | None = "fake.local.deterministic",
            provider_kind: str = "local") -> CouncilMember:
    return CouncilMember(
        id=mid, name=f"Member {mid}", role=role, enabled=enabled,
        gateway_route_id=gateway_route_id, provider_kind=provider_kind,
        provider_display="Fake", model_display="deterministic",
        catalog_version="2026-06-11",
        context_access="prompt_only", transcript_access="own_messages",
        turn_limits={"max_messages": 1, "max_input_tokens": 1024,
                     "max_output_tokens": 256, "max_context_tokens": 1024},
        generation={"temperature": 0.0, "top_p": None, "seed": None},
        system_prompt="Phase 0 fake.", metadata={},
    )


@pytest.fixture
def sample_room() -> CouncilRoom:
    return CouncilRoom(
        format_version=FORMAT_VERSION,
        id="room-1", name="Phase 0 Room", description="",
        members=[_member("m-1"), _member("m-2")],
        topology=TopologyPolicy(
            kind="round_robin", max_rounds=1, max_total_turns=2,
            max_messages_per_member=1, speaker_order=["m-1", "m-2"],
        ),
        context_policy=ContextPolicy(
            default_context_access="prompt_only",
            default_transcript_access="own_messages",
            allow_full_context=False,
            require_confirmation_for_remote_context=True,
            require_confirmation_for_full_context=True,
        ),
        budget_policy=BudgetPolicy(
            max_rounds=1, max_messages_per_member=1, max_total_model_calls=2,
            max_remote_calls_per_run=0, max_remote_calls_per_day=None,
            max_input_tokens_per_turn=1024, max_output_tokens_per_turn=256,
            max_context_tokens_per_member=1024,
            max_estimated_usd_per_run=0.0, max_estimated_usd_per_month=None,
        ),
        finalization_policy=FinalizationPolicy(mode="transcript_only"),
        created_at=_now(), updated_at=_now(), revision=1,
    )


@pytest.fixture
def member_factory() -> Any:
    return _member


# --- Phase 1 additions -------------------------------------------------------

from pathlib import Path


@pytest.fixture
def runs_dir_path(tmp_errorta_home: Path) -> Path:
    """Resolve the council runs directory inside the residency-isolated tmp home."""
    from errorta_council import paths as council_paths
    runs = council_paths.runs_dir()
    runs.mkdir(parents=True, exist_ok=True)
    return runs


@pytest.fixture
def rooms_dir_path(tmp_errorta_home: Path) -> Path:
    from errorta_council import paths as council_paths
    rooms = council_paths.rooms_dir()
    rooms.mkdir(parents=True, exist_ok=True)
    return rooms


@pytest.fixture(autouse=True)
def _drain_scheduler_threads():
    """Wait for any background Council scheduler threads to finish before cleanup."""
    yield
    try:
        from errorta_app.routes.council import drain_scheduler_threads
    except Exception:
        return
    drain_scheduler_threads(timeout=5.0)


@pytest.fixture
def seed_room_full(tmp_errorta_home: Path):
    """Build a complete Phase 0 CouncilRoom and persist it via RoomStore.

    Fix 5: route tests that previously POSTed a simplified `{name, members,
    topology, limits}` payload now seed a real Phase 0 room with all required
    fields, sidestepping the 422 that the route's `CouncilRoom.from_dict`
    would otherwise raise.

    Usage:
        room = seed_room_full(member_count=2, max_rounds=1)
        # room.id is now a valid CouncilRoom on disk.
    """
    from errorta_council import paths as council_paths
    from errorta_council.room_store import RoomStore
    from errorta_council.schema import (
        BudgetPolicy,
        ContextPolicy,
        CouncilMember,
        CouncilRoom,
        FORMAT_VERSION,
        FinalizationPolicy,
        TopologyPolicy,
    )

    NOW = "2026-06-11T00:00:00Z"

    def _member_p1(idx: int, *, provider: str, model: str, role: str, enabled: bool) -> CouncilMember:
        mid = f"m-{idx}"
        return CouncilMember(
            id=mid, name=f"Member {mid}", role=role, enabled=enabled,
            gateway_route_id=f"{provider}.local.{model}",
            provider_kind="local" if provider in ("local", "fake") else provider,
            provider_display="Fake" if provider == "fake" else "Ollama",
            model_display=model,
            catalog_version="2026-06-11",
            context_access="prompt_only",
            transcript_access="own_messages",
            turn_limits={"max_messages": 1, "max_input_tokens": 1024,
                         "max_output_tokens": 256, "max_context_tokens": 1024},
            generation={"temperature": 0.0, "top_p": None, "seed": None},
            system_prompt="Phase 1 seeded member.",
            metadata={},
        )

    def _factory(
        *,
        room_id: str = "rm-phase1",
        name: str = "Phase 1 Seeded Room",
        member_count: int = 2,
        provider: str = "fake",
        model: str = "stub-model",
        role: str = "answerer",
        enabled_ids: list[str] | None = None,
        max_rounds: int = 1,
        max_messages_per_member: int | None = 1,
        per_turn_timeout_seconds: int = 30,
    ) -> CouncilRoom:
        members = [
            _member_p1(
                i + 1,
                provider=provider,
                model=model,
                role=role,
                enabled=(enabled_ids is None or f"m-{i+1}" in enabled_ids),
            )
            for i in range(member_count)
        ]
        room = CouncilRoom(
            format_version=FORMAT_VERSION,
            id=room_id, name=name, description="",
            members=members,
            topology=TopologyPolicy(
                kind="round_robin",
                max_rounds=max_rounds,
                max_total_turns=max_rounds * member_count,
                max_messages_per_member=max_messages_per_member,
                speaker_order=[m.id for m in members],
            ),
            context_policy=ContextPolicy(
                default_context_access="prompt_only",
                default_transcript_access="own_messages",
                allow_full_context=False,
                require_confirmation_for_remote_context=True,
                require_confirmation_for_full_context=True,
            ),
            budget_policy=BudgetPolicy(
                max_rounds=max_rounds,
                max_messages_per_member=max_messages_per_member,
                max_total_model_calls=max_rounds * member_count,
                max_remote_calls_per_run=0,
                max_remote_calls_per_day=None,
                max_input_tokens_per_turn=1024,
                max_output_tokens_per_turn=256,
                max_context_tokens_per_member=1024,
                max_estimated_usd_per_run=0.0,
                max_estimated_usd_per_month=None,
            ),
            finalization_policy=FinalizationPolicy(mode="transcript_only"),
            created_at=NOW, updated_at=NOW, revision=1,
        )
        store = RoomStore(
            rooms_dir=council_paths.rooms_dir(),
            deleted_dir=council_paths.deleted_rooms_dir(),
        )
        return store.create(room)

    return _factory
