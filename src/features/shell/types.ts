// F006 — types shared across the shell feature components.
export type { ManagedProcess, ShellStatus, OllamaHealth, SidecarPortInfo } from "../../lib/api/shell";

export type HealthLevel = "ok" | "warn" | "down" | "unknown";

export function classifyOllama(reachable: boolean, error: string | null | undefined): HealthLevel {
  if (reachable) return "ok";
  if (error) return "down";
  return "unknown";
}
