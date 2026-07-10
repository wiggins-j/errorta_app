import { sidecarFetch } from "../api";

const UI_HEADERS = { "x-errorta-origin": "tauri-ui" };

export type PairingStatus = "pending" | "accepted" | "consumed" | "denied" | "expired";

export interface PairingSession {
  sessionId: string;
  status: PairingStatus;
  appSlug: string;
  appName: string;
  requestedCorpora: string[];
  requestedScopes: string[];
  approvedCorpora: string[];
  approvedScopes: string[];
  createdAt: string | null;
  expiresAt: string | null;
  issuedAt: string | null;
  tokenId: string | null;
}

export interface ServiceTokenMetadata {
  id: string;
  appSlug: string;
  appName: string;
  corpora: string[];
  scopes: string[];
  issuedAt: string;
  lastUsedAt: string | null;
}

async function request<T>(path: string, init: RequestInit): Promise<T> {
  const r = await sidecarFetch(path, init);
  if (!r.ok) {
    const body = await r.text().catch(() => "");
    throw new Error(`HTTP ${r.status} on ${path}: ${body.slice(0, 200)}`);
  }
  const text = await r.text();
  return text ? (JSON.parse(text) as T) : (undefined as T);
}

function jsonInit(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: UI_HEADERS,
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

function mapPairingSession(wire: Record<string, unknown>): PairingSession {
  return {
    sessionId: String(wire.session_id ?? ""),
    status: (wire.status as PairingStatus) ?? "pending",
    appSlug: String(wire.app_slug ?? ""),
    appName: String(wire.app_name ?? ""),
    requestedCorpora: Array.isArray(wire.requested_corpora)
      ? (wire.requested_corpora as string[])
      : [],
    requestedScopes: Array.isArray(wire.requested_scopes)
      ? (wire.requested_scopes as string[])
      : [],
    approvedCorpora: Array.isArray(wire.approved_corpora)
      ? (wire.approved_corpora as string[])
      : [],
    approvedScopes: Array.isArray(wire.approved_scopes)
      ? (wire.approved_scopes as string[])
      : [],
    createdAt: (wire.created_at as string | null | undefined) ?? null,
    expiresAt: (wire.expires_at as string | null | undefined) ?? null,
    issuedAt: (wire.issued_at as string | null | undefined) ?? null,
    tokenId: (wire.token_id as string | null | undefined) ?? null,
  };
}

function mapToken(wire: Record<string, unknown>): ServiceTokenMetadata {
  return {
    id: String(wire.id ?? ""),
    appSlug: String(wire.app_slug ?? ""),
    appName: String(wire.app_name ?? ""),
    corpora: Array.isArray(wire.corpora) ? (wire.corpora as string[]) : [],
    scopes: Array.isArray(wire.scopes) ? (wire.scopes as string[]) : [],
    issuedAt: String(wire.issued_at ?? ""),
    lastUsedAt: (wire.last_used_at as string | null | undefined) ?? null,
  };
}

export async function listPairingRequests(): Promise<PairingSession[]> {
  const wire = await request<{ pairs?: Array<Record<string, unknown>> }>(
    "/api/auth/pairs",
    jsonInit("GET"),
  );
  return (wire.pairs ?? []).map(mapPairingSession);
}

export async function approvePairing(
  sessionId: string,
  grant: { corpora: string[]; scopes: string[] },
): Promise<PairingSession> {
  const wire = await request<{ pairing: Record<string, unknown> }>(
    `/api/auth/pair/${encodeURIComponent(sessionId)}/approve`,
    jsonInit("POST", grant),
  );
  return mapPairingSession(wire.pairing);
}

export async function denyPairing(sessionId: string): Promise<PairingSession> {
  const wire = await request<{ pairing: Record<string, unknown> }>(
    `/api/auth/pair/${encodeURIComponent(sessionId)}/deny`,
    jsonInit("POST"),
  );
  return mapPairingSession(wire.pairing);
}

export async function listTokens(): Promise<ServiceTokenMetadata[]> {
  const wire = await request<{ tokens?: Array<Record<string, unknown>> }>(
    "/api/auth/tokens",
    jsonInit("GET"),
  );
  return (wire.tokens ?? []).map(mapToken);
}

export async function revokeToken(tokenId: string): Promise<void> {
  await request<{ id: string; status: string }>(
    `/api/auth/tokens/${encodeURIComponent(tokenId)}`,
    jsonInit("DELETE"),
  );
}
