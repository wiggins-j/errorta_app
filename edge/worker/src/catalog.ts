// Allowlisted telemetry event names (spec §9). Anything off this list is dropped
// server-side — defense in depth against the client sending an off-catalog name.
// Content is never a field here; only enum names + integer counts + bucket labels.

export const FLOOR_EVENTS = new Set<string>([
  "license_check",
  "app_launch",
  "session_summary",
  "queue_overflow",
]);

export const EXTRA_EVENTS = new Set<string>([
  "feature_used",
  "perf_timing",
  "crash_breadcrumb",
]);

export const FEATURE_NAMES = new Set<string>([
  "judge_run", "corpus_ingest", "brief_collect", "council_run",
  "coding_run", "welcome_ingest", "export_bundle", "watch_start",
]);

export const PERF_OPS = new Set<string>([
  "judge_verdict", "council_turn", "coding_turn", "retrieval",
]);

export const PERF_BUCKETS = new Set<string>(["<1s", "1-5s", "5-15s", "15-60s", ">60s"]);

export function isAllowedEvent(name: string): boolean {
  return FLOOR_EVENTS.has(name) || EXTRA_EVENTS.has(name);
}

export function isFloorEvent(name: string): boolean {
  return FLOOR_EVENTS.has(name);
}
