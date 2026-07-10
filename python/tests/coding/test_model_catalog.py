from pathlib import Path

from errorta_council.coding.model_catalog import (
    default_entry,
    load_catalog,
    load_overrides,
    save_overrides,
)


def test_catalog_keeps_capability_and_cost_independent() -> None:
    assert default_entry("claude_cli.opus").capability_tier == "strong"
    assert default_entry("claude_cli.opus").cost_tier == 1
    assert default_entry("anthropic.haiku").capability_tier == "light"
    assert default_entry("anthropic.haiku").cost_tier == 2


def test_catalog_override_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    save_overrides({"custom.unknown": {"capability_tier": "strong", "cost_tier": 4}}, path)
    assert load_overrides(path)["custom.unknown"]["cost_tier"] == 4
    entry = load_catalog(["custom.unknown"], path)["custom.unknown"]
    assert entry.capability_tier == "strong"
    assert entry.cost_tier == 4
    assert entry.tiers_unset is False
