// F-DIST-01 — typed client for the local /alpha/* sidecar routes that drive the
// activation + lock UI. The sidecar owns the license state and the (disclosed)
// egress to api.errorta.app; this client only reads status and submits a code.

import { getJSON, sidecarFetch, SidecarUnreachableError } from "../api";

export type AlphaState = "disabled" | "unactivated" | "active" | "expired" | "revoked";

export interface AlphaStatus {
  gateEnabled: boolean;
  state: AlphaState;
  locked: boolean;
  reason: string | null;
  graceUntil: number | null;
  deviceId: string | null;
  buildEol: boolean;
  buildEolRequired: boolean;
  updateUrl: string | null;
}

interface AlphaStatusWire {
  gate_enabled: boolean;
  state: AlphaState;
  locked: boolean;
  reason: string | null;
  grace_until: number | null;
  device_id: string | null;
  build_eol?: boolean;
  build_eol_required: boolean;
  update_url: string | null;
}

function adapt(w: AlphaStatusWire): AlphaStatus {
  return {
    gateEnabled: w.gate_enabled,
    state: w.state,
    locked: w.locked,
    reason: w.reason,
    graceUntil: w.grace_until,
    deviceId: w.device_id,
    buildEol: w.build_eol ?? false,
    buildEolRequired: w.build_eol_required,
    updateUrl: w.update_url,
  };
}

export async function getAlphaStatus(): Promise<AlphaStatus> {
  return adapt(await getJSON<AlphaStatusWire>("/alpha/status"));
}

/** Thrown when activation is rejected; `code` is the machine-readable reason
 *  from the check-in service (e.g. `code_exhausted`, `offline`). */
export class AlphaActivationError extends Error {
  constructor(
    public readonly code: string,
    message?: string,
  ) {
    super(message || code);
    this.name = "AlphaActivationError";
  }
}

const UI_HEADERS = { "x-errorta-origin": "tauri-ui" };

// Back-off schedule for a transient activation transport failure. The webview
// can race the sidecar during first-launch warm-up / respawn. A rejected fetch
// does not prove whether the sidecar processed the request before the response
// was lost, so this retry relies on /v1/activate being idempotent for the same
// device_id + code (the Worker returns the existing seat without incrementing).
const ACTIVATE_BACKOFF_MS = [300, 700, 1500];
const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export async function activateAlpha(code: string): Promise<AlphaStatus> {
  // A `SidecarUnreachableError` means no HTTP response reached the webview. The
  // activation protocol is idempotent for the same device + code, so retrying is
  // safe even if an earlier request committed before its response was lost. A
  // real HTTP response (server-side rejection) throws `AlphaActivationError`
  // below and is NOT retried (deterministic). This closes
  // the "first launch → Activate raced the still-booting sidecar → generic
  // failure" gap that a tester can't work around.
  for (let attempt = 0; ; attempt++) {
    let res: Response;
    try {
      res = await sidecarFetch("/alpha/activate", {
        method: "POST",
        headers: { ...UI_HEADERS, "content-type": "application/json" },
        body: JSON.stringify({ code }),
      });
    } catch (err) {
      if (err instanceof SidecarUnreachableError && attempt < ACTIVATE_BACKOFF_MS.length) {
        await sleep(ACTIVATE_BACKOFF_MS[attempt]);
        continue;
      }
      throw err;
    }
    if (res.ok) {
      return adapt((await res.json()) as AlphaStatusWire);
    }
    // FastAPI serializes HTTPException(detail={...}) as {"detail": {error, message}}.
    let errorCode = `http_${res.status}`;
    let message: string | undefined;
    try {
      const body = (await res.json()) as { detail?: { error?: string; message?: string } };
      if (body.detail?.error) errorCode = body.detail.error;
      message = body.detail?.message ?? undefined;
    } catch {
      // non-JSON error body — keep the http_<status> code
    }
    throw new AlphaActivationError(errorCode, message);
  }
}

// --- telemetry (slice 6) -----------------------------------------------------

export interface AlphaTelemetryConsent {
  gateEnabled: boolean;
  extrasEnabled: boolean;
}

export interface AlphaTelemetryEvent {
  event: string;
  name?: string;
  bucket?: string;
  count?: number;
}

export interface AlphaTelemetryInspect {
  extrasEnabled: boolean;
  floor: Record<string, number>;
  queue: AlphaTelemetryEvent[];
  queueLen: number;
}

export async function getTelemetryConsent(): Promise<AlphaTelemetryConsent> {
  const w = await getJSON<{ gate_enabled: boolean; extras_enabled: boolean }>("/alpha/telemetry");
  return { gateEnabled: w.gate_enabled, extrasEnabled: w.extras_enabled };
}

export async function setTelemetryExtras(enabled: boolean): Promise<boolean> {
  const res = await sidecarFetch("/alpha/telemetry", {
    method: "PUT",
    headers: { ...UI_HEADERS, "content-type": "application/json" },
    body: JSON.stringify({ extras_enabled: enabled }),
  });
  if (!res.ok) throw new Error(`PUT /alpha/telemetry failed (${res.status})`);
  const w = (await res.json()) as { extras_enabled: boolean };
  return w.extras_enabled;
}

export async function getTelemetryInspect(): Promise<AlphaTelemetryInspect> {
  const w = await getJSON<{
    extras_enabled: boolean;
    floor: Record<string, number>;
    queue: AlphaTelemetryEvent[];
    queue_len: number;
  }>("/alpha/telemetry/inspect");
  return {
    extrasEnabled: w.extras_enabled,
    floor: w.floor ?? {},
    queue: w.queue ?? [],
    queueLen: w.queue_len ?? 0,
  };
}

// --- feedback (slice 7) ------------------------------------------------------

export type FeedbackKind = "bug" | "suggestion" | "crash";

export interface FeedbackPreview {
  preparedId: string;
  kind: FeedbackKind;
  message: string;
  bundle: {
    sha256: string | null;
    files: string[];
    redaction: Record<string, number>;
  };
}

/** Build the redacted bundle and return its manifest so the tester sees exactly
 *  what will be sent before confirming. */
export async function previewFeedback(
  kind: FeedbackKind,
  message: string,
): Promise<FeedbackPreview> {
  const res = await sidecarFetch("/alpha/feedback/preview", {
    method: "POST",
    headers: { ...UI_HEADERS, "content-type": "application/json" },
    body: JSON.stringify({ kind, message }),
  });
  if (!res.ok) throw new Error(`preview feedback failed (${res.status})`);
  const w = (await res.json()) as {
    prepared_id: string;
    kind: FeedbackKind;
    message: string;
    bundle: { sha256: string | null; files?: string[]; redaction?: Record<string, number> };
  };
  return {
    preparedId: w.prepared_id,
    kind: w.kind,
    message: w.message,
    bundle: {
      sha256: w.bundle.sha256,
      files: w.bundle.files ?? [],
      redaction: w.bundle.redaction ?? {},
    },
  };
}

/** Send a previously-previewed feedback bundle. Returns the ticket id. */
export async function submitFeedback(preparedId: string): Promise<string> {
  const res = await sidecarFetch("/alpha/feedback/submit", {
    method: "POST",
    headers: { ...UI_HEADERS, "content-type": "application/json" },
    body: JSON.stringify({ prepared_id: preparedId }),
  });
  if (!res.ok) throw new Error(`submit feedback failed (${res.status})`);
  const w = (await res.json()) as { ticket_id: string };
  return w.ticket_id;
}
