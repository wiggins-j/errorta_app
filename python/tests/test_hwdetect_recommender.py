"""Tests for errorta_hwdetect.recommender.

Hermetic: the recommendation table is bundled with the module and read
from disk; no network or subprocess calls are involved.
"""
from __future__ import annotations

import pytest

from errorta_hwdetect.recommender import recommend


def test_top_tier_nvidia_24gb_picks_largest_model() -> None:
    result = recommend(vram_gb=24, vendor="nvidia", unified_memory=False, ram_gb=64)
    # 24 GB fits the 32B (20 GB) but not the 70B (40 GB).
    assert result["primary"]["id"] == "qwen2.5:32b"
    assert result["primary"]["compatible"] is True
    # 70B stretch target should surface as "capable".
    assert result["capable"] is not None
    assert result["capable"]["id"] == "llama3.1:70b"
    # Faster should be a smaller compatible model.
    assert result["faster"] is not None
    assert result["faster"]["params_b"] < result["primary"]["params_b"]
    assert result["available_vram_gb"] == 24


def test_no_gpu_falls_back_to_smallest_cpu_friendly_tier() -> None:
    result = recommend(vram_gb=0, vendor="none", unified_memory=False, ram_gb=8)
    # No compatible model — falls back to first (smallest) entry with CPU rationale.
    assert result["primary"]["id"] == "qwen2.5:3b"
    assert result["primary"]["compatible"] is False
    assert "CPU" in result["rationale"]
    assert result["faster"] is None
    # All models are incompatible at 0 VRAM; "capable" picks smallest VRAM target.
    assert result["capable"] is not None
    assert result["capable"]["vram_gb"] == 3


def test_apple_unified_memory_branch_uses_ram_budget() -> None:
    result = recommend(vram_gb=0, vendor="apple", unified_memory=True, ram_gb=16)
    # Effective budget = 16 * 0.7 = 11.2 GB.
    assert result["available_vram_gb"] == pytest.approx(11.2)
    # 11.2 GB fits the 8B (7 GB) but not the 22B (14 GB).
    assert result["primary"]["id"] == "llama3.1:8b"
    assert "unified memory" in result["rationale"]
    assert result["capable"] is not None
    assert result["capable"]["vram_gb"] == 14


@pytest.mark.parametrize(
    "vram_gb,vendor,expected_primary_id",
    [
        # Just at the 3 GB cutoff — smallest model fits.
        (3, "nvidia", "llama3.2:3b"),
        # Just below the 6 GB 7B cutoff — only 3B tiers fit.
        (5, "nvidia", "llama3.2:3b"),
        # At the 6 GB 7B cutoff.
        (6, "nvidia", "qwen2.5:7b"),
        # 7 GB hits the 8B cutoff.
        (7, "nvidia", "llama3.1:8b"),
        # 14 GB hits the 22B cutoff.
        (14, "nvidia", "mistral-small:22b"),
        # 15 GB hits the 24B cutoff.
        (15, "nvidia", "mistral-small3.1"),
        # 40 GB unlocks the 70B top tier.
        (40, "nvidia", "llama3.1:70b"),
    ],
)
def test_tier_cutoff_boundaries(vram_gb: float, vendor: str, expected_primary_id: str) -> None:
    result = recommend(
        vram_gb=vram_gb, vendor=vendor, unified_memory=False, ram_gb=32
    )
    assert result["primary"]["id"] == expected_primary_id
    assert result["primary"]["compatible"] is True


def test_amd_vendor_excluded_from_apple_only_top_tiers() -> None:
    # AMD has plenty of VRAM but 32B/70B are nvidia+apple only.
    result = recommend(vram_gb=48, vendor="amd", unified_memory=False, ram_gb=64)
    # Largest compatible for AMD is the 24B Mistral Small 3.1.
    assert result["primary"]["id"] == "mistral-small3.1"
    # 32B/70B should be in incompatible with vendor reason.
    incompat_ids = {m["id"] for m in result["incompatible"]}
    assert "qwen2.5:32b" in incompat_ids
    assert "llama3.1:70b" in incompat_ids
    for m in result["incompatible"]:
        if m["id"] in {"qwen2.5:32b", "llama3.1:70b"}:
            assert m["incompatible_reason"] is not None
            assert "amd" in m["incompatible_reason"]


def test_none_vendor_sentinel_handled_gracefully() -> None:
    # "none" is the documented sentinel for "no GPU vendor detected".
    result = recommend(vram_gb=4, vendor="none", unified_memory=False, ram_gb=8)
    # 3B models list "none" as a compatible vendor, so they fit at 4 GB VRAM.
    assert result["primary"]["id"] == "llama3.2:3b"
    assert result["primary"]["compatible"] is True
    assert isinstance(result["all"], list)
    assert result["table_version"] == "0.1.0-snapshot"


def test_result_shape_includes_labels_and_table_version() -> None:
    result = recommend(vram_gb=24, vendor="nvidia", unified_memory=False, ram_gb=64)
    primary = result["primary"]
    for key in (
        "id",
        "label",
        "install_label",
        "vram_label",
        "tok_label",
        "compatible",
    ):
        assert key in primary
    assert "GB download" in primary["install_label"]
    assert "tok/s" in primary["tok_label"]
    assert result["table_version"] == "0.1.0-snapshot"
    assert isinstance(result["all"], list) and len(result["all"]) >= 8
