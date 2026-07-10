"""Model recommendation engine.

Given detected hardware (vram, vendor, ram), return three tiers
(Recommended / Faster / More Capable) plus the list of models flagged
incompatible with explicit numeric reasons.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_TABLE_PATH = Path(__file__).with_name("recommendations.json")


def _load_table() -> dict[str, Any]:
    return json.loads(_TABLE_PATH.read_text())


def _effective_vram(vram_gb: float, unified_memory: bool, ram_gb: float) -> float:
    """On Apple unified-memory systems, reserve ~25% of RAM for the OS."""
    if unified_memory:
        # Leave headroom for the OS + other apps. Practical ceiling is
        # roughly 70-75% of total unified memory.
        return round(ram_gb * 0.7, 1)
    return vram_gb


def _model_tier(model: dict[str, Any], available_vram: float, vendor: str) -> dict[str, Any]:
    install_label = f"{model['install_gb']} GB download"
    vram_label = f"~{model['vram_gb']} GB VRAM"
    tok_label = f"~{model['tok_s_low']}-{model['tok_s_high']} tok/s"
    vendor_ok = vendor in model["vendors"] or vendor == "none"
    fits = model["vram_gb"] <= available_vram and vendor_ok
    reason = None
    if not vendor_ok:
        reason = f"{model['label']} not supported on {vendor} GPUs in v0.1."
    elif model["vram_gb"] > available_vram:
        reason = (
            f"{model['label']} needs ~{model['vram_gb']} GB VRAM, "
            f"you have ~{available_vram:g} GB available."
        )
    return {
        "id": model["id"],
        "label": model["label"],
        "params_b": model["params_b"],
        "quant": model["quant"],
        "vram_gb": model["vram_gb"],
        "install_gb": model["install_gb"],
        "tok_s_low": model["tok_s_low"],
        "tok_s_high": model["tok_s_high"],
        "install_label": install_label,
        "vram_label": vram_label,
        "tok_label": tok_label,
        "compatible": fits,
        "incompatible_reason": reason,
    }


def recommend(
    *,
    vram_gb: float,
    vendor: str,
    unified_memory: bool,
    ram_gb: float,
) -> dict[str, Any]:
    table = _load_table()
    models = table["models"]
    available = _effective_vram(vram_gb, unified_memory, ram_gb)

    scored = [_model_tier(m, available, vendor) for m in models]
    compatible = [m for m in scored if m["compatible"]]
    incompatible = [m for m in scored if not m["compatible"]]

    # Primary = largest compatible model; Faster = smaller compatible;
    # More Capable = smallest incompatible (the next stretch).
    primary = compatible[-1] if compatible else None
    faster = None
    if primary is not None:
        smaller = [m for m in compatible if m["params_b"] < primary["params_b"]]
        faster = smaller[-1] if smaller else None
    capable = None
    if incompatible:
        # The smallest incompatible model is the closest "stretch" target.
        capable = min(incompatible, key=lambda m: m["vram_gb"])

    if primary is None:
        # No model fits — recommend smallest, flag as CPU-only fallback.
        primary = scored[0]
        rationale = (
            f"No GPU detected or insufficient VRAM ({available:g} GB available). "
            f"{primary['label']} will run on CPU at reduced speed."
        )
    else:
        mem_kind = "unified memory" if unified_memory else "VRAM"
        rationale = (
            f"{available:g} GB {mem_kind} fits {primary['label']} comfortably "
            f"with headroom for context."
        )

    return {
        "available_vram_gb": available,
        "primary": primary,
        "faster": faster,
        "capable": capable,
        "incompatible": incompatible,
        "all": scored,
        "rationale": rationale,
        "table_version": table.get("version"),
    }
