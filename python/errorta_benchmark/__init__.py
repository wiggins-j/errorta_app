"""F-DEMO-01 benchmark harness — scaffold only.

This package is additive and intentionally has no runtime dependencies on
``errorta_judge`` or ``errorta_app``. It provides a deterministic, pure-logic
scaffold so the live-run pipeline can be wired up later without re-shaping
the data model or report contract.

No live runs are enabled in this version. The CLI driver
(``scripts/run-benchmark.sh``) refuses to call out unless a judge endpoint
is reachable.
"""
from __future__ import annotations

__version__ = "0.0.1"
