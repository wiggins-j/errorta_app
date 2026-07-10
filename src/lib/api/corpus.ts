// F004 — drag-and-drop corpus management client.
import { deleteJSON, getJSON, postJSON, sidecarFetch } from "../api";
import { getSidecarBase } from "../sidecarPort";
import type {
  ApplyResult,
  CorpusFilesResponse,
  FormatsResponse,
  RefreshDiffResponse,
  UploadResponse,
} from "../../features/corpus/types";

export type CorpusCatalogSource = "local" | "remote" | string;
export type CorpusCatalogStatus = "ready" | "indexing" | "empty" | "failed" | string;
export type CorpusUnit = "files" | "chunks" | string;
export type CorpusCapability =
  | "list_files"
  | "upload_files"
  | "folder_watch"
  | "refresh_preview"
  | "remote_ingest";

export type CorpusCapabilities = Partial<Record<CorpusCapability, boolean>>;

export interface CorpusSummary {
  name: string;
  fileCount: number;
  readyCount: number;
  status: CorpusCatalogStatus;
  source: CorpusCatalogSource;
  unit?: CorpusUnit;
  capabilities?: CorpusCapabilities;
}

function sourceFallbackCapabilities(source: CorpusCatalogSource): CorpusCapabilities {
  if (source === "local") {
    return {
      list_files: true,
      upload_files: true,
      folder_watch: true,
      refresh_preview: true,
      remote_ingest: false,
    };
  }
  return {
    list_files: false,
    upload_files: false,
    folder_watch: false,
    refresh_preview: false,
    remote_ingest: false,
  };
}

function capabilitiesFrom(
  raw: Record<string, unknown>,
  source: CorpusCatalogSource,
): CorpusCapabilities {
  const fallback = sourceFallbackCapabilities(source);
  const rawCaps = raw.capabilities;
  if (!rawCaps || typeof rawCaps !== "object") return fallback;
  const caps = rawCaps as Record<string, unknown>;
  return {
    ...fallback,
    list_files: Boolean(caps.list_files ?? fallback.list_files),
    upload_files: Boolean(caps.upload_files ?? fallback.upload_files),
    folder_watch: Boolean(caps.folder_watch ?? fallback.folder_watch),
    refresh_preview: Boolean(caps.refresh_preview ?? fallback.refresh_preview),
    remote_ingest: Boolean(caps.remote_ingest ?? fallback.remote_ingest),
  };
}

function corpusSummaryFrom(raw: Record<string, unknown>): CorpusSummary {
  const source = String(raw.source ?? "local");
  return {
    name: String(raw.name ?? ""),
    fileCount: Number(raw.file_count ?? 0),
    readyCount: Number(raw.ready_count ?? 0),
    status: String(raw.status ?? "empty"),
    source,
    unit: String(raw.unit ?? (source === "remote" ? "chunks" : "files")),
    capabilities: capabilitiesFrom(raw, source),
  };
}

export function hasCorpusCapability(
  corpus: Pick<CorpusSummary, "source" | "capabilities"> | null | undefined,
  capability: CorpusCapability,
): boolean {
  if (!corpus) return false;
  const explicit = corpus.capabilities?.[capability];
  if (explicit !== undefined) return explicit;
  return Boolean(sourceFallbackCapabilities(corpus.source)[capability]);
}

export function corpusCountLabel(corpus: CorpusSummary): string {
  if (corpus.source === "unknown") return "missing from catalog";
  const unit = corpus.unit ?? (corpus.source === "remote" ? "chunks" : "files");
  if (unit === "chunks") {
    if (corpus.status === "indexing") {
      return `${corpus.readyCount}/${corpus.fileCount} chunks ready`;
    }
    return `${corpus.readyCount} chunks ready`;
  }
  return `${corpus.readyCount}/${corpus.fileCount} files ready`;
}

export async function listCorpora(): Promise<CorpusSummary[]> {
  const res = await sidecarFetch("/corpora");
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`list corpora failed (${res.status}): ${body.slice(0, 200)}`);
  }
  const payload = (await res.json()) as {
    corpora?: Array<Record<string, unknown>>;
    source?: string;
  };
  return (payload.corpora ?? []).map((c) =>
    corpusSummaryFrom({
      ...c,
      source: c.source ?? payload.source ?? "local",
    }),
  );
}

export function listFiles(name: string): Promise<CorpusFilesResponse> {
  return getJSON(`/corpus/${encodeURIComponent(name)}/files`);
}

export function listFormats(): Promise<FormatsResponse> {
  return getJSON(`/corpus/formats`);
}

export async function uploadFiles(
  name: string,
  files: File[],
  opts: { confirmLarge?: boolean; overwriteDuplicates?: boolean } = {},
): Promise<UploadResponse> {
  const fd = new FormData();
  for (const f of files) fd.append("files", f, f.name);
  fd.append("confirm_large", opts.confirmLarge ? "true" : "false");
  fd.append("overwrite_duplicates", opts.overwriteDuplicates ? "true" : "false");
  // Route through sidecarFetch so the dynamically-resolved ephemeral port is
  // used (the bundled app has no sidecar on the static dev port). FormData
  // bodies are passed through without an overridden Content-Type.
  const r = await sidecarFetch(
    `/corpus/${encodeURIComponent(name)}/upload`,
    { method: "POST", body: fd },
  );
  if (!r.ok) {
    const body = await r.text().catch(() => "");
    throw new Error(`upload failed: HTTP ${r.status} — ${body.slice(0, 200)}`);
  }
  return (await r.json()) as UploadResponse;
}

export function deleteFile(name: string, fileId: string): Promise<unknown> {
  return deleteJSON(
    `/corpus/${encodeURIComponent(name)}/files/${encodeURIComponent(fileId)}`,
  );
}

/**
 * F114 — delete a whole corpus (its manifest + files + chunks). 404 if the
 * corpus is unknown.
 */
export function deleteCorpus(name: string): Promise<unknown> {
  return deleteJSON(`/corpus/${encodeURIComponent(name)}`);
}

export function reingestFile(name: string, fileId: string): Promise<unknown> {
  return postJSON(
    `/corpus/${encodeURIComponent(name)}/files/${encodeURIComponent(fileId)}/reingest`,
  );
}

export function reingestAll(name: string): Promise<unknown> {
  return postJSON(`/corpus/${encodeURIComponent(name)}/reingest`);
}

/**
 * F015 — preview the diff between the corpus's current refresh snapshot and
 * its on-disk state. Returns a structured diff; no mutation is performed.
 */
export function refreshPreview(
  name: string,
  since?: string,
): Promise<RefreshDiffResponse> {
  const qs = since ? `?since=${encodeURIComponent(since)}` : "";
  return getJSON(`/corpus/${encodeURIComponent(name)}/refresh-preview${qs}`);
}

/**
 * F015-APPLY — apply a refresh diff. If `diff` is omitted, the sidecar
 * recomputes the diff before applying. Returns ApplyResult listing the
 * file_ids ingested/removed/updated, plus any per-file errors.
 */
export function refreshApply(
  name: string,
  diff?: RefreshDiffResponse,
): Promise<ApplyResult> {
  return postJSON(`/corpus/${encodeURIComponent(name)}/refresh-apply`, diff);
}

/**
 * Subscribe to the sidecar's SSE event stream. Calls `onEvent` per file-status
 * update. Returns a function that closes the stream.
 */
export function subscribeEvents(
  onEvent: (ev: { type: string; corpus: string; file_id: string; status: string; error?: string | null; chunk_count: number; token_count: number; progress: number }) => void,
): () => void {
  // Resolve the ephemeral sidecar port before opening the stream (the static
  // dev port is dead in the bundled app). EventSource can't go through
  // sidecarFetch, so resolve the base first; the close() handle stays sync.
  let es: EventSource | null = null;
  let closed = false;
  void getSidecarBase().then((base) => {
    if (closed) return;
    es = new EventSource(`${base}/corpus/events`);
    es.onmessage = (e) => {
      try {
        onEvent(JSON.parse(e.data));
      } catch {
        // ignore malformed event
      }
    };
  });
  return () => {
    closed = true;
    es?.close();
  };
}
