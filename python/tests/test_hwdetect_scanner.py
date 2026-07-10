"""Tests for errorta_hwdetect.scanner."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def scanner_module(tmp_errorta_home: Path, mock_psutil: MagicMock, monkeypatch: pytest.MonkeyPatch):
    """Import scanner with the hardware.json path resolved inside tmp_errorta_home.

    Post F-INFRA-12, scanner resolves the path lazily through
    ``errorta_app.paths.hardware_json_path()``, which honors ``ERRORTA_HOME``
    and otherwise falls back to ``Path.home() / .errorta``. The
    ``tmp_errorta_home`` fixture already pins ``HOME`` to the tmp dir, so
    no further patching is required.
    """
    monkeypatch.setitem(sys.modules, "pynvml", None)

    from errorta_hwdetect import scanner

    return scanner


def test_scan_returns_expected_keys(scanner_module) -> None:
    report = scanner_module.scan()
    for key in ("cpu", "ram_gb", "gpu", "disk_free_gb", "os", "recommendation", "scanned_at"):
        assert key in report
    assert isinstance(report["cpu"], dict)
    assert isinstance(report["gpu"], dict)


def test_scan_falls_back_when_pynvml_missing(scanner_module, monkeypatch: pytest.MonkeyPatch) -> None:
    # Force ImportError for pynvml inside gpu_nvidia.detect
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):
        if name == "pynvml":
            raise ImportError("pynvml not available")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    report = scanner_module.scan()
    # Without pynvml/apple/amd success the fallback stub is returned (vendor either real or "none").
    assert "vendor" in report["gpu"]


def test_scan_persists_to_home_hardware_json(scanner_module, tmp_errorta_home: Path) -> None:
    report = scanner_module.scan()
    path = tmp_errorta_home / ".errorta" / "hardware.json"
    assert path.exists()
    on_disk = json.loads(path.read_text())
    assert on_disk["ram_gb"] == report["ram_gb"]
    assert on_disk["scanned_at"] == report["scanned_at"]


def test_load_persisted_returns_none_when_absent(scanner_module, tmp_errorta_home: Path) -> None:
    path = tmp_errorta_home / ".errorta" / "hardware.json"
    if path.exists():
        path.unlink()
    assert scanner_module.load_persisted() is None


def test_load_persisted_reads_existing_file(scanner_module, tmp_errorta_home: Path) -> None:
    path = tmp_errorta_home / ".errorta" / "hardware.json"
    payload = {"ram_gb": 32.0, "gpu": {"vendor": "test"}, "scanned_at": "2026-01-01T00:00:00+00:00"}
    path.write_text(json.dumps(payload))
    loaded = scanner_module.load_persisted()
    assert loaded == payload


def test_load_persisted_returns_none_on_corrupt_json(scanner_module, tmp_errorta_home: Path) -> None:
    path = tmp_errorta_home / ".errorta" / "hardware.json"
    path.write_text("{not valid json")
    assert scanner_module.load_persisted() is None


def test_scan_ram_gb_matches_mocked_psutil(scanner_module, mock_psutil: MagicMock) -> None:
    report = scanner_module.scan()
    # mock_psutil sets total = 16 * 1024**3 → 16.0 GB
    assert report["ram_gb"] == 16.0


def test_scan_includes_recommendation_dict(scanner_module) -> None:
    report = scanner_module.scan()
    rec = report["recommendation"]
    assert isinstance(rec, dict)
