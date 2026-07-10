// Shared types for the F004 corpus feature.

export type IngestionStatus =
  | "queued"
  | "extracting"
  | "chunking"
  | "embedding"
  | "ready"
  | "failed";

export interface CorpusFile {
  file_id: string;
  original_path: string;
  copied_path: string;
  sha256: string;
  size_bytes: number;
  mime_ext: string;
  status: IngestionStatus;
  error?: string | null;
  chunk_count: number;
  chunk_ids: string[];
  token_count: number;
  ingested_at?: string | null;
  progress: number;
}

export interface CorpusStats {
  file_count: number;
  chunk_count: number;
  token_count: number;
  disk_bytes: number;
}

export interface CorpusFilesResponse {
  corpus: string;
  files: CorpusFile[];
  stats: CorpusStats;
}

export interface UploadResultItem {
  filename: string;
  file_id?: string | null;
  status: "accepted" | "duplicate" | "rejected" | "needs_confirm";
  reason?: string | null;
  sha256?: string | null;
  size_bytes?: number | null;
}

export interface UploadResponse {
  corpus: string;
  results: UploadResultItem[];
}

export interface FormatsResponse {
  extensions: string[];
  large_file_bytes: number;
}

// F015 — corpus refresh preview. Mirrors the sidecar's diff_to_dict() payload.
export interface RefreshDiffEntry {
  original_path: string;
}

export interface RefreshDiffUpdatedEntry {
  old: RefreshDiffEntry;
  new: RefreshDiffEntry;
}

// F015-APPLY — result of POST /corpus/{name}/refresh-apply.
export interface ApplyResultError {
  path: string;
  message: string;
}

export interface ApplyResult {
  corpus?: string;
  ingested: string[];
  removed: string[];
  updated: string[];
  errors: ApplyResultError[];
}

export interface RefreshDiffResponse {
  corpus: string;
  added: RefreshDiffEntry[];
  removed: RefreshDiffEntry[];
  updated: RefreshDiffUpdatedEntry[];
  snapshot_at: string;
  partial: boolean;
}
