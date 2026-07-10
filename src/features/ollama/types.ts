// F003 — shared types for the Ollama feature surface.

export type InstallPhase =
  | "idle"
  | "downloading"
  | "verifying"
  | "installing"
  | "starting"
  | "ready"
  | "error";

export interface OllamaHealth {
  reachable: boolean;
  host: string;
  version: string | null;
  error: string | null;
  managed_by_errorta: boolean;
  needs_install: boolean;
  platform_supported: boolean;
}

export interface OllamaInstallProgress {
  phase: InstallPhase;
  percent: number;
  message: string;
  error: string | null;
  started_at: number | null;
  ended_at: number | null;
  host: string | null;
  version: string | null;
}

export interface OllamaSettings {
  host: string;
  storage_path: string | null;
  managed_by_errorta: boolean;
  installed_version: string | null;
  last_install_at: string | null;
  expect_running: boolean;
}

export interface OllamaSettingsUpdate {
  host?: string;
  storage_path?: string | null;
}

export interface OllamaRestartResult {
  attempted: boolean;
  succeeded: boolean;
  reason: string;
}
