"""Locks the fail-closed asymmetry the Windows port depends on
(spec 2026-07-06-windows-port-design.md §3 caveat, review H1):

- `resolve_sandbox_backend("auto")` degrades to SANDBOX_NONE when no
  native backend is available (Windows today: no seatbelt/bwrap).
- `resolve_sandbox_backend("seatbelt")` (an EXPLICIT unavailable backend,
  e.g. a Mac-authored profile opened on Windows) RAISES SandboxUnavailable
  — never a silent downgrade.

The manager/route layers catch that raise and record a blocked result;
this test pins the primitive so that contract can't regress.
"""
import pytest

import errorta_tools.runner.sandbox as sandbox
from errorta_council.coding.runtime_process import resolve_sandbox_backend
from errorta_tools.runner.sandbox import SANDBOX_NONE, SandboxUnavailable


def test_auto_degrades_to_none_when_no_native_backend(monkeypatch):
    # Simulate a host with no seatbelt/bwrap/docker (i.e. Windows pre-Phase-3).
    monkeypatch.setattr(sandbox, "is_available", lambda backend: False)
    assert resolve_sandbox_backend("auto") == SANDBOX_NONE


def test_explicit_unavailable_backend_raises(monkeypatch):
    monkeypatch.setattr(sandbox, "is_available", lambda backend: False)
    with pytest.raises(SandboxUnavailable):
        resolve_sandbox_backend("seatbelt")
