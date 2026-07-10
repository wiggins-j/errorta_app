"""F129 typed per-task model assignment records."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .ledger import _now
from .model_catalog import provider_class


@dataclass(frozen=True)
class ModelAssignment:
    assignment_id: str
    task_id: str
    member_id: str
    route_id: str
    task_type: str
    difficulty_tier: str
    rationale: str
    source: str
    assigned_at: str
    catalog_revision: str = ""
    escalation_count: int = 0
    attempted_route_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ModelAssignment | None":
        if not isinstance(raw, dict) or not raw.get("route_id"):
            return None
        fields = cls.__dataclass_fields__
        known = {key: value for key, value in raw.items() if key in fields}
        known["attempted_route_ids"] = list(known.get("attempted_route_ids") or [])
        return cls(**known)


def make_assignment(*, task_id: str, member_id: str, route_id: str,
                    task_type: str, difficulty_tier: str, rationale: str,
                    source: str, catalog_revision: str = "") -> ModelAssignment:
    return ModelAssignment(
        assignment_id=f"ma-{uuid.uuid4().hex[:12]}", task_id=task_id,
        member_id=member_id, route_id=route_id, task_type=task_type,
        difficulty_tier=difficulty_tier, rationale=rationale, source=source,
        assigned_at=_now(), catalog_revision=catalog_revision,
    )


def bind_member_route(member: dict[str, Any], assignment: ModelAssignment) -> dict[str, Any]:
    """Return an execution copy whose entire route identity matches assignment."""
    bound = dict(member)
    route_id = assignment.route_id
    provider = provider_class(route_id)
    model = route_id.split(".", 1)[1] if "." in route_id else route_id
    bound.update({
        "gateway_route_id": route_id,
        "route_id": route_id,
        "provider_kind": provider,
        "provider": provider,
        "model": model,
        "model_display": model,
        "model_assignment": assignment.to_dict(),
    })
    return bound


def resolve_task_assignment(
    task: Any,
    member: dict[str, Any],
) -> tuple[ModelAssignment | None, str]:
    """Resolve/revalidate a task assignment. Returns (assignment, override reason)."""
    from .model_availability import available_route_ids, resolve_route_availability
    from .model_catalog import catalog_revision, load_catalog
    from .model_selector import NoCapableModel, select
    from .model_tier import tier_rank
    from .performance_corpus import digest

    member_id = str(member.get("id") or "")
    mode = str(member.get("model_mode") or "single")
    task_id = str(getattr(task, "task_id", "") or "")
    task_type = str(getattr(task, "task_type", "implementation") or "implementation")
    difficulty = str(getattr(task, "difficulty_tier", "mid") or "mid")
    existing = ModelAssignment.from_dict(getattr(task, "model_assignment", None))
    if mode != "multi":
        # Production room validation requires gateway_route_id. The fallback
        # preserves the runner's long-standing injected-fake test seam.
        route = str(
            member.get("gateway_route_id") or member.get("provider_kind")
            or member.get("id") or ""
        )
        if not route:
            return None, "missing_gateway_route"
        if existing and existing.member_id == member_id and existing.route_id == route:
            return existing, ""
        return make_assignment(
            task_id=task_id, member_id=member_id, route_id=route,
            task_type=task_type, difficulty_tier=difficulty,
            rationale="Single member configured route", source="single",
        ), ""

    pool = [str(route) for route in member.get("model_pool", []) if str(route)]
    projection = resolve_route_availability(pool)
    available = available_route_ids(projection)
    catalog = load_catalog(pool)
    revision = catalog_revision(catalog)
    if (
        existing and existing.member_id == member_id and existing.route_id in available
        and tier_rank(catalog[existing.route_id].capability_tier) >= tier_rank(difficulty)
    ):
        return existing, ""

    preferred = str(getattr(task, "preferred_route_id", "") or "")
    override_reason = ""
    chosen = ""
    source = "selector"
    rationale = str(getattr(task, "assignment_rationale", "") or "")
    if preferred:
        if preferred not in pool:
            override_reason = "route_out_of_pool"
        elif preferred not in available:
            override_reason = projection.get(preferred).reason if projection.get(preferred) else "unavailable"
        elif tier_rank(catalog[preferred].capability_tier) < tier_rank(difficulty):
            override_reason = "route_below_difficulty"
        else:
            chosen, source = preferred, "pm"
    if not chosen:
        selected = select(
            pool, available, catalog, difficulty,
            task_type=task_type, corpus_digest=digest(),
        )
        if isinstance(selected, NoCapableModel):
            return None, override_reason or selected.reason
        chosen = selected.route_id
        rationale = rationale or selected.rationale
        source = "override" if override_reason else "selector"
    return make_assignment(
        task_id=task_id, member_id=member_id, route_id=chosen,
        task_type=task_type, difficulty_tier=difficulty,
        rationale=rationale or "PM-selected model", source=source,
        catalog_revision=revision,
    ), override_reason


def next_escalation_assignment(task: Any) -> ModelAssignment | None:
    """Strictly increase capability within the persisted member pool."""
    from .model_availability import available_route_ids, resolve_route_availability
    from .model_catalog import catalog_revision, load_catalog
    from .model_selector import NoCapableModel, select
    from .model_tier import tier_rank
    from .performance_corpus import digest

    current = ModelAssignment.from_dict(getattr(task, "model_assignment", None))
    if current is None:
        return None
    extras = getattr(task, "_extras", {}) or {}
    pool = [str(route) for route in extras.get("model_pool_snapshot", []) if str(route)]
    if not pool:
        return None
    attempted = set(current.attempted_route_ids) | {current.route_id}
    candidates = [route for route in pool if route not in attempted]
    projection = resolve_route_availability(candidates)
    available = available_route_ids(projection)
    catalog = load_catalog(pool)
    current_rank = tier_rank(catalog[current.route_id].capability_tier)
    selected = select(
        candidates, available, catalog, current.difficulty_tier,
        task_type=current.task_type, corpus_digest=digest(),
        minimum_rank_exclusive=current_rank,
    )
    if isinstance(selected, NoCapableModel):
        return None
    return replace(
        current,
        assignment_id=f"ma-{uuid.uuid4().hex[:12]}",
        route_id=selected.route_id,
        rationale=f"Escalated after {current.route_id} was unproductive",
        source="escalation",
        assigned_at=_now(),
        catalog_revision=catalog_revision(catalog),
        escalation_count=current.escalation_count + 1,
        attempted_route_ids=sorted(attempted),
    )


__all__ = [
    "ModelAssignment", "bind_member_route", "make_assignment",
    "next_escalation_assignment", "resolve_task_assignment",
]
