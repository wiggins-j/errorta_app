"""Guards for `tests/liverun` -- the fix cycle's tests must never reach a real,
billable CLI probe.

`_default_assign_dev_route` (fixloop.py) may call
`errorta_app.routes.gateway.probe_cli_provider` via `loop_bridge.run_coro`
whenever a CLI route currently reads `cli_not_verified` -- any test in this
directory that reaches that path unmocked would launch a real `claude -p` (or
`codex`/`agent`) subprocess. Legitimate direct coverage of `probe_cli_provider`
itself lives in `tests/test_gateway_routes.py`, outside this directory.

A test that needs to exercise the probe-wiring itself (e.g.
`test_default_assign_dev_route_probes_a_cold_cli_route_before_seating`) sets
its own `monkeypatch.setattr(gw, "probe_cli_provider", ...)` inside the test
body -- that assignment runs AFTER this autouse fixture's setup and simply
overrides it for the rest of that test, so no marker/opt-out is needed.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _guard_real_cli_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    from errorta_app.routes import gateway as gw

    async def _refuse(provider: str) -> dict:
        raise AssertionError("real CLI probe reached from a unit test")

    monkeypatch.setattr(gw, "probe_cli_provider", _refuse)
