"""Tests for errorta_ollama.detect."""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from errorta_ollama import detect


class _FakeClient:
    """Minimal context-manager stand-in for httpx.Client."""

    def __init__(self, tags_response: MagicMock, version_response: MagicMock | None = None,
                 raise_on_tags: Exception | None = None) -> None:
        self._tags = tags_response
        self._version = version_response
        self._raise = raise_on_tags

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *a) -> None:
        return None

    def get(self, url: str) -> MagicMock:
        if self._raise is not None and url.endswith("/api/tags"):
            raise self._raise
        if url.endswith("/api/tags"):
            return self._tags
        return self._version or MagicMock(status_code=404)


def test_probe_reachable_returns_version(monkeypatch: pytest.MonkeyPatch) -> None:
    tags = MagicMock(status_code=200)
    version = MagicMock(status_code=200)
    version.json.return_value = {"version": "0.1.42"}
    monkeypatch.setattr(httpx, "Client", lambda **_: _FakeClient(tags, version))

    out = detect.probe("http://localhost:11434")
    assert out.reachable is True
    assert out.version == "0.1.42"
    assert out.host == "http://localhost:11434"


def test_probe_unreachable_when_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    tags = MagicMock(status_code=503)
    monkeypatch.setattr(httpx, "Client", lambda **_: _FakeClient(tags))

    out = detect.probe("http://localhost:11434")
    assert out.reachable is False
    assert "503" in (out.error or "")


def test_probe_unreachable_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx, "Client",
        lambda **_: _FakeClient(MagicMock(), raise_on_tags=httpx.ConnectError("refused")),
    )
    out = detect.probe("http://localhost:11434")
    assert out.reachable is False
    assert out.error is not None


def test_probe_tolerates_missing_version_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    tags = MagicMock(status_code=200)

    class _NoVer(_FakeClient):
        def get(self, url: str) -> MagicMock:
            if url.endswith("/api/tags"):
                return self._tags
            raise httpx.ConnectError("no version")

    monkeypatch.setattr(httpx, "Client", lambda **_: _NoVer(tags))
    out = detect.probe("http://localhost:11434")
    assert out.reachable is True
    assert out.version is None


def test_wait_until_ready_returns_true_quickly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        detect, "probe",
        MagicMock(return_value=detect.DetectionResult(reachable=True, host="x")),
    )
    assert detect.wait_until_ready("http://localhost:11434", total_timeout=1.0) is True


def test_wait_until_ready_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        detect, "probe",
        MagicMock(return_value=detect.DetectionResult(reachable=False, host="x")),
    )
    # Very short timeout; interval also short so it returns within the test budget.
    assert detect.wait_until_ready("http://localhost:11434", total_timeout=0.05, interval=0.01) is False
