// POST /v1/metrics — batched Tier-2 extras. Drop any event whose name isn't in
// the allowlisted catalog (defense in depth; the client also drops off-list).
// Active, known devices receive 202; unknown/revoked devices are rejected.

import { FEATURE_NAMES, PERF_BUCKETS, PERF_OPS } from "../catalog";
import type { MetricRow } from "../db";
import { type HandlerCtx, type Result } from "../result";

interface MetricsInput {
  device_id?: unknown;
  app_version?: unknown;
  platform?: unknown;
  events?: unknown;
}

export async function handleMetrics(input: MetricsInput, ctx: HandlerCtx): Promise<Result> {
  const deviceId = typeof input.device_id === "string" ? input.device_id : "";
  if (!deviceId) return { status: 400, body: { error: "missing_fields" } };
  const license = await ctx.db.getLicense(deviceId);
  if (!license) return { status: 404, body: { error: "unknown_device" } };
  if (license.status === "revoked") return { status: 403, body: { error: "revoked" } };
  const appVersion = boundedString(input.app_version, 100);
  const platform = boundedString(input.platform, 100);

  const events = Array.isArray(input.events) ? input.events : [];
  const rows: MetricRow[] = [];
  for (const raw of events) {
    if (!raw || typeof raw !== "object") continue;
    const e = raw as Record<string, unknown>;
    const event = typeof e.event === "string" ? e.event : "";
    const bucket = safeBucket(event, e);
    if (bucket === undefined) continue;
    rows.push({
      device_id: deviceId,
      event,
      count: toCount(e.count),
      bucket,
      app_version: appVersion,
      platform,
      tier: "extra",
    });
  }
  await ctx.db.recordMetrics(rows, ctx.now);
  return { status: 202, body: null };
}

function safeBucket(event: string, e: Record<string, unknown>): string | null | undefined {
  if (event === "feature_used") {
    return typeof e.name === "string" && FEATURE_NAMES.has(e.name) ? e.name : undefined;
  }
  if (event === "perf_timing") {
    return typeof e.name === "string" && PERF_OPS.has(e.name)
      && typeof e.bucket === "string" && PERF_BUCKETS.has(e.bucket)
      ? `${e.name}:${e.bucket}` : undefined;
  }
  if (event === "crash_breadcrumb") {
    return typeof e.bucket === "string" && /^[A-Za-z0-9_.:@-]{1,200}$/.test(e.bucket)
      ? e.bucket : undefined;
  }
  return undefined;
}

function toCount(value: unknown): number {
  return typeof value === "number" && Number.isInteger(value) && value > 0
    ? Math.min(value, 1_000_000) : 1;
}

function boundedString(value: unknown, max: number): string | null {
  return typeof value === "string" && value.length <= max ? value : null;
}
