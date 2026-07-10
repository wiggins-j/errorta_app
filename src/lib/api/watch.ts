// F005 — folder watch + auto-ingest client.
import { getJSON, postJSON } from "../api";
import type {
  DeletionPolicy,
  PathCheck,
  WatchStatus,
  WatchStatusList,
} from "../../features/watch/types";

export interface StartWatchRequest {
  corpus: string;
  watched_path: string;
  deletion_policy?: DeletionPolicy;
  type_filter?: string[];
  extra_ignores?: string[];
}

export function start(req: StartWatchRequest): Promise<WatchStatus> {
  return postJSON<WatchStatus>("/watch/start", req);
}

export function stop(corpus: string): Promise<{ stopped: boolean; corpus: string }> {
  return postJSON("/watch/stop", { corpus });
}

export function pause(corpus: string): Promise<{ paused: boolean; corpus: string }> {
  return postJSON("/watch/pause", { corpus });
}

export function resume(corpus: string): Promise<{ paused: boolean; corpus: string }> {
  return postJSON("/watch/resume", { corpus });
}

export function changePath(corpus: string, watched_path: string): Promise<WatchStatus> {
  return postJSON<WatchStatus>("/watch/change-path", { corpus, watched_path });
}

export function setDeletionPolicy(
  corpus: string,
  deletion_policy: DeletionPolicy,
): Promise<WatchStatus> {
  return postJSON<WatchStatus>("/watch/set-deletion-policy", {
    corpus,
    deletion_policy,
  });
}

export function forceRescan(corpus: string): Promise<WatchStatus> {
  return postJSON<WatchStatus>("/watch/force-rescan", { corpus });
}

export function checkPath(path: string, type_filter: string[] = []): Promise<PathCheck> {
  return postJSON<PathCheck>("/watch/check-path", { path, type_filter });
}

export function status(corpus?: string): Promise<WatchStatus | WatchStatusList> {
  const q = corpus ? `?corpus=${encodeURIComponent(corpus)}` : "";
  return getJSON<WatchStatus | WatchStatusList>(`/watch/status${q}`);
}
