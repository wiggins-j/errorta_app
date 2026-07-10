// F132 — guard against reintroducing the bundler-evading Tauri-invoke shim.
//
// The `new Function("s","return import(s)")` trick dodges Vite/Rolldown so
// `@tauri-apps/api/core` (and plugin modules) never ship in the packaged app —
// every invoke then silently no-ops in production (the SSH-test "browser-dev"
// bug, residency switching, CLI login, diagnostics export, the updater). All
// invoke/plugin loads must use a bundler-RESOLVED `await import(...)`.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");

// Every file that used to carry the shim, plus the shared helper.
const GUARDED = [
  "src/lib/sidecarPort.ts",
  "src/lib/api/providerKeys.ts",
  "src/lib/api/updater.ts",
  "src/features/onboarding/StepResidency.tsx",
  "src/features/shell/DataResidencyCard.tsx",
  "src/features/shell/AppShellSettings.tsx",
  "src/features/shell/DiagnosticsExport.tsx",
  "src/features/shell/FilePickerDialog.tsx",
];

// Strip comments so the explanatory notes that *mention* the old shim don't
// trip the guard — only real code counts. (None of these files put
// `new Function(` inside a string literal, so this is sufficient.)
function stripComments(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\/\/.*$/gm, "");
}

describe("Tauri invoke loads through a bundler-resolved import", () => {
  it("no source uses the `new Function` import shim (code, not comments)", () => {
    for (const rel of GUARDED) {
      const code = stripComments(readFileSync(resolve(root, rel), "utf8"));
      expect(code, `${rel} must not use the new Function import shim`).not.toMatch(
        /new Function\s*\(/,
      );
    }
  });
});
