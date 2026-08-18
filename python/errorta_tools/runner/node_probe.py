"""Process spawn for the web probe's headless-browser oracle.

Lives in ``errorta_tools`` rather than ``errorta_council`` on purpose. The F039
egress invariant is that ``errorta_council`` reaches every process through this
layer and never imports ``subprocess`` itself — the same way
``errorta_council/coding/workspace.py`` reaches git through
``errorta_tools.runner.apply_workspace`` instead of shelling out directly. Two
tests lock it:

* ``tests/council/test_tool_runner_local.py::test_errorta_council_runner_imports_no_process_egress_modules``
* ``tests/council/test_toolgateway_slice1.py::test_errorta_council_tool_use_imports_no_egress_modules``

``coding/web_probe.py`` (SPEC-40's black-canvas oracle) needs to shell out to
Node + Playwright, so the spawn itself belongs here and the engine-side parsing
and verdict logic stays in ``web_probe``.
"""
from __future__ import annotations

import subprocess
from typing import Optional, Sequence


def run_node_probe(
    argv: Sequence[str], *, cwd: str, timeout_s: float,
) -> Optional[str]:
    """Run ``argv`` in ``cwd`` and return its stdout, or ``None`` on ANY failure.

    **Fail-open by contract.** The caller treats ``None`` as "the probe could
    not run", which records NO evidence — never a red gate and never a failed
    turn. A missing ``node``, an unavailable Chromium, a timeout, or any spawn
    error must therefore degrade to ``None`` rather than propagate. This
    function never raises.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — trusted engine script, argv-only
            list(argv), cwd=cwd, capture_output=True,
            text=True, timeout=timeout_s, check=False)
    except Exception:  # noqa: BLE001 — spawn/timeout failure -> no evidence
        return None
    return proc.stdout or ""
