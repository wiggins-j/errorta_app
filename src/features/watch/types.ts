// F005 — types shared across the watch feature pane.

export type DeletionPolicy = "remove" | "mark_missing";

export interface WatchStatus {
  corpus: string;
  watching: boolean;
  alive?: boolean;
  watched_path?: string;
  started_at?: string;
  deletion_policy?: DeletionPolicy;
  type_filter?: string[];
  extra_ignores?: string[];
  last_scan_at?: string | null;
  last_scan_ok?: boolean;
  last_error?: string | null;
  last_heartbeat?: string | null;
  heartbeat_age_seconds?: number | null;
  stale?: boolean;
  paused?: boolean;
  file_count?: number;
  // F005-PROD: set by POST /watch/force-rescan only. True when the call
  // actually started a new scan; false when another scan was already in
  // flight (e.g. mid backoff) and the caller got the current status back
  // without blocking.
  rescan_started?: boolean;
}

export interface WatchStatusList {
  watchers: WatchStatus[];
}

export interface PathCheck {
  path: string;
  exists: boolean;
  file_count: number;
  total_bytes: number;
  cloud_sync_provider: string | null;
  default_ignores: string[];
  supported_extensions: string[];
}

export type FileSource = "watched" | "uploaded";
