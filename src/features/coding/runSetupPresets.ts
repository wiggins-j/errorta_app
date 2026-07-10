// F121 Part B — the two built-in readiness-gate presets (D2). Each preset is a
// named bundle of the resolved config the gate pre-fills. "Custom" is NOT a
// stored preset — it's the label the gate shows once the user edits any field
// away from the active preset (the values are kept). Numbers are deliberately
// conservative; sticky defaults absorb the user's real preference over time.
//
// THE COVERED-SETTINGS CONTRACT: every key a run uses must appear here so a new
// knob can't be added without surfacing it in the gate (spec criterion 9). The
// `RUN_SETUP_COVERED_FIELDS` list below is asserted against the presets + the
// gate fields by a test — adding a field to RunSetupConfig without listing it
// (and rendering it) fails.

import type { RunSetupConfig } from "../../lib/api/coding";

export type PresetName = "careful" | "autonomous" | "custom";

// The fields the gate covers, grouped for the UI + the coverage test. Excludes
// `teamRoomId` (rendered by the dedicated Team selector, not a preset value) and
// `groundingEnabled` (a project property surfaced read-only in the gate).
export const RUN_SETUP_COVERED_FIELDS = [
  "governanceMode",
  "blockOnProblems",
  "humanCodeApproval",
  "maxReviewRounds",
  "checkpointCadence",
  "checkpointN",
  "guardrailEnabled",
  "maxIterations",
  "maxModelCalls",
  "maxParallelWorkers",
  "memberFailureLimit",
  "preflightEnabled",
] as const;

export type CoveredField = (typeof RUN_SETUP_COVERED_FIELDS)[number];

// Careful — watch it closely. Light governance with showstoppers blocking,
// per-milestone human code approval, milestone checkpoints, guardrail on, and
// conservative spend caps.
export const CAREFUL_PRESET: RunSetupConfig = {
  governanceMode: "light",
  blockOnProblems: true,
  humanCodeApproval: "per_milestone",
  maxReviewRounds: 2,
  checkpointCadence: "per_milestone",
  checkpointN: 5,
  guardrailEnabled: true,
  maxIterations: 50,
  maxModelCalls: 200,
  maxParallelWorkers: 1,
  memberFailureLimit: 2,
  preflightEnabled: true,
};

// Autonomous — let it run. Light governance with problems auto-resolved,
// final-only approval, looser caps, guardrail still on (safety floor).
export const AUTONOMOUS_PRESET: RunSetupConfig = {
  governanceMode: "light",
  blockOnProblems: false,
  humanCodeApproval: "final_only",
  maxReviewRounds: 3,
  checkpointCadence: "off",
  checkpointN: 5,
  guardrailEnabled: true,
  maxIterations: 200,
  maxModelCalls: null,
  maxParallelWorkers: null,
  memberFailureLimit: 3,
  preflightEnabled: true,
};

export const PRESETS: Record<Exclude<PresetName, "custom">, RunSetupConfig> = {
  careful: CAREFUL_PRESET,
  autonomous: AUTONOMOUS_PRESET,
};

// The covered fields a preset must pin — used by the coverage test to assert the
// presets fully specify the run config.
export function presetFor(name: Exclude<PresetName, "custom">): RunSetupConfig {
  return PRESETS[name];
}

// Detect whether a config equals a preset on the covered fields (so the gate can
// label "Careful"/"Autonomous" vs "Custom" honestly).
export function matchPreset(cfg: RunSetupConfig): PresetName {
  for (const name of ["careful", "autonomous"] as const) {
    const preset = PRESETS[name];
    const matches = RUN_SETUP_COVERED_FIELDS.every(
      (f) => (cfg[f] ?? null) === (preset[f] ?? null),
    );
    if (matches) return name;
  }
  return "custom";
}

// Fill every covered setting that is `undefined` with the Careful-preset value,
// so the gate's state is always concrete. WHY THIS EXISTS: the gate's
// <select>/<input> used display-only fallbacks (`value={config.x ?? default}`)
// while the wire serializer skips undefined fields — so a setting the user SAW
// (e.g. governance "light") but never explicitly touched was silently NOT
// applied on confirm, and the backend kept its own default (governance "off").
// Normalizing the seed makes what the gate shows exactly what gets sent +
// applied + saved as the sticky default. `teamRoomId` / `groundingEnabled` are
// intentionally NOT covered (the Team selector / a read-only project property).
export function withRunSetupDefaults(cfg: RunSetupConfig): RunSetupConfig {
  const out: RunSetupConfig = { ...cfg };
  for (const f of RUN_SETUP_COVERED_FIELDS) {
    if (out[f] === undefined) {
      (out as Record<string, unknown>)[f] = CAREFUL_PRESET[f];
    }
  }
  return out;
}
