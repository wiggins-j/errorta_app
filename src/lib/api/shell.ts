// F006 — Tauri shell polish / settings client.
//
// Backs the AppShellSettings + ProcessHealthIndicator components. All calls
// route through the sidecar's /shell/* endpoints. Tauri-command-level shell
// integration (sidecar_port, processes, restart_sidecar) lives in
// ../../features/shell/* and falls back to these HTTP endpoints when the
// Tauri command bridge is not available (e.g. in plain `vite dev`).

import { getJSON, postJSON } from "../api";

export interface ManagedProcess {
  pid: number;
  name: string;
  role: string;
  status: string;
  cpu_percent: number;
  rss_bytes: number;
  started_at: number | null;
}

export interface ProcessesResponse {
  processes: ManagedProcess[];
}

export interface OllamaHealth {
  host: string;
  reachable: boolean;
  version?: string | null;
  error?: string | null;
}

export interface SidecarSubHealth {
  pid: number;
  uptime_seconds: number;
  cold_start_seconds: number | null;
}

export interface ShellStatus {
  sidecar: SidecarSubHealth;
  ollama: OllamaHealth;
  processes: ManagedProcess[];
}

export interface SidecarPortInfo {
  port: number;
  source: "env" | "default";
}

export interface OllamaHostInfo {
  host: string;
}

export interface ColdStartInfo {
  cold_start_seconds: number | null;
  process_start_epoch: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function numberOr(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function nullableNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function stringOr(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function normalizeManagedProcess(value: unknown): ManagedProcess | null {
  if (!isRecord(value)) return null;
  const pid = numberOr(value.pid, NaN);
  if (!Number.isFinite(pid)) return null;
  return {
    pid,
    name: stringOr(value.name, "unknown"),
    role: stringOr(value.role, "unknown"),
    status: stringOr(value.status, "unknown"),
    cpu_percent: numberOr(value.cpu_percent, 0),
    rss_bytes: numberOr(value.rss_bytes, 0),
    started_at: nullableNumber(value.started_at),
  };
}

export function normalizeProcessesResponse(raw: unknown): ProcessesResponse {
  const processes = isRecord(raw) && Array.isArray(raw.processes)
    ? raw.processes
        .map((p) => normalizeManagedProcess(p))
        .filter((p): p is ManagedProcess => p !== null)
    : [];
  return { processes };
}

export function normalizeShellStatus(raw: unknown): ShellStatus {
  const root = isRecord(raw) ? raw : {};
  const sidecar = isRecord(root.sidecar) ? root.sidecar : {};
  const ollama = isRecord(root.ollama) ? root.ollama : {};
  const reachable = ollama.reachable === true;

  return {
    sidecar: {
      pid: numberOr(sidecar.pid, 0),
      uptime_seconds: numberOr(sidecar.uptime_seconds, 0),
      cold_start_seconds: nullableNumber(sidecar.cold_start_seconds),
    },
    ollama: {
      host: stringOr(ollama.host, ""),
      reachable,
      version: stringOrNull(ollama.version),
      error: stringOrNull(ollama.error) ?? (reachable ? null : "status unavailable"),
    },
    processes: normalizeProcessesResponse(root).processes,
  };
}

export function normalizeSidecarPort(raw: unknown): SidecarPortInfo {
  const root = isRecord(raw) ? raw : {};
  const port = numberOr(root.port, 8770);
  return {
    port: port > 0 && port <= 65535 ? port : 8770,
    source: root.source === "env" ? "env" : "default",
  };
}

export function status(): Promise<ShellStatus> {
  return getJSON<unknown>("/shell/status").then(normalizeShellStatus);
}

export function processes(): Promise<ProcessesResponse> {
  return getJSON<unknown>("/shell/processes").then(normalizeProcessesResponse);
}

export function sidecarPort(): Promise<SidecarPortInfo> {
  return getJSON<unknown>("/shell/sidecar/port").then(normalizeSidecarPort);
}

export function getOllamaHost(): Promise<OllamaHostInfo> {
  return getJSON<OllamaHostInfo>("/shell/config/ollama-host");
}

export function setOllamaHost(host: string): Promise<OllamaHostInfo> {
  return postJSON<OllamaHostInfo>("/shell/config/ollama-host", { host });
}

export function markReady(): Promise<ColdStartInfo> {
  return postJSON<ColdStartInfo>("/shell/ready");
}

export function coldStart(): Promise<ColdStartInfo> {
  return getJSON<ColdStartInfo>("/shell/cold-start");
}
