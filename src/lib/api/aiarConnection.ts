import { getJSON, sidecarFetch } from "../api";

export type AiarRuntimeKind =
  | "local-aiar"
  | "aiar-service"
  | "errorta-sidecar-remote"
  | "disconnected";

export interface AiarCapabilityMap {
  answer: boolean;
  judge: boolean;
  model_catalog: boolean;
  model_active_status: boolean;
  model_set_active: boolean;
  ollama_pull: boolean;
  corpus_list: boolean;
  corpus_upload: boolean;
  folder_watch: boolean;
  pure_retrieve: boolean;
  grounding_record: boolean;
  grounding_lookup: boolean;
  remote_ingest: boolean;
  diagnostics?: string;
}

export interface AiarStatus {
  kind?: AiarRuntimeKind;
  runtime_kind: AiarRuntimeKind;
  display_name: string;
  connected: boolean;
  base_url?: string | null;
  token_configured?: boolean;
  verify_tls?: boolean;
  timeout_s?: number;
  backend_id?: string | null;
  capabilities: AiarCapabilityMap;
  active_model?: string | null;
  active_model_ready?: boolean | null;
  available_models: string[];
  corpus_count?: number | null;
  config_source?: string | null;
  status_source?: string | null;
  error_code?: string | null;
  error_message?: string | null;
}

export interface AiarConnectionResponse {
  configured: boolean;
  canonical: Record<string, unknown> | null;
  status: AiarStatus;
}

export interface AiarConnectionUpdate {
  kind: AiarRuntimeKind;
  display_name?: string | null;
  base_url?: string | null;
  token?: string | null;
  timeout_s?: number;
  verify_tls?: boolean;
  preferred_model?: string | null;
  allow_disconnected?: boolean;
}

export function getAiarStatus(): Promise<AiarStatus> {
  return getJSON<AiarStatus>("/aiar/status");
}

export function getAiarConnection(): Promise<AiarConnectionResponse> {
  return getJSON<AiarConnectionResponse>("/aiar/connection");
}

export async function updateAiarConnection(
  update: AiarConnectionUpdate,
): Promise<AiarConnectionResponse> {
  const response = await sidecarFetch("/aiar/connection", {
    method: "PUT",
    headers: {
      "content-type": "application/json",
      "x-errorta-origin": "tauri-ui",
    },
    body: JSON.stringify(update),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`PUT /aiar/connection failed (${response.status}): ${text}`);
  }
  return (await response.json()) as AiarConnectionResponse;
}
