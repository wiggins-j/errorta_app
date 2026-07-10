// F-INFRA-06 — Diagnostics bundle export client.

import { getJSON, postJSON } from "../api";

export interface RedactionManifest {
  rules: Record<string, number>;
  generated_at: string;
}

export interface DiagnosticsExportResult {
  path: string;
  sha256: string;
  redaction_manifest: RedactionManifest;
  files: string[];
}

export function exportDiagnostics(
  destPath: string,
  userNote: string,
): Promise<DiagnosticsExportResult> {
  return postJSON<DiagnosticsExportResult>("/diagnostics/export", {
    dest_path: destPath,
    user_note: userNote,
  });
}

// F048 — sidecar lifecycle.
export interface SidecarLifecycle {
  component: string;
  pid: number;
  sidecar_version: string;
  residency_mode: string;
  config_signature: string;
  signature_inputs: Record<string, unknown>;
  recent_log_tail?: {
    lines: string[];
    redaction_counts: Record<string, number>;
    truncated: boolean;
  };
}

export function getSidecarLifecycle(): Promise<SidecarLifecycle> {
  return getJSON<SidecarLifecycle>("/diagnostics/lifecycle?tail_lines=0");
}
