// F007 — welcome corpus shared types.

export interface WelcomeOption {
  id: string;
  name: string;
  description: string;
  source_url: string;
  license: string;
  fully_deletable: boolean;
  approx_size_mb: number;
}

export interface WelcomeOptionsResponse {
  options: WelcomeOption[];
}

export interface WelcomeStatus {
  phase:
    | "idle"
    | "downloading"
    | "verifying"
    | "extracting"
    | "ingesting"
    | "done"
    | "error";
  progress: number;
  bytes_downloaded: number;
  bytes_total: number | null;
  eta_seconds: number | null;
  corpus_name: string | null;
  suggested_prompt: string | null;
  error: string | null;
}

export interface WelcomeInstallResult {
  corpus_name: string;
  suggested_prompt: string;
  files_ingested: number;
  bytes_downloaded: number;
  sha256: string;
  f004_invoked: boolean;
  f004_error: string | null;
}
