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
    # SPEC-44: the tier that was REQUESTED when this assignment is a bounded
    # downgrade; "" otherwise. `difficulty_tier` above always carries the tier the
    # selector actually ran at — the tier the corpus buckets under and the reuse
    # guard compares against — so an assignment never claims a capability its route
    # does not have. Visibility rides on THIS persisted field rather than on the
    # `difficulty_downgraded` decision, because every `record_decision` in this repo
    # is best-effort. `from_dict` filters to `__dataclass_fields__`, so rows
    # persisted before this field load unchanged.
    difficulty_downgraded_from: str = ""

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
                    source: str, catalog_revision: str = "",
                    difficulty_downgraded_from: str = "") -> ModelAssignment:
    return ModelAssignment(
        assignment_id=f"ma-{uuid.uuid4().hex[:12]}", task_id=task_id,
        member_id=member_id, route_id=route_id, task_type=task_type,
        difficulty_tier=difficulty_tier, rationale=rationale, source=source,
        assigned_at=_now(), catalog_revision=catalog_revision,
        difficulty_downgraded_from=difficulty_downgraded_from,
    )


def bind_member_route(member: dict[str, Any], assignment: ModelAssignment) -> dict[str, Any]:
    """Return an execution copy whose entire route identity matches assignment."""
    bound = dict(member)
    route_id = assignment.route_id
    provider = provider_class(route_id)
    # Strip the transport segment too: the local handler's own routes are
    # `local.ollama.<model>`, and the un-stripped form is a 404 at Ollama.
    from .model_catalog import model_id_from_route
    model = model_id_from_route(route_id)
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


def _select_downgraded(pool, available, catalog, difficulty, *, task_type,
                       corpus_digest, limit):
    """SPEC-44: the highest tier strictly BELOW ``difficulty`` that the pool can
    satisfy, within ``limit`` ranks and never below ``light``.

    Returns ``(Selection | None, satisfied_tier)``. With the default limit of 1 this
    reaches ``strong -> mid`` and ``mid -> light`` but refuses ``strong -> light``:
    a two-rank drop is a different claim about what the run ran at.
    """
    from .model_selector import NoCapableModel, select
    from .model_tier import LIGHT, MID, STRONG, tier_rank

    by_rank = (LIGHT, MID, STRONG)
    start = tier_rank(difficulty)
    floor = max(0, start - limit)
    for rank in range(start - 1, floor - 1, -1):
        got = select(pool, available, catalog, by_rank[rank],
                     task_type=task_type, corpus_digest=corpus_digest)
        if not isinstance(got, NoCapableModel):
            return got, by_rank[rank]
    return None, ""


def resolve_task_assignment(
    task: Any,
    member: dict[str, Any],
    *,
    difficulty_downgrade_limit: int = 0,
) -> tuple[ModelAssignment | None, str]:
    """Resolve/revalidate a task assignment. Returns (assignment, override reason).

    SPEC-44: ``difficulty_downgrade_limit`` is how many capability ranks the INITIAL
    assignment may drop when the pool contains nothing at the requested tier. It
    defaults to 0 — the legacy value — so the existing two-argument callers (and the
    ~50 direct `build_run_turn` test callers upstream) keep today's behaviour exactly:
    ``(None, "no_capable_model")`` and a hard block. `CodingRunner.run` passes the
    policy field, whose default is 1.
    """
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
    if existing and existing.member_id == member_id and existing.route_id in available:
        have = tier_rank(catalog[existing.route_id].capability_tier)
        if have >= tier_rank(difficulty):
            return existing, ""
        # SPEC-44 constraint 5. `difficulty` is re-derived from the task every turn,
        # so a downgraded assignment fails the clause above FOREVER — minting a fresh
        # assignment_id and writing a duplicate `difficulty_downgraded` decision on
        # every single turn. An already-recorded downgrade for THIS request is
        # honoured instead of re-derived, provided the route still satisfies the tier
        # the downgrade settled on.
        #
        # The guard stays tight in the direction that matters: if the operator later
        # adds a capable route, the weak route still fails `have >= requested`, and
        # this branch keeps it. That is accepted and named — the remedy is the same
        # as for any stale assignment, the F127 escalate rung.
        if (existing.difficulty_downgraded_from == difficulty
                and have >= tier_rank(existing.difficulty_tier)):
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
    satisfied = difficulty
    downgraded_from = ""
    if not chosen:
        corpus = digest()
        selected = select(
            pool, available, catalog, difficulty,
            task_type=task_type, corpus_digest=corpus,
        )
        if isinstance(selected, NoCapableModel):
            # SPEC-44 constraint 4: ONLY `no_capable_model` is a tier problem.
            # `unavailable` and `empty_pool` are connectivity/config faults, and a
            # lower requested tier cannot make an unreachable route reachable —
            # downgrading there would fabricate a capability claim out of a
            # connectivity fault.
            if selected.reason != "no_capable_model" or difficulty_downgrade_limit <= 0:
                return None, override_reason or selected.reason
            selected, satisfied = _select_downgraded(
                pool, available, catalog, difficulty, task_type=task_type,
                corpus_digest=corpus, limit=difficulty_downgrade_limit,
            )
            if selected is None:
                return None, override_reason or "no_capable_model"
            downgraded_from = difficulty
        chosen = selected.route_id
        rationale = rationale or selected.rationale
        source = "override" if override_reason else "selector"
    return make_assignment(
        task_id=task_id, member_id=member_id, route_id=chosen,
        task_type=task_type, difficulty_tier=satisfied,
        rationale=rationale or "PM-selected model", source=source,
        catalog_revision=revision, difficulty_downgraded_from=downgraded_from,
    ), override_reason


def next_escalation_assignment(task: Any) -> tuple[ModelAssignment | None, str]:
    """Strictly increase capability within the persisted member pool.

    SPEC-44 Move 3: returns ``(assignment, reason)``. ``reason`` is ``""`` on
    success; otherwise it names WHICH of the five ways the rung was unavailable, so
    a ladder that silently loses a rung can no longer report itself as fully bounded:

    * ``no_current_assignment`` — the task never had an assignment to escalate from
    * ``empty_pool_snapshot`` — no ``model_pool_snapshot`` on the task
    * ``all_routes_attempted`` — every route in the snapshot is already attempted.
      This is the reason a SINGLE-model box hits: with a one-route pool the candidate
      list is empty and `select` returns ``empty_pool`` before the candidate loop, so
      it never reaches ``no_capable_model``. The selector's ``empty_pool`` is remapped
      here because at THIS layer the pool is not empty — the candidate set is, and
      that difference is the whole point of the discrimination.
    * ``unavailable`` — candidates remain but none is currently reachable
    * ``no_capable_model`` — candidates are reachable but none outranks the current
      route (issue #82); reaching it needs >=2 routes.

    There is NO downgrade here, deliberately: escalation is supposed to be able to
    find nothing, and the downgrade is an ENTRY condition for work, not a recovery
    rung.
    """
    from .model_availability import available_route_ids, resolve_route_availability
    from .model_catalog import catalog_revision, load_catalog
    from .model_selector import NoCapableModel, select
    from .model_tier import tier_rank
    from .performance_corpus import digest

    current = ModelAssignment.from_dict(getattr(task, "model_assignment", None))
    if current is None:
        return None, "no_current_assignment"
    extras = getattr(task, "_extras", {}) or {}
    pool = [str(route) for route in extras.get("model_pool_snapshot", []) if str(route)]
    if not pool:
        return None, "empty_pool_snapshot"
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
        reason = ("all_routes_attempted" if selected.reason == "empty_pool"
                  else selected.reason)
        return None, reason
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
    ), ""


__all__ = [
    "ModelAssignment", "bind_member_route", "make_assignment",
    "next_escalation_assignment", "resolve_task_assignment",
]
