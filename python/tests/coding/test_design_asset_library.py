"""Slice 1 §3/§9 — the host-side asset library acceptance test.

The load-bearing checks: the manifest schema is well-formed, EVERY file it
references actually exists on disk, every font family and icon set carries a real
LICENSE file, and the families span the personality axes. This repo is PUBLIC, so
a manifest that references a font that isn't vendored (or a family with no license)
is a defect the build must catch.
"""
from __future__ import annotations

from errorta_council.coding import design_library

_TTF_MAGIC = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf")


def test_manifest_loads_and_has_schema() -> None:
    manifest = design_library.load_manifest()
    assert manifest.get("schema_version") == 1
    assert manifest.get("font_families"), "at least one font family"
    assert manifest.get("icon_sets"), "at least one icon set"


def test_every_family_is_well_formed() -> None:
    for fam in design_library.font_families():
        assert fam.get("id"), fam
        assert fam.get("weights"), fam["id"]
        assert fam.get("personality_tags"), fam["id"]
        assert fam.get("license"), fam["id"]
        assert fam.get("license_file"), fam["id"]
        assert fam.get("files"), fam["id"]


def test_every_referenced_file_exists() -> None:
    """The core §9 acceptance: every path the manifest names is really on disk."""
    missing = [str(p) for p in design_library.referenced_files() if not p.exists()]
    assert not missing, f"manifest references missing files: {missing}"


def test_every_referenced_font_is_a_real_ttf() -> None:
    root = design_library.library_root()
    for fam in design_library.font_families():
        for rel in fam["files"]:
            path = root / rel
            head = path.read_bytes()[:4]
            assert any(head.startswith(m) for m in _TTF_MAGIC), (rel, head)
            assert path.stat().st_size > 1000, rel


def test_every_family_and_icon_set_has_a_license_file_with_content() -> None:
    root = design_library.library_root()
    for fam in design_library.font_families():
        lic = root / fam["license_file"]
        assert lic.exists() and lic.stat().st_size > 200, fam["id"]
    for icon_set in design_library.icon_sets():
        lic = root / icon_set["license_file"]
        assert lic.exists() and lic.stat().st_size > 100, icon_set["id"]


def test_families_span_the_personality_axes() -> None:
    tags = {t for fam in design_library.font_families()
            for t in fam.get("personality_tags") or []}
    # geometric sans, humanist sans, slab, display serif, mono, display — the axes
    # the Designer chooses among (spec §3).
    for axis in ("geometric-sans", "humanist-sans", "slab-serif", "display-serif",
                 "monospace", "display"):
        assert axis in tags, f"asset library missing the {axis!r} personality axis"
    assert len(design_library.font_families()) >= 10


def test_icon_set_is_real_svgs() -> None:
    root = design_library.library_root()
    for icon_set in design_library.icon_sets():
        assert icon_set["files"], icon_set["id"]
        for rel in icon_set["files"]:
            text = (root / rel).read_text(encoding="utf-8")
            assert "<svg" in text, rel


def test_manifest_summary_renders_for_prompt() -> None:
    summary = design_library.manifest_summary_for_prompt()
    assert "font families" in summary
    assert "poppins" in summary
