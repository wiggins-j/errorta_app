"""SPEC-46 — the per-run drop counter, keyed by normalized task identity.

Because dedupe deliberately lets a `dropped` task be re-created as a NEW record
(so a regression can re-open one), a per-task-id counter cannot see a create→drop
→re-create loop. This ledger keys on `task_dedupe.identity_key`, persisted in
`run_state.json`, so a repeatedly-dropped job accumulates a count across cycles.
"""
from __future__ import annotations

from typing import Any

_KEY = "drop_ledger"


def _ledger(store: Any) -> dict[str, int]:
    try:
        raw = store.get_run_state().get(_KEY) or {}
        return {str(k): int(v) for k, v in raw.items()}
    except Exception:  # noqa: BLE001 — a read failure means "no counts yet"
        return {}


def drop_count(store: Any, identity: str) -> int:
    return _ledger(store).get(str(identity), 0)


def record_drop(store: Any, identity: str) -> int:
    """Increment and persist the count for `identity`; return the new value."""
    with store.lock:  # RMW guarded — nested set_run_state re-acquires the RLock
        led = _ledger(store)
        led[str(identity)] = led.get(str(identity), 0) + 1
        try:
            store.set_run_state(**{_KEY: led})
        except Exception:  # noqa: BLE001 — best-effort; a lost increment only delays quarantine
            pass
        return led[str(identity)]
