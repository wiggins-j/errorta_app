"""F100 - materialize approved governance plan slices into DEV tasks."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .governance import GovernanceError, GovernanceStore, PlanSlice
from .ledger import LedgerStore
from .topology import DEV


def _slice_state_key(plan_artifact_id: str, slice_id: str) -> str:
    return f"{plan_artifact_id}:{slice_id}"


def _load_slice_state(governance: GovernanceStore) -> dict[str, Any]:
    path = governance.dir / "governance_slice_state.json"
    if not path.exists():
        return {"schema_version": "coding_governance_slice_state.v1", "slices": {}}
    try:
        import json
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {"schema_version": "coding_governance_slice_state.v1", "slices": {}}
    if not isinstance(raw.get("slices"), dict):
        raw["slices"] = {}
    return raw


def _save_slice_state(governance: GovernanceStore, state: dict[str, Any]) -> None:
    from .ledger import _atomic_write_json
    _atomic_write_json(governance.dir / "governance_slice_state.json", state)


def materialize_approved_plan(
    store: LedgerStore,
    governance: GovernanceStore | None = None,
) -> dict[str, Any]:
    """Create DEV tasks for every approved plan slice, idempotently."""
    governance = governance or GovernanceStore.for_ledger(store)
    state = governance.load_state()
    plan = governance.latest_approved_artifact("implementation_plan")
    spec = governance.latest_approved_artifact("spec")
    if plan is None:
        if state.mode == "strict":
            raise GovernanceError("strict governance requires an approved plan")
        return {"created": 0, "existing": 0, "tasks": []}
    if state.mode == "strict" and spec is None:
        raise GovernanceError("strict governance requires an approved spec")

    slices = governance.plan_slices(plan)
    slice_state = _load_slice_state(governance)
    mapping: dict[str, Any] = dict(slice_state.get("slices") or {})
    created = 0
    existing = 0
    tasks: list[dict[str, Any]] = []
    title_to_task_id = {t.title: t.task_id for t in store.list_tasks()}
    slice_to_task_id: dict[str, str] = {}

    for item in mapping.values():
        if isinstance(item, dict) and item.get("task_id"):
            slice_to_task_id[str(item.get("slice_id") or "")] = str(item["task_id"])

    for plan_slice in slices:
        key = _slice_state_key(plan.artifact_id, plan_slice.slice_id)
        existing_record = mapping.get(key)
        if isinstance(existing_record, dict) and existing_record.get("task_id"):
            existing += 1
            tasks.append(existing_record)
            slice_to_task_id[plan_slice.slice_id] = str(existing_record["task_id"])
            continue
        task = store.add_task(
            title=plan_slice.title,
            role=DEV,
            detail=plan_slice.task_detail(),
            source_spec_artifact_id=spec.artifact_id if spec else None,
            source_plan_artifact_id=plan.artifact_id,
            source_slice_id=plan_slice.slice_id,
            governance_required=state.mode == "strict",
        )
        created += 1
        rec = {
            "slice_id": plan_slice.slice_id,
            "task_id": task.task_id,
            "plan_artifact_id": plan.artifact_id,
            "task_title": task.title,
            "slice": asdict(plan_slice),
        }
        mapping[key] = rec
        title_to_task_id[task.title] = task.task_id
        slice_to_task_id[plan_slice.slice_id] = task.task_id
        tasks.append(rec)

    # Resolve dependencies after all tasks exist. Dependencies can reference a
    # slice id or a task title.
    for plan_slice in slices:
        task_id = slice_to_task_id.get(plan_slice.slice_id)
        if not task_id:
            continue
        deps: list[str] = []
        for dep in plan_slice.depends_on:
            dep_task_id = slice_to_task_id.get(dep) or title_to_task_id.get(dep)
            if dep_task_id and dep_task_id not in deps:
                deps.append(dep_task_id)
        if deps:
            try:
                store.update_task(task_id, depends_on=deps)
            except Exception:
                pass

    slice_state["slices"] = mapping
    _save_slice_state(governance, slice_state)
    if state.phase != "development":
        governance.update_state(phase="development")
    return {"created": created, "existing": existing, "tasks": tasks}


def plan_slice_for_task(store: LedgerStore, task_id: str) -> PlanSlice | None:
    task = next((t for t in store.list_tasks() if t.task_id == task_id), None)
    if task is None or not task.source_plan_artifact_id or not task.source_slice_id:
        return None
    governance = GovernanceStore.for_ledger(store)
    plan = governance.get_artifact(task.source_plan_artifact_id)
    for plan_slice in governance.plan_slices(plan):
        if plan_slice.slice_id == task.source_slice_id:
            return plan_slice
    return None


__all__ = ["materialize_approved_plan", "plan_slice_for_task"]
