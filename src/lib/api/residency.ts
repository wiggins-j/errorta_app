// F-INFRA-12 Phase B Slice 9 — typed client for the sidecar residency routes.
//
// Mirrors the shape of `errorta_app/routes/residency.py` exactly so the
// Settings UI doesn't have to translate between two schemas.
//
// Three endpoints:
//   GET  /residency          → ResidencyGetResponse
//   PUT  /residency          → ResidencyGetResponse (with the new state)
//   POST /residency/probe    → ResidencyProbeResponse
//
// All requests are routed through the shared `sidecarFetch` helper, which in
// turn resolves the sidecar base URL via the Tauri `sidecar_port` command (or
// the dev-mode `VITE_SIDECAR_BASE` fallback). We never hardcode 127.0.0.1.
//
// Error mode mirrors `lib/api/diagnostics.ts`: non-2xx throws an Error whose
// `.message` includes the HTTP status + the upstream body snippet.

import { sidecarFetch } from "../api";

export type ResidencyMode = "local" | "ssh-remote" | "cloud";

/**
 * Discriminated union for the live tunnel state — wire shape mirrors the
 * Rust-side `TunnelState` enum (kebab-case, `kind` + optional `detail`).
 */
export type TunnelState =
  | { kind: "down"; detail?: string }
  | { kind: "connecting"; detail?: string }
  | { kind: "up"; detail?: string }
  | { kind: "error"; detail?: string };

/**
 * Persisted residency config. Field names match
 * `errorta_residency.config.ResidencyState` exactly. `cloud_token` is never
 * echoed by GET responses — it's accepted on PUT but the server redacts it on
 * the way back out, so the field stays optional + nullable on responses.
 */
export interface ResidencyConfig {
  mode: ResidencyMode;
  ssh_host?: string | null;
  ssh_port?: number | null;
  ssh_key_path?: string | null;
  ssh_username?: string | null;
  remote_sidecar_port?: number | null;
  local_tunnel_port?: number | null;
  cloud_url?: string | null;
  cloud_token?: string | null;
  updated_at?: string | null;
}

export interface ResidencyGetResponse {
  config: ResidencyConfig;
  tunnel_state: TunnelState;
  remote_healthz: unknown | null;
}

interface ResidencyGetWireResponse {
  config: ResidencyConfig;
  tunnel_state?: unknown;
  remote_healthz: unknown | null;
}

export interface ResidencyProbeResponse {
  ok: boolean;
  status: number | null;
  body: unknown | null;
  error: string | null;
}

async function request<T>(path: string, init: RequestInit): Promise<T> {
  const r = await sidecarFetch(path, init);
  if (!r.ok) {
    const body = await r.text().catch(() => "");
    throw new Error(`HTTP ${r.status} on ${path}: ${body.slice(0, 200)}`);
  }
  if (r.status === 204) return undefined as unknown as T;
  const text = await r.text();
  if (!text) return undefined as unknown as T;
  return JSON.parse(text) as T;
}

export function normalizeTunnelState(value: unknown): TunnelState {
  const fromKind = (kind: string, detail?: unknown): TunnelState => {
    switch (kind) {
      case "down":
      case "local":
        return { kind: "down" };
      case "connecting":
        return { kind: "connecting" };
      case "up":
        return { kind: "up" };
      case "error":
        return {
          kind: "error",
          ...(typeof detail === "string" && detail.trim()
            ? { detail }
            : {}),
        };
      default:
        return {
          kind: "error",
          detail: `Unknown tunnel state: ${kind}`,
        };
    }
  };

  if (typeof value === "string") {
    return fromKind(value);
  }
  if (value && typeof value === "object") {
    const maybe = value as { kind?: unknown; detail?: unknown };
    if (typeof maybe.kind === "string") {
      return fromKind(maybe.kind, maybe.detail);
    }
  }
  return { kind: "down" };
}

function normalizeResidencyResponse(
  wire: ResidencyGetWireResponse,
): ResidencyGetResponse {
  return {
    config: wire.config,
    tunnel_state: normalizeTunnelState(wire.tunnel_state),
    remote_healthz: wire.remote_healthz ?? null,
  };
}

export function getResidency(): Promise<ResidencyGetResponse> {
  return request<ResidencyGetWireResponse>("/residency", { method: "GET" })
    .then(normalizeResidencyResponse);
}

/**
 * PUT /residency body must always carry a concrete `mode`. The other fields
 * are optional — the sidecar route fills in defaults and clears irrelevant
 * fields when transitioning modes.
 */
export function putResidency(
  body: Partial<ResidencyConfig> & { mode: ResidencyMode },
): Promise<ResidencyGetResponse> {
  return request<ResidencyGetWireResponse>("/residency", {
    method: "PUT",
    body: JSON.stringify(body),
  }).then(normalizeResidencyResponse);
}

/**
 * Probe an arbitrary `url` (with optional bearer `token`). The sidecar never
 * raises on probe failures — `{ok, status, body, error}` is always returned.
 */
export function probeResidency(
  url: string,
  token?: string,
): Promise<ResidencyProbeResponse> {
  return request<ResidencyProbeResponse>("/residency/probe", {
    method: "POST",
    body: JSON.stringify({ url, ...(token ? { token } : {}) }),
  });
}
