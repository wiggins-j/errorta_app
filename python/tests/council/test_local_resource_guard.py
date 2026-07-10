from __future__ import annotations

import pytest

from errorta_council.resources import (
    AdmissionResult,
    LocalResourceGuard,
    RoomResourcePreview,
)


class _FakeGateway:
    def __init__(self, *, reachable: bool, installed: list[str]) -> None:
        self._reachable = reachable
        self._installed = list(installed)

    async def is_reachable(self) -> bool:
        return self._reachable

    async def list_installed_models(self) -> list[str]:
        return list(self._installed)


@pytest.mark.asyncio
async def test_admit_ok_when_ollama_reachable_and_model_installed() -> None:
    guard = LocalResourceGuard(
        gateway=_FakeGateway(reachable=True, installed=["llama3.2:1b"]),
        hardware_scan_present=True,
    )
    result = await guard.admit(
        proposal=type("P", (), {"member_id": "m1"})(),
        member={"id": "m1", "provider": "local", "model": "llama3.2:1b"},
    )
    assert result.admitted is True
    assert result.classification == "fits"
    assert result.reason_code is None


@pytest.mark.asyncio
async def test_admit_blocks_when_ollama_unreachable() -> None:
    guard = LocalResourceGuard(
        gateway=_FakeGateway(reachable=False, installed=["llama3.2:1b"]),
        hardware_scan_present=True,
    )
    result = await guard.admit(
        proposal=type("P", (), {"member_id": "m1"})(),
        member={"id": "m1", "provider": "local", "model": "llama3.2:1b"},
    )
    assert result.admitted is False
    assert result.classification == "unavailable"
    assert result.reason_code == "local_provider_unavailable"


@pytest.mark.asyncio
async def test_admit_blocks_when_model_missing() -> None:
    guard = LocalResourceGuard(
        gateway=_FakeGateway(reachable=True, installed=[]),
        hardware_scan_present=True,
    )
    result = await guard.admit(
        proposal=type("P", (), {"member_id": "m1"})(),
        member={"id": "m1", "provider": "local", "model": "nope:1b"},
    )
    assert result.admitted is False
    assert result.classification == "unavailable"
    assert result.reason_code == "local_model_missing"


@pytest.mark.asyncio
async def test_admit_warns_when_hardware_scan_missing_but_still_admits() -> None:
    guard = LocalResourceGuard(
        gateway=_FakeGateway(reachable=True, installed=["llama3.2:1b"]),
        hardware_scan_present=False,
    )
    result = await guard.admit(
        proposal=type("P", (), {"member_id": "m1"})(),
        member={"id": "m1", "provider": "local", "model": "llama3.2:1b"},
    )
    assert result.admitted is True
    assert result.classification == "warning"
    assert "hardware_scan" in " ".join(result.warnings)


@pytest.mark.asyncio
async def test_admit_skips_resource_check_for_fake_provider() -> None:
    guard = LocalResourceGuard(
        gateway=_FakeGateway(reachable=False, installed=[]),
        hardware_scan_present=True,
    )
    result = await guard.admit(
        proposal=type("P", (), {"member_id": "m1"})(),
        member={"id": "m1", "provider": "fake", "model": "stub"},
    )
    assert result.admitted is True
    assert result.classification == "fits"


@pytest.mark.asyncio
async def test_no_auto_pull(monkeypatch) -> None:
    """Invariant 4 prep: never pull a missing model automatically."""
    pull_called = {"n": 0}

    async def _bomb_pull(*a, **k) -> None:
        pull_called["n"] += 1
        raise AssertionError("Phase 1 must not auto-pull")

    guard = LocalResourceGuard(
        gateway=_FakeGateway(reachable=True, installed=[]),
        hardware_scan_present=True,
        ollama_pull=_bomb_pull,
    )
    await guard.admit(
        proposal=type("P", (), {"member_id": "m1"})(),
        member={"id": "m1", "provider": "local", "model": "nope:1b"},
    )
    assert pull_called["n"] == 0


@pytest.mark.asyncio
async def test_preview_summarizes_per_member_status() -> None:
    guard = LocalResourceGuard(
        gateway=_FakeGateway(reachable=True, installed=["llama3.2:1b"]),
        hardware_scan_present=True,
    )
    preview = await guard.preview(
        room={
            "id": "rm",
            "members": [
                {"id": "m1", "enabled": True, "provider": "local", "model": "llama3.2:1b"},
                {"id": "m2", "enabled": True, "provider": "local", "model": "nope:1b"},
            ],
        }
    )
    assert isinstance(preview, RoomResourcePreview)
    assert preview.ollama_reachable is True
    statuses = {p["member_id"]: p for p in preview.per_member}
    assert statuses["m1"]["classification"] == "fits"
    assert statuses["m2"]["reason_code"] == "local_model_missing"
