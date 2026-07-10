"""F129 model catalog: F127 capability plus an independent cost axis."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .model_tier import LIGHT, MID, STRONG, tier_for_route

CAPABILITY_TIERS = (LIGHT, MID, STRONG)


@dataclass(frozen=True)
class ModelCatalogEntry:
    route_id: str
    capability_tier: str
    cost_tier: int
    size_rank: int
    speed_rank: int
    tiers_unset: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_KNOWN_MARKERS = (
    "haiku", "mini", "nano", "flash", "lite", "sonnet", "opus", "gpt-",
    "gemini", "grok", "composer", "codex", "qwen", "gemma", "mistral",
    "llama", "thinking", "high", "low", "max",
)


def provider_class(route_id: str) -> str:
    rid = str(route_id or "").strip()
    return rid.split(".", 1)[0] if "." in rid else rid


def default_cost_tier(route_id: str) -> int:
    provider = provider_class(route_id)
    low = route_id.lower()
    if provider in {"local", "fake"}:
        return 0
    if provider in {"claude_cli", "codex_cli", "cursor_cli"}:
        return 1
    if any(token in low for token in ("haiku", "mini", "nano", "flash", "lite")):
        return 2
    if any(token in low for token in ("opus", "o1", "o3", "xhigh", "-max")):
        return 4
    if provider in {"anthropic", "openai", "google", "custom"}:
        return 3
    return 3


def _default_hints(route_id: str, capability: str) -> tuple[int, int]:
    low = route_id.lower()
    if any(token in low for token in ("nano", "mini", "haiku", "flash", "lite", "3b", "7b")):
        return 0, 0
    if any(token in low for token in ("opus", "xhigh", "-max", "70b")):
        return 2, 2
    rank = {LIGHT: 0, MID: 1, STRONG: 2}.get(capability, 1)
    return rank, rank


def default_entry(route_id: str) -> ModelCatalogEntry:
    capability = tier_for_route(route_id)
    size, speed = _default_hints(route_id, capability)
    known = provider_class(route_id) in {
        "local", "fake", "claude_cli", "codex_cli", "cursor_cli",
        "anthropic", "openai", "google", "custom",
    } and any(marker in route_id.lower() for marker in _KNOWN_MARKERS)
    return ModelCatalogEntry(
        route_id=route_id,
        capability_tier=capability,
        cost_tier=default_cost_tier(route_id),
        size_rank=size,
        speed_rank=speed,
        tiers_unset=not known,
    )


def overrides_path() -> Path:
    from errorta_app.paths import errorta_home

    path = errorta_home() / "council" / "model-catalog-overrides.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_overrides(path: Path | None = None) -> dict[str, dict[str, Any]]:
    source = path or overrides_path()
    try:
        raw = json.loads(source.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, dict[str, Any]] = {}
    for route_id, value in raw.items():
        if not isinstance(route_id, str) or not isinstance(value, dict):
            continue
        try:
            clean[route_id] = normalize_override(value)
        except ValueError:
            continue
    return clean


def normalize_override(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "capability_tier" in raw:
        capability = str(raw["capability_tier"]).lower()
        if capability not in CAPABILITY_TIERS:
            raise ValueError("capability_tier must be light, mid, or strong")
        out["capability_tier"] = capability
    for key, minimum, maximum in (
        ("cost_tier", 0, 4), ("size_rank", 0, 100), ("speed_rank", 0, 100),
    ):
        if key in raw:
            value = int(raw[key])
            if not minimum <= value <= maximum:
                raise ValueError(f"{key} must be in [{minimum}, {maximum}]")
            out[key] = value
    return out


def save_overrides(overrides: dict[str, dict[str, Any]], path: Path | None = None) -> None:
    destination = path or overrides_path()
    clean = {str(route): normalize_override(value) for route, value in overrides.items()}
    fd, tmp_name = tempfile.mkstemp(prefix=".model-catalog-", suffix=".json",
                                    dir=str(destination.parent), text=True)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(clean, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, destination)
        os.chmod(destination, 0o600)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def load_catalog(route_ids: list[str], path: Path | None = None) -> dict[str, ModelCatalogEntry]:
    overrides = load_overrides(path)
    out: dict[str, ModelCatalogEntry] = {}
    for route_id in route_ids:
        base = default_entry(route_id)
        override = overrides.get(route_id, {})
        out[route_id] = ModelCatalogEntry(
            route_id=route_id,
            capability_tier=str(override.get("capability_tier", base.capability_tier)),
            cost_tier=int(override.get("cost_tier", base.cost_tier)),
            size_rank=int(override.get("size_rank", base.size_rank)),
            speed_rank=int(override.get("speed_rank", base.speed_rank)),
            tiers_unset=base.tiers_unset and not bool(override),
        )
    return out


def catalog_revision(catalog: dict[str, ModelCatalogEntry]) -> str:
    payload = [catalog[key].to_dict() for key in sorted(catalog)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


__all__ = [
    "ModelCatalogEntry", "CAPABILITY_TIERS", "catalog_revision", "default_cost_tier",
    "default_entry", "load_catalog", "load_overrides", "normalize_override",
    "overrides_path", "provider_class", "save_overrides",
]
