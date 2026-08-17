"""Slice 1 §3 — host-side OFL/permissively-licensed design asset library.

A vendored directory of font families + a stroke-icon set, each with its license
committed alongside, plus a ``manifest.json`` describing them. The Designer's
authoring prompt is shown this manifest; the ``design_spec`` ``assets`` block picks
family/icon ids from it; the materialize DEV task (§4) copies the chosen files into
the project repo. Nothing here ever touches the network — that is what makes
non-system typography possible in the network-off sandbox.

Pure, read-only, egress-free (Council inv. 3): filesystem reads only.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_LIBRARY_DIR = Path(__file__).parent / "assets" / "design_library"
_MANIFEST = _LIBRARY_DIR / "manifest.json"


def library_root() -> Path:
    """Absolute path to the vendored asset library directory."""
    return _LIBRARY_DIR


@lru_cache(maxsize=1)
def load_manifest() -> dict[str, Any]:
    """The parsed ``manifest.json`` (cached). Raises ``FileNotFoundError`` if the
    library was not vendored."""
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def font_families() -> list[dict[str, Any]]:
    return list(load_manifest().get("font_families") or [])


def icon_sets() -> list[dict[str, Any]]:
    return list(load_manifest().get("icon_sets") or [])


def referenced_files() -> list[Path]:
    """Every asset/license file the manifest references, as absolute paths.

    The single source of truth for "does every referenced file exist?" — used by
    the acceptance test and by the materialize task when it copies chosen assets."""
    out: list[Path] = []
    manifest = load_manifest()
    for family in manifest.get("font_families") or []:
        for rel in list(family.get("files") or []) + [family.get("license_file", "")]:
            if rel:
                out.append(_LIBRARY_DIR / rel)
    for icon_set in manifest.get("icon_sets") or []:
        for rel in list(icon_set.get("files") or []) + [icon_set.get("license_file", "")]:
            if rel:
                out.append(_LIBRARY_DIR / rel)
    return out


def manifest_summary_for_prompt(*, max_families: int | None = None) -> str:
    """A compact, model-facing summary of the library for the Designer's prompt:
    one line per family (id + personality tags + weights) and the icon set ids."""
    families = font_families()
    if max_families is not None:
        families = families[:max_families]
    lines = ["Available OFL/permissive font families (pick ids for the design_spec "
             "assets block):"]
    for fam in families:
        tags = ", ".join(fam.get("personality_tags") or [])
        weights = ", ".join(str(w) for w in (fam.get("weights") or []))
        lines.append(f"- {fam['id']}: {tags} (weights: {weights})")
    for icon_set in icon_sets():
        lines.append(f"Icon set: {icon_set['id']} ({icon_set.get('style', 'stroke')}, "
                     f"{len(icon_set.get('files') or [])} icons)")
    return "\n".join(lines)


__all__ = [
    "library_root", "load_manifest", "font_families", "icon_sets",
    "referenced_files", "manifest_summary_for_prompt",
]
