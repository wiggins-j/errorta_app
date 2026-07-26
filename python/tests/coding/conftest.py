"""Coding-suite fixtures.

Spec 20 (repair): make this suite HERMETIC. ``LedgerStore(root=tmp_path)`` is
per-test, but ``CodingWorkspace`` is not — ``ApplyWorkspace`` roots its snapshot
at ``council_root() / "apply-workspaces" / <run_id>`` (apply_workspace.py:227),
which resolves through ``$ERRORTA_HOME`` and defaults to the developer's real
``~/.errorta``. So every gate/evidence test that builds a workspace wrote into
live user data under a fixed, non-unique id (``coding-gate``, ``coding-ev``, …),
and ``ApplyWorkspace.ensure`` is copy-once: on the next run it short-circuits on
``self.exists()`` and keeps the OLD ``<run_id>.source.json``. For a greenfield
project that pointer names a ``tempfile.mkdtemp`` seed dir, which the OS reaps
after a few days — after which ``merge_back_preview`` raises
``apply_source_missing``, ``gather_merge_evidence`` fails closed (evidence.py
M1), and the merge gate is permanently blocked on ``preview_unavailable``. That
is exactly the five-failure cluster in test_merge_gate_wiring /
test_merge_gate_strict_dual_review / test_evidence_binding: not a product bug and
not caused by any source change, but a result that depends on how long ago this
machine last ran the suite.

Pinning ``$ERRORTA_HOME`` at ``tmp_path/.errorta`` fixes both halves: results no
longer depend on machine history, and the suite stops writing into the user's
real Council data. The layout deliberately matches the shared ``tmp_errorta_home``
fixture (same ``tmp_path``, same ``.errorta`` child), so tests that also request
that fixture resolve to the identical directory and compose unchanged.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _hermetic_errorta_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / ".errorta"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ERRORTA_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return home
