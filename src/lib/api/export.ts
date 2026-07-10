// F010 — USB export API client.
//
// Two endpoints:
//   POST /export/plan -> JSON ExportPlanResponse
//   POST /export/run  -> Server-Sent Events streaming progress
//
// EventSource cannot POST, so we use fetch + ReadableStream (mirroring
// `replayCorpusStream` in lib/api/judge.ts) with Accept: text/event-stream.

import { postJSON, sidecarFetch } from "../api";

export interface ExportPlanRequest {
  target_dir: string;
  corpora_list: string[];
  include_models?: boolean;
}

export interface ExportPlanResponse {
  files_count: number;
  total_size_bytes: number;
  corpora: string[];
  dest_root: string;
}

export interface ExportFileEvent {
  event: "file";
  file_index: number;
  file_path: string | null;
  bytes_done: number;
  bytes_total: number;
  size_bytes: number;
}

export interface ExportPhaseEvent {
  event: "phase";
  phase: "copying" | "verifying" | string;
}

export interface ExportDoneEvent {
  event: "done";
  summary: {
    files_copied: number;
    bytes_written: number;
    duration_s: number;
    manifest_path: string | null;
  };
}

export interface ExportErrorEvent {
  event: "error";
  error: string;
}

export interface ExportHelloEvent {
  event: "hello";
}

export type ExportProgressEvent =
  | ExportHelloEvent
  | ExportPhaseEvent
  | ExportFileEvent
  | ExportDoneEvent
  | ExportErrorEvent;

export interface ImportResult {
  corpora_imported: string[];
  files_copied: number;
  total_bytes: number;
  errors: string[];
}

/**
 * Thrown by ``importBundle`` when the backend rejects the upload with HTTP
 * 409 because one or more corpus names already exist in ``~/.errorta/corpora``.
 * The UI surfaces ``conflictingCorpora`` so the user can rename or cancel.
 */
export class CorpusCollisionError extends Error {
  conflictingCorpora: string[];
  constructor(conflictingCorpora: string[]) {
    super(
      "corpus name(s) already exist in target: " +
        conflictingCorpora.join(", "),
    );
    this.name = "CorpusCollisionError";
    this.conflictingCorpora = conflictingCorpora;
  }
}

/**
 * POST /export/import — upload a tarball produced by /export/run (after the
 * user has packed the dest_root into a .tar.gz on their USB stick).
 *
 * On HTTP 409 throws a typed ``CorpusCollisionError``. Other non-2xx
 * responses raise a generic ``Error`` with the body excerpt.
 */
export async function importBundle(
  file: File | Blob,
  opts: { renameCorpora?: Record<string, string> } = {},
): Promise<ImportResult> {
  const form = new FormData();
  // FormData expects a name when given a Blob — fall back to "bundle.tar.gz".
  const filename =
    (file as File).name && (file as File).name.length > 0
      ? (file as File).name
      : "bundle.tar.gz";
  form.append("tarball", file, filename);
  if (opts.renameCorpora) {
    form.append("rename_corpora", JSON.stringify(opts.renameCorpora));
  }
  const resp = await sidecarFetch("/export/import", {
    method: "POST",
    body: form,
  });
  if (resp.status === 409) {
    let detail: { conflicting_corpora?: string[] } | undefined;
    try {
      const body = (await resp.json()) as {
        detail?: { conflicting_corpora?: string[] };
      };
      detail = body.detail;
    } catch {
      detail = undefined;
    }
    throw new CorpusCollisionError(detail?.conflicting_corpora ?? []);
  }
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(
      `import HTTP ${resp.status}: ${text.slice(0, 200)}`,
    );
  }
  return (await resp.json()) as ImportResult;
}

export function planExport(
  req: ExportPlanRequest,
): Promise<ExportPlanResponse> {
  return postJSON<ExportPlanResponse>("/export/plan", req);
}

/**
 * Stream /export/run via fetch+ReadableStream. Calls ``onEvent`` for every
 * parsed SSE frame. Returns an ``unsubscribe`` function that aborts the
 * underlying request.
 */
export function streamExport(
  req: ExportPlanRequest,
  onEvent: (e: ExportProgressEvent) => void,
): () => void {
  const ac = new AbortController();
  (async () => {
    let resp: Response;
    try {
      resp = await sidecarFetch("/export/run", {
        method: "POST",
        body: JSON.stringify(req),
        headers: { Accept: "text/event-stream" },
        signal: ac.signal,
      });
    } catch (err) {
      const e = err as { name?: string; message?: string };
      if (e?.name === "AbortError") return;
      onEvent({ event: "error", error: e?.message ?? String(err) });
      return;
    }
    if (!resp.ok || !resp.body) {
      onEvent({ event: "error", error: `export HTTP ${resp.status}` });
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
          const isHello = lines.some((l) => l.startsWith("event: hello"));
          const dataLine = lines.find((l) => l.startsWith("data: "));
          if (!dataLine) continue;
          const payloadText = dataLine.slice("data: ".length);
          if (isHello && payloadText.trim() === "{}") {
            onEvent({ event: "hello" });
            continue;
          }
          try {
            const payload = JSON.parse(payloadText) as ExportProgressEvent;
            onEvent(payload);
          } catch {
            // ignore malformed frame
          }
        }
      }
    } catch (err) {
      const e = err as { name?: string; message?: string };
      if (e?.name === "AbortError") return;
      onEvent({ event: "error", error: e?.message ?? String(err) });
    }
  })();
  return () => ac.abort();
}
