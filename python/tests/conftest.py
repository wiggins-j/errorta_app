"""Shared pytest fixtures for the Errorta sidecar test suite.

Foundation-laid; individual test modules consume these fixtures.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def tmp_errorta_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate ~/.errorta to a tmp dir for the duration of one test.

    Sets HOME (and USERPROFILE on Windows) so any code that reads
    Path.home() lands inside tmp_path. Ensures ~/.errorta exists.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    errorta_dir = tmp_path / ".errorta"
    errorta_dir.mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def mock_psutil(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock psutil so hardware scans run deterministically.

    Also rebinds any already-imported consumer modules' module-level
    ``psutil`` attribute, so tests stay deterministic even if a prior test
    (e.g. one that boots the FastAPI app) caused ``errorta_hwdetect.scanner``
    to import the real ``psutil`` first.
    """
    fake = MagicMock()
    fake.cpu_count.return_value = 8
    fake.virtual_memory.return_value = MagicMock(total=16 * 1024**3, available=8 * 1024**3)
    monkeypatch.setitem(sys.modules, "psutil", fake)
    # Rebind on already-imported consumers so module-level `import psutil`
    # references resolve to the mock for the rest of this test.
    scanner_mod = sys.modules.get("errorta_hwdetect.scanner")
    if scanner_mod is not None:
        monkeypatch.setattr(scanner_mod, "psutil", fake, raising=False)
    return fake


@pytest.fixture
def mock_httpx_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch httpx.Client / httpx.AsyncClient with MagicMock instances."""
    import httpx

    client = MagicMock(spec=httpx.Client)
    async_client = MagicMock(spec=httpx.AsyncClient)
    monkeypatch.setattr(httpx, "Client", MagicMock(return_value=client))
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=async_client))
    return client


@pytest.fixture
def mock_subprocess_popen(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace subprocess.Popen with a MagicMock; returns the mock factory."""
    import subprocess

    proc = MagicMock()
    proc.pid = 12345
    proc.poll.return_value = None
    proc.returncode = None
    factory = MagicMock(return_value=proc)
    monkeypatch.setattr(subprocess, "Popen", factory)
    return factory


@pytest.fixture
def isolated_manifest_locks() -> Iterator[None]:
    """Clear errorta_corpus.manifest._LOCKS between tests.

    No-op if the module is not importable in this environment.
    """
    try:
        from errorta_corpus import manifest  # type: ignore
    except Exception:
        yield
        return
    locks = getattr(manifest, "_LOCKS", None)
    if isinstance(locks, dict):
        locks.clear()
    yield
    if isinstance(locks, dict):
        locks.clear()


@pytest.fixture
def mock_aiar_pipeline(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Install a MagicMock as the judge router's pipeline override.

    F001-SEAM routes resolve ``errorta_query.pipeline.default_pipeline`` at
    request time in production. Route tests inject a MagicMock through the
    explicit ``_pipeline`` override hook that mimics the adapter return shape
    (an ``AnswerResult``-like object exposing ``.answer`` + ``.raw_verdict``).
    Tests opt in by passing this fixture and can override ``return_value``.
    """
    from errorta_app.routes import judge as judge_routes

    fake_result = MagicMock()
    fake_result.answer = "stub answer"
    fake_result.raw_verdict = {
        "rating": "pass",
        "reason": "ok",
        "failure_tags": [],
        "confidence": 0.9,
    }
    fake_result.verdict = None

    pipeline = MagicMock()
    pipeline.answer = MagicMock(return_value=fake_result)
    pipeline.record_grounding = MagicMock(return_value=True)

    monkeypatch.setattr(judge_routes, "_pipeline", pipeline)
    return pipeline


@pytest.fixture
def mock_grounding_store(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Force the route's ``_record_grounding`` to return success.

    With F001-SEAM landed, grounding goes through ``_pipeline.record_grounding``
    (when the pipeline is the AIAR adapter). This fixture pairs with
    ``mock_aiar_pipeline`` and lets tests observe the call site cleanly.
    """
    from errorta_app.routes import judge as judge_routes

    stub = MagicMock(return_value=True)
    monkeypatch.setattr(judge_routes, "_record_grounding", stub)
    return stub


@pytest.fixture
def watch_coordinator_cleanup() -> Iterator[None]:
    """Stop any running watch coordinator after the test."""
    yield
    try:
        from errorta_watch import coordinator  # type: ignore
    except Exception:
        return
    for fn_name in ("stop_all", "shutdown", "stop"):
        fn: Any = getattr(coordinator, fn_name, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass
            break
