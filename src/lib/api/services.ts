import { sidecarFetch } from "../api";

export interface ServicePromptRequest {
  prompt: string;
  corpus: string;
  model?: string;
  judge?: boolean;
  system?: string;
  top_k?: number;
  metadata?: Record<string, unknown>;
}

export interface ServiceCitation {
  source_path: string;
  chunk_text: string;
  page_num?: number | null;
}

export interface ServicePromptResponse {
  id: string;
  answer: string;
  verdict: Record<string, unknown> | null;
  citations: ServiceCitation[];
  judge_model: string | null;
  latency_ms: number;
}

export interface ServicesMetaResponse {
  errorta_version: string;
  aiar_version: string | null;
  sdk_contract_version: string;
  judge_available: boolean;
  default_model: string | null;
  default_judge_model: string | null;
  corpora: Array<Record<string, unknown>>;
  corpus_source: string;
  catalog_verified: boolean;
}

async function request<T>(
  path: string,
  token: string,
  init: RequestInit,
): Promise<T> {
  const r = await sidecarFetch(path, {
    ...init,
    headers: {
      "X-Errorta-Token": token,
      ...(init.headers ?? {}),
    },
  });
  if (!r.ok) {
    const body = await r.text().catch(() => "");
    throw new Error(`HTTP ${r.status} on ${path}: ${body.slice(0, 200)}`);
  }
  const text = await r.text();
  return text ? (JSON.parse(text) as T) : (undefined as T);
}

export function prompt(
  token: string,
  body: ServicePromptRequest,
): Promise<ServicePromptResponse> {
  return request<ServicePromptResponse>("/services/prompt", token, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function meta(token: string): Promise<ServicesMetaResponse> {
  return request<ServicesMetaResponse>("/services/meta", token, {
    method: "GET",
  });
}
