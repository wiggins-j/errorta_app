"""Shared observed-connectivity cache (process-level, advisory).

Both the app gateway routes (``errorta_app.routes.gateway``) and the engine-core
preflight (``errorta_council.coding.member_health``) depend on
``errorta_model_gateway``, but the architecture invariant *"``errorta_council``
must not import ``errorta_app``"* means the engine cannot warm the app's
``_PROBE_CACHE`` directly. This module is the shared seam between the two layers:
when the engine preflight probes a CLI/subscription provider and it reports
``connected``, it records that observation here; the app's ``/gateway/providers``
and ``cli-status`` readers consult it (most-recent-signal-wins against the
explicit Test cache) so a provider that is *actively used* shows ``connected``
without the user ever pressing Test.

Safety — this cache records POSITIVE observations only. A provider is recorded
here ONLY when it was genuinely observed connected; a failed / unknown probe
records nothing. It therefore can never flip a provider to ``connected`` falsely,
and an explicit *newer* negative Test still wins at the read site (it carries a
later timestamp). No credential/token is ever stored — only a boolean, a
timestamp, and a short free-text source tag.
"""
from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
# provider_class -> {"connected": True, "at": <epoch float>, "source": <str>}
_OBSERVED: dict[str, dict[str, object]] = {}


def record_connected(provider_class: str, *, source: str = "") -> None:
    """Record that ``provider_class`` was just observed CONNECTED.

    Positive-only: there is deliberately no ``record_disconnected`` — a probe
    that is not ``connected`` records nothing, so this cache can never assert a
    provider is reachable when it isn't.
    """
    pc = (provider_class or "").strip()
    if not pc:
        return
    with _LOCK:
        _OBSERVED[pc] = {"connected": True, "at": time.time(), "source": (source or "")[:40]}


def observed_at(provider_class: str) -> float | None:
    """Timestamp of the last connected observation for ``provider_class`` (or None).

    A returned value always corresponds to a ``connected`` observation (this cache
    holds nothing else), so a non-``None`` result means "observed connected at t".
    """
    pc = (provider_class or "").strip()
    with _LOCK:
        entry = _OBSERVED.get(pc)
        if not entry:
            return None
        return float(entry.get("at") or 0.0)


def clear(provider_class: str | None = None) -> None:
    """Drop one observation (or all, when ``provider_class`` is None).

    Used by tests for isolation and available to any explicit disconnect flow.
    """
    with _LOCK:
        if provider_class is None:
            _OBSERVED.clear()
        else:
            _OBSERVED.pop((provider_class or "").strip(), None)


__all__ = ["record_connected", "observed_at", "clear"]
