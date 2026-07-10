import { getJSON, putJSON, sidecarFetch } from "../api";

export type LogLevel = "info" | "debug";

export interface Settings {
  log_level: LogLevel;
}

export function getSettings(): Promise<Settings> {
  return getJSON<Settings>("/settings");
}

export function setLogLevel(level: LogLevel): Promise<Settings> {
  return putJSON<Settings>("/settings/log-level", { level });
}

export interface ToolsSettings {
  searxng_url: string;
  configured: boolean;
  env_configured: boolean;
}

export interface ToolsSettingsUpdate {
  searxng_url?: string;
}

const UI_HEADERS = { "x-errorta-origin": "tauri-ui" };

async function toolsSettingsRequest(init: RequestInit): Promise<ToolsSettings> {
  const res = await sidecarFetch("/settings/tools", {
    ...init,
    headers: {
      ...UI_HEADERS,
      ...(init.headers ?? {}),
    },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(
      `${init.method ?? "GET"} /settings/tools failed (${res.status}): ${text}`,
    );
  }
  return (await res.json()) as ToolsSettings;
}

export function getToolsSettings(): Promise<ToolsSettings> {
  return toolsSettingsRequest({ method: "GET" });
}

export function putToolsSettings(
  update: ToolsSettingsUpdate,
): Promise<ToolsSettings> {
  return toolsSettingsRequest({
    method: "PUT",
    body: JSON.stringify(update),
  });
}

export interface ModelFamiliesSettings {
  configured: string[];
  allowlist: string[] | null;
  effective: string[];
  derived: boolean;
}

async function modelFamiliesRequest(init: RequestInit): Promise<ModelFamiliesSettings> {
  const res = await sidecarFetch("/settings/model-families", {
    ...init,
    headers: { ...UI_HEADERS, ...(init.headers ?? {}) },
  });
  if (!res.ok) {
    throw new Error(`${init.method ?? "GET"} /settings/model-families failed (${res.status})`);
  }
  return (await res.json()) as ModelFamiliesSettings;
}

export function getModelFamilies(): Promise<ModelFamiliesSettings> {
  return modelFamiliesRequest({ method: "GET" });
}

export function putModelFamilies(families: string[] | null): Promise<ModelFamiliesSettings> {
  return modelFamiliesRequest({ method: "PUT", body: JSON.stringify({ families }) });
}

export interface ModelCatalogEntry {
  route_id: string;
  capability_tier: "light" | "mid" | "strong";
  cost_tier: number;
  size_rank: number;
  speed_rank: number;
  tiers_unset: boolean;
}

export interface ModelCatalogResponse {
  revision: string;
  entries: ModelCatalogEntry[];
  overrides: Record<string, Partial<Omit<ModelCatalogEntry, "route_id" | "tiers_unset">>>;
}

async function modelCatalogRequest(init: RequestInit): Promise<ModelCatalogResponse> {
  const res = await sidecarFetch("/council/model-catalog", {
    ...init,
    headers: { ...UI_HEADERS, ...(init.headers ?? {}) },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${init.method ?? "GET"} /council/model-catalog failed (${res.status}): ${text}`);
  }
  return (await res.json()) as ModelCatalogResponse;
}

export function getModelCatalog(): Promise<ModelCatalogResponse> {
  return modelCatalogRequest({ method: "GET" });
}

export function putModelCatalog(
  overrides: ModelCatalogResponse["overrides"],
): Promise<ModelCatalogResponse> {
  return modelCatalogRequest({ method: "PUT", body: JSON.stringify({ overrides }) });
}

export interface RemoteAiarTunnelState {
  ssh_host: string;
  remote_host: string;
  remote_port: number;
  local_port: number;
  state: "down" | "connecting" | "up" | "reconnecting" | "error";
  last_error: string;
  since: string;
}

export interface RemoteAiarSettings {
  configured: boolean;
  managed: boolean;
  base_url: string;
  tunnel_port: number | null;
  timeout_s: number;
  verify: boolean;
  token_configured: boolean;
  token_preview: string | null;
  updated_at: string | null;
  // F089 managed-tunnel mode.
  ssh_host: string | null;
  remote_host: string;
  remote_port: number | null;
  ssh_port: number | null;
  ssh_username: string | null;
  ssh_key_path: string | null;
  auto_start: boolean;
  tunnel?: RemoteAiarTunnelState | null;
}

export interface RemoteAiarSettingsUpdate {
  base_url?: string;
  tunnel_port?: number | null;
  token?: string;
  timeout_s?: number;
  verify?: boolean;
  clear?: boolean;
  clear_token?: boolean;
  ssh_host?: string | null;
  remote_host?: string | null;
  remote_port?: number | null;
  ssh_port?: number | null;
  ssh_username?: string | null;
  ssh_key_path?: string | null;
  auto_start?: boolean | null;
}

async function remoteAiarRequest(
  init: RequestInit,
): Promise<RemoteAiarSettings> {
  const res = await sidecarFetch("/settings/remote-aiar", {
    ...init,
    headers: {
      ...UI_HEADERS,
      ...(init.headers ?? {}),
    },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(
      `${init.method ?? "GET"} /settings/remote-aiar failed (${res.status}): ${text}`,
    );
  }
  return (await res.json()) as RemoteAiarSettings;
}

export function getRemoteAiarSettings(): Promise<RemoteAiarSettings> {
  return remoteAiarRequest({ method: "GET" });
}

export function putRemoteAiarSettings(
  update: RemoteAiarSettingsUpdate,
): Promise<RemoteAiarSettings> {
  return remoteAiarRequest({
    method: "PUT",
    body: JSON.stringify(update),
  });
}

export async function reconnectRemoteAiarTunnel(): Promise<RemoteAiarSettings> {
  const res = await sidecarFetch("/settings/remote-aiar/tunnel/reconnect", {
    method: "POST",
    headers: { ...UI_HEADERS },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`reconnect tunnel failed (${res.status}): ${text}`);
  }
  return (await res.json()) as RemoteAiarSettings;
}
