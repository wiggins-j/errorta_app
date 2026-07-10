// F134 — copy-consistency guard: the F132 per-panel blurbs and the Quick Start
// guide must share ONE copy source (quickStartContent.tsx) so they can't drift.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

import {
  PANEL_BLURBS,
  QUICK_START_SECTIONS,
  QUICK_START_TOC,
} from "./quickStartContent";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../../..");

describe("PANEL_BLURBS", () => {
  it("has a non-empty blurb for each Knowledge panel", () => {
    for (const key of ["corpus", "briefs", "watch"] as const) {
      expect(PANEL_BLURBS[key], key).toBeTruthy();
      expect(PANEL_BLURBS[key].length).toBeGreaterThan(20);
    }
  });

  // The three panels must source their `blurb` from the shared constant, not an
  // inline string — otherwise the guide and the header can silently diverge.
  const PANELS = {
    corpus: "src/features/corpus/index.tsx",
    briefs: "src/features/briefs/index.tsx",
    watch: "src/features/watch/index.tsx",
  } as const;

  for (const [key, rel] of Object.entries(PANELS)) {
    it(`${key} panel sources its blurb from PANEL_BLURBS (no inline string)`, () => {
      const src = readFileSync(resolve(root, rel), "utf8");
      expect(src, `${rel} should reference PANEL_BLURBS.${key}`).toContain(
        `PANEL_BLURBS.${key}`,
      );
      expect(
        src,
        `${rel} should not hardcode an inline blurb= string literal`,
      ).not.toMatch(/blurb="/);
    });
  }
});

describe("QUICK_START_SECTIONS", () => {
  it("covers all three build paths plus use-your-corpus", () => {
    const ids = QUICK_START_SECTIONS.map((s) => s.id);
    for (const id of [
      "build-from-files",
      "build-from-brief",
      "folder-watcher",
      "use-your-corpus",
    ]) {
      expect(ids).toContain(id);
    }
  });

  it("has a TOC entry per section, in order", () => {
    expect(QUICK_START_TOC.map((t) => t.id)).toEqual(
      QUICK_START_SECTIONS.map((s) => s.id),
    );
  });

  it("has unique section ids", () => {
    const ids = QUICK_START_SECTIONS.map((s) => s.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});
