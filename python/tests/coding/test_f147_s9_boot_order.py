"""F147 S9 follow-up (review NIT-5) — pin the safety-critical boot ordering.

``server.py``'s lifespan MUST run coding boot recovery (``scan_and_recover``)
BEFORE it writes THIS sidecar's own advertisement (``write_advertisement``). The
order is safety-critical: the owner-aware boot-recovery seam
(``_coding_owner_peer_fn``) reads ``${ERRORTA_HOME}/sidecar.json`` to decide
whether a ``running`` run is owned by a live PEER sidecar. If our own advert were
written first, boot recovery would read our OWN pid back and could mis-decide
(``owner_is_live_peer_sidecar`` would see the advert naming *us* and treat a
genuine orphan as a live peer, wedging it).

S9b verified this order only by code reading — the boot-recovery unit tests inject
the ``owner_peer_fn`` seam directly, so a refactor that hoisted the advert above
the scan would silently disable the protection with all of them still green. This
test spies on the two symbols the lifespan actually calls and asserts scan
precedes write, through the REAL ``server.py`` wiring.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from errorta_council.coding import run_recovery
from errorta_council.coding.run_recovery import CodingRunRecoverySummary


def test_boot_recovery_scan_precedes_self_advertisement(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from errorta_app import sidecar_advert
    from errorta_app.server import app

    calls: list[str] = []

    def spy_scan(**kwargs: object) -> CodingRunRecoverySummary:
        calls.append("scan")
        return CodingRunRecoverySummary()

    def spy_write(**kwargs: object) -> bool:
        calls.append("write")
        return True

    # The lifespan imports both symbols lazily at call time (``from ... import
    # scan_and_recover as _scan_coding`` / ``from . import sidecar_advert``), so
    # patching the source module attributes intercepts the real calls.
    monkeypatch.setattr(run_recovery, "scan_and_recover", spy_scan)
    monkeypatch.setattr(sidecar_advert, "write_advertisement", spy_write)
    # No parent → the watchdog thread is a no-op; keeps the lifespan light.
    monkeypatch.delenv("ERRORTA_PARENT_PID", raising=False)

    # Entering TestClient as a context manager runs the real lifespan startup.
    with TestClient(app):
        pass

    assert "scan" in calls, "coding boot recovery did not run in the lifespan"
    assert "write" in calls, "sidecar self-advertisement did not run in the lifespan"
    assert calls.index("scan") < calls.index("write"), (
        "coding boot recovery (scan_and_recover) MUST run before this sidecar "
        f"writes its own advertisement (write_advertisement); got order {calls}"
    )
