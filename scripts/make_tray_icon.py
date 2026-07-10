#!/usr/bin/env python3
"""Generate the macOS menu-bar tray icon for Errorta.

macOS menu-bar icons are TEMPLATE images: macOS uses only the alpha channel and
tints the shape (black in light bars, white in dark). So this renders a
monochrome cloud silhouette with the letter "E" knocked OUT of it, on a fully
transparent background — no colored fill, no opaque square.

The glyph is TIGHT-CROPPED to its bounding box with only a hair of margin so the
cloud fills the menu-bar height (a padded square scaled down to ~18pt left the
cloud tiny). Rendered at high res then downscaled with LANCZOS for crisp edges.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SS = 1024            # supersample canvas
MARGIN_FRAC = 0.05   # tiny uniform breathing room around the cropped glyph
OUT = Path(__file__).resolve().parent.parent / "src-tauri" / "icons"


def build_glyph() -> Image.Image:
    """RGBA glyph: black cloud with the 'E' knocked out; transparent elsewhere."""
    m = Image.new("L", (SS, SS), 0)
    d = ImageDraw.Draw(m)

    def disc(cx, cy, r):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)

    # --- cloud silhouette: a flat-bottomed body with THREE distinct top bumps
    # (kept distinct so it still reads as a cloud, not an oval, at ~22px) -----
    d.rounded_rectangle([214, 506, 810, 742], radius=118, fill=255)  # wide flat body
    disc(360, 470, 138)   # left bump
    disc(512, 430, 166)   # center bump (tallest)
    disc(664, 470, 138)   # right bump
    disc(248, 596, 96)    # round off the lower-left shoulder
    disc(776, 596, 96)    # round off the lower-right shoulder

    # --- knock out a bold "E" (alpha back to 0), sized big within the cloud -
    e = 0
    x0, y_top, y_bot = 398, 384, 716     # taller, bolder E
    stem_w = 92
    arm_long = 232
    arm_mid = 184
    arm_h = 80
    d.rectangle([x0, y_top, x0 + stem_w, y_bot], fill=e)                 # stem
    d.rectangle([x0, y_top, x0 + arm_long, y_top + arm_h], fill=e)       # top arm
    midy = (y_top + y_bot) // 2 - arm_h // 2
    d.rectangle([x0, midy, x0 + arm_mid, midy + arm_h], fill=e)          # mid arm
    d.rectangle([x0, y_bot - arm_h, x0 + arm_long, y_bot], fill=e)       # bottom arm

    rgba = Image.merge("RGBA", (Image.new("L", (SS, SS), 0),) * 3 + (m,))
    # tight-crop to the opaque pixels, then add a small uniform margin
    bbox = m.getbbox()
    glyph = rgba.crop(bbox)
    pad = round(max(glyph.size) * MARGIN_FRAC)
    out = Image.new("RGBA", (glyph.width + 2 * pad, glyph.height + 2 * pad), (0, 0, 0, 0))
    out.alpha_composite(glyph, (pad, pad))
    return out


def main() -> None:
    glyph = build_glyph()
    OUT.mkdir(parents=True, exist_ok=True)
    # Keep the glyph's aspect (wider than tall); the menu bar scales to fit its
    # height. Ship @1x + @2x; tray.rs embeds the @2x.
    for height, name in [(40, "tray-template.png"), (80, "tray-template@2x.png")]:
        w = round(glyph.width * height / glyph.height)
        img = glyph.resize((w, height), Image.LANCZOS)
        img.save(OUT / name)
        print("wrote", OUT / name, img.size)


if __name__ == "__main__":
    main()
