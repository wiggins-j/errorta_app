// Thin client for the Errorta Python sidecar.
//
// The base URL is resolved dynamically via `getSidecarBase()` (which talks to
// the Tauri `sidecar_port` command) so each Errorta window connects to its
// own ephemeral port. In browser-dev (no Tauri shell) we fall back to the
// fixed port the sidecar uses when run standalone (`python -m errorta_app.server`).

import {
  ensureSidecarBase,
  getSidecarBase,
  resetSidecarBaseCache,
} from "./sidecarPort";

/** Thrown when the sidecar can't be reached even after a re-resolve (F063). */
export class SidecarUnreachableError extends Error {
  constructor() {
    super(
      "sidecar_unreachable: the Errorta backend is not responding — try reopening the app",
    );
    this.name = "SidecarUnreachableError";
  }
}

// Methods safe to auto-retry after a transport failure. A transport failure
// ("Load failed" — typically a stale cached port after the sidecar respawns on
// a new ephemeral port) means the request never got a response, so a retry
// against the re-resolved port heals it. We restrict auto-retry to IDEMPOTENT
// methods (RFC 7231): GET/HEAD/PUT/DELETE. A retried PUT/DELETE is harmless by
// definition — e.g. PUT /rooms/{id} carries expected_revision, so a re-send
// either lands once or returns a clean 409, never a double-write; a re-sent
// DELETE 404s cleanly. POST/PATCH are NOT retried (a POST /council/runs could
// have committed before the drop), so we re-resolve the port and surface a
// clear error for the user to re-submit against the now-healed sidecar.
const RETRYABLE_METHODS = new Set(["GET", "HEAD", "PUT", "DELETE"]);

export const DEFAULT_SIDECAR_BASE =
  (import.meta as ImportMeta & { env: { VITE_SIDECAR_BASE?: string } }).env
    .VITE_SIDECAR_BASE ?? "http://127.0.0.1:8770";

/**
 * SIDECAR_BASE — the *dev-mode fallback* base URL.
 *
 * For real requests, always go through `sidecarFetch` / `fetchJSON` etc.
 * (which await `getSidecarBase()`). Direct use of this constant is retained
 * only for callers that need a sync default before the Tauri command has
 * resolved — they should migrate to the async path.
 */
export const SIDECAR_BASE = DEFAULT_SIDECAR_BASE;

export type AiarPinSource = "editable" | "pinned" | "absent" | "remote";

export interface AiarPin {
  available: boolean;
  version: string | null;
  source: AiarPinSource;
}

/** Build provenance (commit this sidecar was built from). Absent on builds that
 * predate the provenance stamp — which is itself a "this app is old" signal. */
export interface SidecarBuild {
  commit?: string | null;
  commit_short?: string | null;
  built_at?: string | null;
  dirty?: boolean;
  source?: string;
}

/** Capability surfaces this build exposes. An older build omits keys it
 * predates (e.g. `grounding`), so the UI can detect drift. */
export interface SidecarFeatures {
  coding?: boolean;
  council?: boolean;
  briefs?: boolean;
  judge?: boolean;
  grounding?: boolean;
  model_assignment_ready?: boolean;
}

export interface CorpusBackendHealth {
  kind: string;
  detail?: Record<string, unknown>;
  retrieval_coordinated?: boolean;
  backend_id?: string | null;
}

export interface AiarRuntimeHealth {
  kind?: string;
  runtime_kind?: string;
  display_name?: string;
  connected?: boolean;
  backend_id?: string | null;
  active_model?: string | null;
  active_model_ready?: boolean | null;
  corpus_count?: number | null;
  capabilities?: Record<string, unknown>;
  error_code?: string | null;
  error_message?: string | null;
}

export interface SidecarHealth {
  service: string;
  version: string;
  now: string;
  aiar_available: boolean;
  aiar_version?: string | null;
  /** Added in v0.1.5. Optional for forward/backward compatibility. */
  aiar_pin?: AiarPin;
  /** Added in F031 Phase 0. Optional for forward compatibility. */
  council?: boolean;
  briefs?: boolean;
  /** Build-freshness: commit + capability surface. Absent on older builds. */
  build?: SidecarBuild;
  features?: SidecarFeatures;
  /** F095/F115: catalog backend and retrieval-routing coordination status. */
  corpus_backend?: CorpusBackendHealth;
  /** F116: active AIAR runtime, distinct from local aiar_pin install metadata. */
  aiar_runtime?: AiarRuntimeHealth;
}

/** Whether the coding console's grounding features are present in this build. */
export function groundingSupported(health: SidecarHealth | null): boolean {
  return health?.features?.grounding === true;
}

/** Whether the running app looks stale (built before current code). True when
 * the build carries no commit stamp at all (pre-provenance build) or is missing
 * a capability current builds always expose (grounding). Both mean: rebuild. */
export function appLooksStale(health: SidecarHealth | null): boolean {
  if (!health) return false; // unknown — don't cry wolf
  if (!health.build || !health.build.commit) return true;
  if (health.features && health.features.grounding === false) return true;
  return false;
}

/**
 * Low-level fetch helper. Resolves the sidecar base via `getSidecarBase()`,
 * then issues a normal `fetch` against `<base><path>`. Caller owns the
 * Response — useful when streaming or reading non-JSON bodies.
 */
export async function sidecarFetch(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  // Build headers via a `Headers` object (case-insensitive `set`) rather than a
  // plain-object spread. A spread keeps `"Content-Type"` (auto-added below) AND
  // a caller's `"content-type"` as two DISTINCT record keys; WKWebView (the
  // macOS Tauri webview) then combines them into a single invalid
  // `application/json, application/json` value, which the sidecar can't parse as
  // JSON and rejects with HTTP 422. `Headers.set` overwrites case-insensitively,
  // so an explicit caller content-type replaces — never duplicates — the
  // auto-added one. (curl / happy-dom / Chromium dedupe silently, which is why
  // this only ever bit the packaged mac app.)
  const headers = new Headers({ Accept: "application/json" });
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (init.headers) {
    new Headers(init.headers as HeadersInit).forEach((value, key) => {
      headers.set(key, value);
    });
  }
  const method = (init.method ?? "GET").toUpperCase();
  const base = await getSidecarBase();
  try {
    return await fetch(`${base}${path}`, { ...init, headers });
  } catch (err) {
    // A rejected fetch is a TRANSPORT failure ("Load failed") — the sidecar is
    // gone/changed port, NOT an HTTP error (those resolve with !ok). Re-resolve
    // a live sidecar (respawn if dead) and, for idempotent methods only, retry
    // once. Non-idempotent writes are never auto-retried (no double-apply).
    if (!(err instanceof TypeError)) throw err;
    resetSidecarBaseCache();
    const fresh = await ensureSidecarBase();
    // Retry idempotent methods always. For a NON-idempotent write (POST/PATCH),
    // retry once ONLY when the sidecar actually moved to a different port: the
    // original request hit a now-dead port, so it was never received by any
    // server (no double-apply risk) — this is exactly the "sidecar respawned on
    // a new ephemeral port" case that otherwise breaks Start Run with a
    // spurious "sidecar unreachable" even though the backend is healthy. If the
    // port is unchanged, a write could have been received before the drop, so
    // we don't retry — surface a clear error for the user to re-submit.
    const portMoved = fresh !== base;
    if (!RETRYABLE_METHODS.has(method) && !portMoved) {
      throw new SidecarUnreachableError();
    }
    try {
      return await fetch(`${fresh}${path}`, { ...init, headers });
    } catch (err2) {
      if (err2 instanceof TypeError) throw new SidecarUnreachableError();
      throw err2;
    }
  }
}

async function request<T>(path: string, init: RequestInit): Promise<T> {
  const r = await sidecarFetch(path, init);
  if (!r.ok) {
    const body = await r.text().catch(() => "");
    throw new Error(`HTTP ${r.status} on ${path}: ${body.slice(0, 200)}`);
  }
  // 204 No Content
  if (r.status === 204) return undefined as unknown as T;
  const text = await r.text();
  if (!text) return undefined as unknown as T;
  return JSON.parse(text) as T;
}

export async function fetchJSON<T>(path: string): Promise<T> {
  return request<T>(path, { method: "GET" });
}

export function getJSON<T>(path: string): Promise<T> {
  return request<T>(path, { method: "GET" });
}

export function postJSON<T>(
  path: string,
  body?: unknown,
  headers?: Record<string, string>,
): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
    ...(headers ? { headers } : {}),
  });
}

export function putJSON<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "PUT",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export function deleteJSON<T>(path: string): Promise<T> {
  return request<T>(path, { method: "DELETE" });
}

export function sidecarHealth(): Promise<SidecarHealth> {
  return fetchJSON<SidecarHealth>("/healthz");
}
