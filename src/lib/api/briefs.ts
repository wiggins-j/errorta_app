// F008 Briefs API client. Wraps the 10 endpoints exposed by
// `python/errorta_app/routes/briefs.py`. All routes are mounted under `/briefs`
// by the sidecar; the helpers below return parsed JSON or throw on non-2xx.

import { deleteJSON, getJSON, postJSON, putJSON, sidecarFetch } from "../api";
import type { BriefStateValue } from "../../features/briefs/types";

export interface BriefSummary {
  brief_id: string;
  corpus_name: string;
  state: BriefStateValue;
  created_at: string;
  last_run_at?: string | null;
}

export interface BriefSourceConfig {
  name: string;
  config: Record<string, unknown>;
}

export interface BriefConfig {
  project: string;
  corpus: string;
  sensitivity?: string | null;
  refresh?: string | null;
  description?: string | null;
  tags?: string[];
  per_doc_max_pages?: number | null;
  target_doc_count?: number | null;
  target_total_pages?: number | null;
  sources: BriefSourceConfig[];
  [key: string]: unknown;
}

export interface BriefManifest {
  brief_id: string;
  corpus_name: string;
  project?: string;
  state: BriefStateValue;
  created_at: string;
  last_run_at?: string | null;
  runs?: Array<Record<string, unknown>>;
  parse_errors?: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface BriefDetail {
  manifest: BriefManifest;
  markdown: string;
  config: BriefConfig | null;
  body: string;
}

export interface ConnectorStatus {
  ok: boolean;
  reason?: string | null;
  [key: string]: unknown;
}

export interface DryRunSourceProjection {
  connector_name: string;
  candidates_seen: number;
  compliance_pass: number;
  compliance_refused: number;
  sample_refusal_reasons: string[];
}

export interface ValidateResponse {
  ok: boolean;
  errors: Array<Record<string, unknown>>;
  connectors: Record<string, ConnectorStatus>;
  dry_run_projection?: Record<string, DryRunSourceProjection> | null;
}

export interface RunResponse {
  run_id: string;
  brief_id: string;
  state: BriefStateValue;
}

export interface SourceStatus {
  name: string;
  state: string;
  docs_collected: number;
  docs_refused: number;
  page_or_offset: number;
  last_canonical_id?: string | null;
  last_error?: string | null;
}

export interface LiveStatus {
  brief_id: string;
  run_id?: string | null;
  state: BriefStateValue;
  per_source: SourceStatus[];
  compliance_refusals: Array<Record<string, unknown>>;
  failures: Array<Record<string, unknown>>;
  ingested_count: number;
}

export interface PauseResponse {
  brief_id: string;
  run_id?: string | null;
  paused: boolean;
}

export interface DeleteResponse {
  brief_id: string;
  deleted: boolean;
}

export function listBriefs(): Promise<BriefSummary[]> {
  return getJSON<BriefSummary[]>("/briefs");
}

export function createBrief(markdown: string): Promise<BriefSummary> {
  return postJSON<BriefSummary>("/briefs", { markdown });
}

export function getBrief(briefId: string): Promise<BriefDetail> {
  return getJSON<BriefDetail>(`/briefs/${encodeURIComponent(briefId)}`);
}

export function updateBrief(briefId: string, markdown: string): Promise<BriefSummary> {
  return putJSON<BriefSummary>(`/briefs/${encodeURIComponent(briefId)}`, { markdown });
}

export function deleteBrief(briefId: string): Promise<DeleteResponse> {
  return deleteJSON<DeleteResponse>(`/briefs/${encodeURIComponent(briefId)}`);
}

export function validateBrief(
  briefId: string,
  opts?: { dry_run?: boolean },
): Promise<ValidateResponse> {
  const qs = opts?.dry_run ? "?dry_run=true" : "";
  return postJSON<ValidateResponse>(
    `/briefs/${encodeURIComponent(briefId)}/validate${qs}`,
  );
}

// F008-IMPORT-VAL — stateless pre-flight validation for the import path.
//
// Mirrors `validateBrief` but takes the raw markdown body and returns the
// same shape without touching disk. Used by ImportBriefButton to gate the
// createBrief() call so a malformed brief surfaces inline errors instead of
// round-tripping a 4xx.
export interface ValidateMarkdownResponse {
  ok: boolean;
  errors: Array<Record<string, unknown>>;
  connectors: Record<string, ConnectorStatus>;
  compliance_projection?: Record<string, DryRunSourceProjection> | null;
  parsed?: BriefConfig | null;
}

export function validateMarkdown(
  markdown: string,
  opts?: { dry_run?: boolean },
): Promise<ValidateMarkdownResponse> {
  return postJSON<ValidateMarkdownResponse>("/briefs/validate-markdown", {
    markdown,
    dry_run: Boolean(opts?.dry_run),
  });
}

export function runBrief(briefId: string): Promise<RunResponse> {
  return postJSON<RunResponse>(`/briefs/${encodeURIComponent(briefId)}/run`);
}

export function startBrief(briefId: string): Promise<RunResponse> {
  return postJSON<RunResponse>(`/briefs/${encodeURIComponent(briefId)}/start`);
}

export function refreshBrief(briefId: string): Promise<RunResponse> {
  return postJSON<RunResponse>(`/briefs/${encodeURIComponent(briefId)}/refresh`);
}

export function pauseBrief(briefId: string): Promise<PauseResponse> {
  return postJSON<PauseResponse>(`/briefs/${encodeURIComponent(briefId)}/pause`);
}

export function statusBrief(briefId: string): Promise<LiveStatus> {
  return getJSON<LiveStatus>(`/briefs/${encodeURIComponent(briefId)}/status`);
}

// --- F008-HISTORY — per-brief edit history -----------------------------

export interface BriefHistoryEntry {
  timestamp: string;
  byte_size: number;
  sha256: string;
}

/**
 * List snapshot metadata for a brief, most-recent first. Returns `[]` when
 * the brief has never been edited (no history directory on disk).
 */
export function listBriefHistory(
  briefId: string,
): Promise<BriefHistoryEntry[]> {
  return getJSON<BriefHistoryEntry[]>(
    `/briefs/${encodeURIComponent(briefId)}/history`,
  );
}

/**
 * Fetch the raw markdown body of a single snapshot. The sidecar returns
 * `text/markdown` — we decode it to a string for the read-only modal.
 */
export async function getBriefHistorySnapshot(
  briefId: string,
  timestamp: string,
): Promise<string> {
  const path = `/briefs/${encodeURIComponent(briefId)}/history/${encodeURIComponent(timestamp)}`;
  const resp = await sidecarFetch(path);
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`HTTP ${resp.status} on ${path}: ${text.slice(0, 200)}`);
  }
  return resp.text();
}

/**
 * Restore a brief's markdown to a prior snapshot. The sidecar first snapshots
 * the current ``brief.md`` (so the restore itself is undoable), then writes
 * the snapshot content to disk and flips the brief state to DRAFT.
 */
export function restoreBriefSnapshot(
  briefId: string,
  timestamp: string,
): Promise<BriefSummary> {
  return postJSON<BriefSummary>(
    `/briefs/${encodeURIComponent(briefId)}/history/${encodeURIComponent(timestamp)}/restore`,
  );
}

// --- F014-LIB — brief template library ---------------------------------

export interface BriefTemplate {
  id: string;
  title: string;
  description: string;
  // Full markdown body of the example brief. Picker UIs should seed their
  // editor from this field so the user always gets the complete template
  // (F014-LIB fix — `markdown_preview` is capped server-side and would
  // silently truncate any template longer than the cap).
  markdown: string;
  markdown_preview: string;
  mtime: number;
}

/**
 * Fetch the brief template library from the sidecar. The endpoint scans
 * `docs/examples/briefs/` at request time, so it always reflects what's on
 * disk. Throws (like the other helpers) on non-2xx; callers handle the
 * offline / sidecar-down case by falling back to a built-in template set.
 */
export function fetchBriefTemplates(): Promise<BriefTemplate[]> {
  return getJSON<BriefTemplate[]>("/briefs/templates");
}

// --- F008-BUNDLE — portable .tar.gz bundle export -----------------------

export interface ExportBundleOptions {
  /** Absolute directory on the host where the .tar.gz should land. */
  targetDir: string;
  /** When true, the sidecar walks files but writes no archive. */
  dryRun?: boolean;
}

export interface BundleHelloEvent {
  event: "hello";
}
export interface BundlePhaseEvent {
  event: "phase";
  phase: "planning" | "packaging" | "verifying" | string;
}
export interface BundleFileEvent {
  event: "file";
  path: string;
  size_bytes: number;
}
export interface BundleDoneEvent {
  event: "done";
  dest_path: string;
  sha256_hex: string;
  file_count: number;
  total_size_bytes: number;
  dry_run?: boolean;
}
export interface BundleErrorEvent {
  event: "error";
  message: string;
}
export type BundleProgressEvent =
  | BundleHelloEvent
  | BundlePhaseEvent
  | BundleFileEvent
  | BundleDoneEvent
  | BundleErrorEvent;

/**
 * One-shot POST helper (no streaming). Resolves once the server closes the
 * SSE stream, returning the last ``done`` event payload (or rejects on error).
 */
export async function exportBundle(
  briefId: string,
  opts: ExportBundleOptions,
): Promise<BundleDoneEvent> {
  return new Promise((resolve, reject) => {
    let lastDone: BundleDoneEvent | null = null;
    const unsubscribe = streamExportBundle(briefId, opts, (e) => {
      if (e.event === "done") {
        lastDone = e;
      } else if (e.event === "error") {
        unsubscribe();
        reject(new Error(e.message));
      }
    });
    // The stream-closer callback chains through via streamExportBundle's
    // internal completion: poll lastDone on a microtask tick after the
    // underlying fetch completes. We mimic that by attaching a listener via
    // a setTimeout fallback — but simpler is to wrap the helper directly.
    // Since streamExportBundle returns an unsubscribe, we rely on the SSE
    // close to surface the done frame before the stream ends.
    void unsubscribe;
    // The real completion path is: streamExportBundle reads until EOF, calls
    // onEvent with done, then exits. We set a microtask poller:
    const interval = setInterval(() => {
      if (lastDone) {
        clearInterval(interval);
        resolve(lastDone);
      }
    }, 25);
    // Safety timeout — 10 minutes.
    setTimeout(() => {
      clearInterval(interval);
      if (lastDone) resolve(lastDone);
      else reject(new Error("export-bundle timed out"));
    }, 10 * 60 * 1000);
  });
}

// --- BUNDLE-IMPORT — restore a brief bundle from a .tar.gz upload --------

export interface BriefImportResult {
  brief_id: string;
  corpus_name: string;
  files_imported: number;
  warnings: string[];
  timestamp_imported: string;
}

export interface ImportBundleOptions {
  /** Optional override for the effective brief_id (used on 409 retry). */
  renameTo?: string;
  /** Corpus name the imported brief should land under. Defaults to "default". */
  corpusName?: string;
}

/**
 * POST a multipart/form-data upload of a brief bundle (.tar.gz) to
 * ``/briefs/import-bundle``. On non-2xx the underlying error preserves the
 * sidecar's structured body so callers can inspect 409 (already_exists) and
 * surface a rename retry.
 *
 * The thrown Error carries:
 *   - ``.message``  — readable summary (e.g. "HTTP 409 on /briefs/import-bundle: …")
 *   - ``.status``   — numeric HTTP status code (when available)
 *   - ``.body``     — parsed JSON body (when the response was JSON)
 */
export async function importBundle(
  file: File | Blob,
  opts: ImportBundleOptions = {},
): Promise<BriefImportResult> {
  const form = new FormData();
  // FastAPI's UploadFile expects the field name to match the parameter name.
  // We pass a stable filename so the server's tempfile gets the right suffix.
  const filename = file instanceof File ? file.name : "bundle.tar.gz";
  form.append("tarball", file, filename);
  const qs = new URLSearchParams();
  qs.set("corpus_name", opts.corpusName ?? "default");
  if (opts.renameTo) qs.set("rename_to", opts.renameTo);
  const path = `/briefs/import-bundle?${qs.toString()}`;
  const resp = await sidecarFetch(path, {
    method: "POST",
    body: form,
  });
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    let body: unknown = undefined;
    try {
      body = text ? JSON.parse(text) : undefined;
    } catch {
      body = undefined;
    }
    const err = new Error(
      `HTTP ${resp.status} on ${path}: ${text.slice(0, 200)}`,
    ) as Error & { status?: number; body?: unknown };
    err.status = resp.status;
    err.body = body;
    throw err;
  }
  return (await resp.json()) as BriefImportResult;
}

export interface BundleStreamHandlers {
  onEvent?: (e: BundleProgressEvent) => void;
}

/**
 * Stream the bundle build via fetch+ReadableStream (POST cannot use
 * EventSource). Mirrors ``streamExport`` in lib/api/export.ts. Returns an
 * unsubscribe function that aborts the request.
 */
export function streamExportBundle(
  briefId: string,
  opts: ExportBundleOptions,
  onEvent: (e: BundleProgressEvent) => void,
): () => void {
  const ac = new AbortController();
  const url = `/briefs/${encodeURIComponent(briefId)}/export-bundle`;
  (async () => {
    let resp: Response;
    try {
      resp = await sidecarFetch(url, {
        method: "POST",
        headers: {
          Accept: "text/event-stream",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          target_dir: opts.targetDir,
          dry_run: Boolean(opts.dryRun),
        }),
        signal: ac.signal,
      });
    } catch (err) {
      const e = err as { name?: string; message?: string };
      if (e?.name === "AbortError") return;
      onEvent({ event: "error", message: e?.message ?? String(err) });
      return;
    }
    if (!resp.ok || !resp.body) {
      onEvent({ event: "error", message: `export-bundle HTTP ${resp.status}` });
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx: number;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          const lines = frame.split("\n");
          if (lines.every((l) => l.startsWith(":"))) continue;
          let eventName = "message";
          let dataText = "";
          for (const line of lines) {
            if (line.startsWith("event: ")) eventName = line.slice(7).trim();
            else if (line.startsWith("data: ")) dataText = line.slice(6);
          }
          if (eventName === "hello") {
            onEvent({ event: "hello" });
            continue;
          }
          let payload: Record<string, unknown> = {};
          if (dataText) {
            try {
              payload = JSON.parse(dataText) as Record<string, unknown>;
            } catch {
              continue;
            }
          }
          if (eventName === "phase") {
            onEvent({
              event: "phase",
              phase: String(payload.phase ?? ""),
            });
          } else if (eventName === "file") {
            onEvent({
              event: "file",
              path: String(payload.path ?? ""),
              size_bytes: Number(payload.size_bytes ?? 0),
            });
          } else if (eventName === "done") {
            onEvent({
              event: "done",
              dest_path: String(payload.dest_path ?? ""),
              sha256_hex: String(payload.sha256_hex ?? ""),
              file_count: Number(payload.file_count ?? 0),
              total_size_bytes: Number(payload.total_size_bytes ?? 0),
              dry_run: Boolean(payload.dry_run),
            });
          } else if (eventName === "error") {
            onEvent({
              event: "error",
              message: String(payload.message ?? "export-bundle error"),
            });
          }
        }
      }
    } catch (err) {
      const e = err as { name?: string; message?: string };
      if (e?.name === "AbortError") return;
      onEvent({ event: "error", message: e?.message ?? String(err) });
    }
  })();
  return () => ac.abort();
}
