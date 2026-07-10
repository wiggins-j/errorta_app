// F-INFRA-09 Slice 4 — typed client for the Tauri updater commands.
//
// All four functions go through loadTauriInvoke() so they degrade
// gracefully in plain `vite dev` / vitest happy-dom where the Tauri
// command bridge is not attached. In that case the API returns the
// `not_configured` stub identical to what the Rust default-build
// command returns. This keeps UpdatesCard renderable everywhere.
//
// listRollbacks / rollbackTo are wired through to slice 5's Rust
// commands. Until slice 5 lands, the Tauri commands return empty /
// stub payloads — the API layer absorbs that without crashing.
//
// Tauri invoke resolves through the shared, bundler-visible `loadTauriInvoke`
// (src/lib/sidecarPort.ts) so the updater commands actually ship in the packaged
// app. `null` === browser-dev/vitest (returns the not_configured stub).

import { loadTauriInvoke } from "../sidecarPort";

export type UpdateCheckResult =
  | { status: "up_to_date" }
  | { status: "available"; version: string; notes?: string; date?: string }
  | { status: "not_configured"; reason: string }
  | { status: "error"; error: string };

export type InstallResult =
  | { status: "installed"; version?: string }
  | { status: "up_to_date" }
  | { status: "not_configured"; reason: string }
  | { status: "error"; error: string };

export type RollbackEntry = {
  version: string;
  installed_at: string;
  size_bytes: number;
  notes?: string;
  crashed_on_launch?: boolean;
};

export type RollbackResult = { status: "ok" } | { status: "error"; error: string };

export type CrashRecoveryEntry = {
  failed_version: string;
  rolled_back_to: string;
  recorded_at: string;
  error: string;
};

const NOT_CONFIGURED: UpdateCheckResult = {
  status: "not_configured",
  reason: "auto-update activates post-v0.6 signed release",
};

export async function checkForUpdates(): Promise<UpdateCheckResult> {
  const invoke = await loadTauriInvoke();
  if (!invoke) return NOT_CONFIGURED;
  try {
    return await invoke<UpdateCheckResult>("check_for_updates");
  } catch (e) {
    return { status: "error", error: String(e) };
  }
}

export async function installUpdate(): Promise<InstallResult> {
  const invoke = await loadTauriInvoke();
  if (!invoke) {
    return {
      status: "not_configured",
      reason: "auto-update activates post-v0.6 signed release",
    };
  }
  try {
    return await invoke<InstallResult>("install_update");
  } catch (e) {
    return { status: "error", error: String(e) };
  }
}

export async function listRollbacks(): Promise<RollbackEntry[]> {
  const invoke = await loadTauriInvoke();
  if (!invoke) return [];
  try {
    return await invoke<RollbackEntry[]>("list_rollbacks");
  } catch {
    return [];
  }
}

export async function rollbackTo(version: string): Promise<RollbackResult> {
  const invoke = await loadTauriInvoke();
  if (!invoke) {
    return { status: "error", error: "Tauri command bridge unavailable" };
  }
  try {
    await invoke("rollback_to", { version });
    return { status: "ok" };
  } catch (e) {
    return { status: "error", error: String(e) };
  }
}

export async function getCrashRecovery(): Promise<CrashRecoveryEntry | null> {
  const invoke = await loadTauriInvoke();
  if (!invoke) return null;
  try {
    return await invoke<CrashRecoveryEntry | null>("get_crash_recovery");
  } catch {
    return null;
  }
}

export async function dismissCrashRecovery(): Promise<RollbackResult> {
  const invoke = await loadTauriInvoke();
  if (!invoke) return { status: "ok" };
  try {
    await invoke("dismiss_crash_recovery");
    return { status: "ok" };
  } catch (e) {
    return { status: "error", error: String(e) };
  }
}

export async function getAboutVersion(): Promise<string | null> {
  const invoke = await loadTauriInvoke();
  if (!invoke) return null;
  try {
    const about = await invoke<{ version: string }>("about");
    return about.version ?? null;
  } catch {
    return null;
  }
}
