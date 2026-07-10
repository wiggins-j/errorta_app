"""F-DIST-01 slice 6 — transparent, tiered telemetry (spec §9/§11).

Owns ``telemetry.json``: the opt-out flag, the unsent **floor** deltas (Tier-1),
and the bounded **extras** queue (Tier-2). Every recorded event is either a
fixed floor counter or an allowlisted extra with only an enum name + integer
count + bucket label — **never content**. The allowlist is enforced here so a
caller cannot smuggle a prompt/filename/path into a metric name.

The whole module is **inert when the alpha gate is off** (production posture):
record calls no-op, so a shipped keyless build never writes ``telemetry.json``.
Extras additionally require the opt-out flag to be on.

Nothing here touches the network — sending is ``client.py``'s job (invariant 1).
"""
from __future__ import annotations

import threading
from typing import Any

from errorta_app.paths import alpha_telemetry_path

from . import config
from .storage import read_json, write_json_0600

EXTRAS_QUEUE_CAP = 1000

# Tier-2 extras: the allowed event *types*.
_EXTRA_EVENTS = frozenset({"feature_used", "perf_timing", "crash_breadcrumb"})

# feature_used.name allowlist (spec §9). Counts only — never arguments/content.
FEATURE_NAMES = frozenset(
    {
        "judge_run",
        "corpus_ingest",
        "brief_collect",
        "council_run",
        "coding_run",
        "welcome_ingest",
        "export_bundle",
        "watch_start",
    }
)

# perf_timing.op allowlist + coarse latency buckets (never raw timings).
PERF_OPS = frozenset({"judge_verdict", "council_turn", "coding_turn", "retrieval"})
PERF_BUCKETS = frozenset({"<1s", "1-5s", "5-15s", "15-60s", ">60s"})

# Floor counters we track locally (Tier-1). `queue_overflow` is a local-only
# signal that extras were dropped; it rides the heartbeat floor dict and is
# ignored by servers that don't yet unpack it.
_FLOOR_KEYS = ("launches", "crash_free_sessions", "queue_overflow")

_lock = threading.Lock()


# ---- persistence ------------------------------------------------------------

def _norm_floor(v: Any) -> dict[str, int]:
    v = v if isinstance(v, dict) else {}
    out: dict[str, int] = {}
    for k in _FLOOR_KEYS:
        n = v.get(k)
        if isinstance(n, int) and not isinstance(n, bool) and n > 0:
            out[k] = n
    return out


def _event_is_clean(e: Any) -> bool:
    """Re-validate a queued event against the same allowlist enforced at record
    time. Defense in depth for the marquee guarantee: even a corrupted or
    tampered ``telemetry.json`` can never put a non-allowlisted name (a smuggled
    prompt/path) back onto the wire, since ``snapshot_queue`` feeds the sender."""
    if not isinstance(e, dict):
        return False
    kind = e.get("event")
    if kind == "feature_used":
        return e.get("name") in FEATURE_NAMES
    if kind == "perf_timing":
        return e.get("name") in PERF_OPS and e.get("bucket") in PERF_BUCKETS
    if kind == "crash_breadcrumb":
        # Content-free code identifier only (e.g. Class@module:line). Mirror the
        # producer's refusal of a path-shaped value so a corrupted/tampered store
        # can never resurrect a filename/path onto the wire.
        bucket = e.get("bucket")
        return (
            isinstance(bucket, str)
            and "/" not in bucket
            and "\\" not in bucket
            and len(bucket) <= 200
        )
    return False


def _norm_queue(v: Any) -> list[dict[str, Any]]:
    if not isinstance(v, list):
        return []
    out = [e for e in v if _event_is_clean(e)]
    return out[:EXTRAS_QUEUE_CAP]


def _load() -> dict[str, Any]:
    data = read_json(alpha_telemetry_path()) or {}
    return {
        "extras_enabled": bool(data.get("extras_enabled", True)),
        "floor": _norm_floor(data.get("floor")),
        "queue": _norm_queue(data.get("queue")),
    }


def _save(state: dict[str, Any]) -> None:
    write_json_0600(alpha_telemetry_path(), state)


# ---- consent ----------------------------------------------------------------

def extras_enabled() -> bool:
    """Whether Tier-2 extras are on. Meaningless when the gate is off."""
    return _load()["extras_enabled"]


def set_extras_enabled(value: bool) -> None:
    if not config.gate_enabled():
        return
    with _lock:
        s = _load()
        s["extras_enabled"] = bool(value)
        _save(s)


# ---- recording (inert when the gate is off) ---------------------------------

def record_launch() -> None:
    _bump_floor("launches")


def record_clean_session() -> None:
    _bump_floor("crash_free_sessions")


def _bump_floor(key: str) -> None:
    if not config.gate_enabled():
        return
    with _lock:
        s = _load()
        s["floor"][key] = s["floor"].get(key, 0) + 1
        _save(s)


def record_feature_used(name: str) -> bool:
    """Record one use of an allowlisted feature. Returns False (dropped) for an
    off-catalog name, gate-off, or extras-off."""
    if name not in FEATURE_NAMES:
        return False
    return _enqueue({"event": "feature_used", "name": name, "count": 1})


def record_perf(op: str, bucket: str) -> bool:
    if op not in PERF_OPS or bucket not in PERF_BUCKETS:
        return False
    return _enqueue({"event": "perf_timing", "name": op, "bucket": bucket, "count": 1})


def record_crash_breadcrumb(descriptor: str) -> bool:
    """Enqueue a crash breadcrumb as a Tier-2 extra. ``descriptor`` is a compact,
    content-free code identifier (e.g. ``ValueError@errorta_council.engine:412``).
    A path-shaped value (containing ``/``) is refused as a defensive backstop, so
    a raw traceback can never slip through even if a caller mis-builds it."""
    if not descriptor or "/" in descriptor or "\\" in descriptor:
        return False
    return _enqueue({"event": "crash_breadcrumb", "bucket": descriptor[:200], "count": 1})


def _enqueue(evt: dict[str, Any]) -> bool:
    if not config.gate_enabled():
        return False
    with _lock:
        s = _load()
        if not s["extras_enabled"]:
            return False
        q: list[dict[str, Any]] = s["queue"]
        q.append(evt)
        if len(q) > EXTRAS_QUEUE_CAP:
            # Drop-oldest, and record that a drop happened (spec §11).
            del q[0 : len(q) - EXTRAS_QUEUE_CAP]
            s["floor"]["queue_overflow"] = s["floor"].get("queue_overflow", 0) + 1
        _save(s)
        return True


# ---- send-side helpers (called by client.py) --------------------------------

def snapshot_floor() -> dict[str, int]:
    return dict(_load()["floor"])


def clear_floor(sent: dict[str, int]) -> None:
    """Subtract the counts that were successfully sent, so events that arrived
    between snapshot and send are preserved."""
    with _lock:
        s = _load()
        floor = s["floor"]
        for k, v in (sent or {}).items():
            remaining = floor.get(k, 0) - int(v)
            if remaining > 0:
                floor[k] = remaining
            else:
                floor.pop(k, None)
        _save(s)


def snapshot_queue() -> list[dict[str, Any]]:
    return list(_load()["queue"])


def drop_queue_prefix(count: int) -> None:
    """Remove the first ``count`` queued events after a successful send."""
    with _lock:
        s = _load()
        s["queue"] = s["queue"][count:]
        _save(s)


# ---- inspector ("see exactly what we send") ---------------------------------

def inspector_snapshot(limit: int = 50) -> dict[str, Any]:
    s = _load()
    queue = s["queue"]
    return {
        "extras_enabled": s["extras_enabled"],
        "floor": s["floor"],
        "queue": queue[-limit:],
        "queue_len": len(queue),
    }
