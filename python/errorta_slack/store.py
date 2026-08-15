"""Durable bridge store for the Slack PM bridge.

Persists channel bindings, the outbound cursor, event dedupe, C-class
confirmation records, and per-channel prefs under ``slack_dir()``. Each
concern lives in its own JSON file (``bindings.json``, ``cursors.json``,
``seen-events.json``, ``confirmations.json``, ``prefs.json``) written with
the same atomic tmp+rename discipline as ``errorta_slack.config`` and
``errorta_slack.secrets``.

This module MUST NOT import ``slack_sdk`` or any other optional dependency
at module load time — the Slack bridge is strictly optional and disabled by
default, so the rest of the sidecar must keep working when ``slack-sdk``
isn't installed.

Concurrency: single-process file-based, like the mobile stores. Every
mutating operation's full read-modify-write is additionally guarded by a
module-level ``threading.RLock`` (see ``_LOCK`` below) so concurrent
in-process threads (e.g. Task 8's per-``thread_ts`` Slack workers) can't
lose an update to each other; this is not a cross-process lock.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from errorta_slack import config

# Dedupe: keep only the most recent N seen event ids.
_SEEN_EVENTS_MAX = 512

# Guards every mutating operation's full read-modify-write (load -> mutate
# -> write) so it is atomic within this process. Task 8's connection.py runs
# one worker per Slack thread_ts, each as its own OS thread via
# asyncio.to_thread — without this lock, two concurrent callers (e.g. two
# threads both staging a confirmation) can race: both load the same stale
# file, mutate their own in-memory copy, and the second write clobbers the
# first's change (a lost update — a staged confirmation silently vanishes
# and its Approve button no-ops forever). An RLock (not Lock) so a function
# that legitimately calls another locked function internally doesn't
# deadlock. This protects concurrent THREADS within one process; it is not
# a cross-process lock (matches this module's existing documented scope).
_LOCK = threading.RLock()


# --- Generic atomic-write JSON helpers ------------------------------------


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return raw


def _write_json(path: Path, data: Any) -> None:
    """Atomically write ``data`` as JSON to ``path`` with mode 0600.

    mkstemp in the same dir -> write -> chmod 0600 -> os.replace, mirroring
    ``errorta_slack.config.save`` / ``errorta_slack.secrets.save_tokens``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.stem}-",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


# --- Bindings --------------------------------------------------------------


def _bindings_path() -> Path:
    return config.slack_dir() / "bindings.json"


def _load_bindings() -> dict[str, dict[str, Any]]:
    raw = _read_json(_bindings_path(), {})
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def bind_channel(channel_id: str, project_id: str) -> None:
    """Bind ``channel_id`` to ``project_id``, overwriting any prior binding."""
    with _LOCK:
        bindings = _load_bindings()
        bindings[channel_id] = {"channel_id": channel_id, "project_id": project_id}
        _write_json(_bindings_path(), bindings)


def binding_for(channel_id: str) -> dict[str, Any] | None:
    """Return the binding record for ``channel_id``, or ``None`` if unbound."""
    return _load_bindings().get(channel_id)


def list_bindings() -> list[dict[str, Any]]:
    """Return all binding records."""
    return list(_load_bindings().values())


def unbind(channel_id: str) -> None:
    """Remove any binding for ``channel_id``. No-op if it wasn't bound."""
    with _LOCK:
        bindings = _load_bindings()
        if channel_id not in bindings:
            return
        del bindings[channel_id]
        _write_json(_bindings_path(), bindings)


# --- Outbound cursor ---------------------------------------------------


def _cursors_path() -> Path:
    return config.slack_dir() / "cursors.json"


def _load_cursors() -> dict[str, str]:
    raw = _read_json(_cursors_path(), {})
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, str)}


def get_cursor(channel_id: str) -> str | None:
    """Return the last-posted marker for ``channel_id``, or ``None``."""
    return _load_cursors().get(channel_id)


def advance_cursor(channel_id: str, marker: str) -> None:
    """Advance the outbound cursor for ``channel_id`` to ``marker``.

    Idempotent: advancing to the same marker twice is a no-op (no write).
    """
    with _LOCK:
        cursors = _load_cursors()
        if cursors.get(channel_id) == marker:
            return
        cursors[channel_id] = marker
        _write_json(_cursors_path(), cursors)


# --- Dedupe ----------------------------------------------------------------


def _seen_events_path() -> Path:
    return config.slack_dir() / "seen-events.json"


def _load_seen_events() -> list[str]:
    raw = _read_json(_seen_events_path(), [])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]


def seen_event(event_id: str) -> bool:
    """Record ``event_id`` and report whether it was already seen.

    Keeps a bounded list of the last ``_SEEN_EVENTS_MAX`` ids (oldest
    evicted first), so long-running channels don't grow this file
    unbounded.
    """
    with _LOCK:
        seen = _load_seen_events()
        if event_id in seen:
            return True
        seen.append(event_id)
        if len(seen) > _SEEN_EVENTS_MAX:
            seen = seen[-_SEEN_EVENTS_MAX:]
        _write_json(_seen_events_path(), seen)
        return False


# --- Confirmations -----------------------------------------------------


def _confirmations_path() -> Path:
    return config.slack_dir() / "confirmations.json"


def _load_confirmations() -> dict[str, dict[str, Any]]:
    raw = _read_json(_confirmations_path(), {})
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def stage_confirmation(
    verb: str,
    args: dict[str, Any],
    thread_ts: str,
    *,
    now: float | None = None,
) -> str:
    """Create a new pending confirmation record and return its id.

    Ids are generated with ``uuid4().hex`` (stdlib, not time-based) per
    the security requirement that confirmation ids be unguessable.
    """
    cid = uuid.uuid4().hex
    record = {
        "id": cid,
        "verb": verb,
        "args": args,
        "thread_ts": thread_ts,
        "created_at": now if now is not None else time.time(),
        "state": "pending",
    }
    with _LOCK:
        confirmations = _load_confirmations()
        confirmations[cid] = record
        _write_json(_confirmations_path(), confirmations)
    return cid


def get_confirmation(cid: str) -> dict[str, Any] | None:
    """Return the confirmation record for ``cid``, or ``None``."""
    return _load_confirmations().get(cid)


def resolve_confirmation(cid: str, decision: str) -> dict[str, Any]:
    """Set the confirmation's state to ``decision`` and return the record.

    Raises ``KeyError`` if ``cid`` is unknown.
    """
    with _LOCK:
        confirmations = _load_confirmations()
        record = confirmations[cid]
        record = dict(record)
        record["state"] = decision
        confirmations[cid] = record
        _write_json(_confirmations_path(), confirmations)
        return record


def pop_pending_older_than(
    max_age_seconds: float, *, now: float | None = None
) -> list[dict[str, Any]]:
    """Resolve and return pending confirmations older than ``max_age_seconds``.

    Each matching record's state is set to ``"timed_out"``. ``now``
    (epoch seconds) is injectable for deterministic tests; defaults to
    ``time.time()``.
    """
    effective_now = now if now is not None else time.time()
    with _LOCK:
        confirmations = _load_confirmations()
        stale: list[dict[str, Any]] = []
        changed = False
        for cid, record in confirmations.items():
            if record.get("state") != "pending":
                continue
            created_at = record.get("created_at")
            if not isinstance(created_at, (int, float)):
                continue
            if effective_now - created_at <= max_age_seconds:
                continue
            updated = dict(record)
            updated["state"] = "timed_out"
            confirmations[cid] = updated
            stale.append(updated)
            changed = True
        if changed:
            _write_json(_confirmations_path(), confirmations)
        return stale


# --- Prefs ----------------------------------------------------------------


def _prefs_path() -> Path:
    return config.slack_dir() / "prefs.json"


def _load_all_prefs() -> dict[str, dict[str, Any]]:
    raw = _read_json(_prefs_path(), {})
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def set_pref(channel_id: str, key: str, value: Any) -> None:
    """Set a single preference ``key`` for ``channel_id``."""
    with _LOCK:
        all_prefs = _load_all_prefs()
        channel_prefs = dict(all_prefs.get(channel_id, {}))
        channel_prefs[key] = value
        all_prefs[channel_id] = channel_prefs
        _write_json(_prefs_path(), all_prefs)


def get_prefs(channel_id: str) -> dict[str, Any]:
    """Return all preferences for ``channel_id`` (empty dict if none set)."""
    return _load_all_prefs().get(channel_id, {})


__all__ = [
    "bind_channel",
    "binding_for",
    "list_bindings",
    "unbind",
    "get_cursor",
    "advance_cursor",
    "seen_event",
    "stage_confirmation",
    "get_confirmation",
    "resolve_confirmation",
    "pop_pending_older_than",
    "set_pref",
    "get_prefs",
]
