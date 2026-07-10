// POST /v1/heartbeat — status (active | revoked | build_eol) + a fresh token,
// and unpack the Tier-1 floor counters into metrics_events. Unknown device -> 404
// (the sidecar treats that as "needs reactivation", never as a lock).

import { type HandlerCtx, type Result, err, ok } from "../result";
import { makePayload, signToken } from "../token";

interface HeartbeatInput {
  device_id?: unknown;
  app_version?: unknown;
  platform?: unknown;
  floor?: unknown;
}

export async function handleHeartbeat(input: HeartbeatInput, ctx: HandlerCtx): Promise<Result> {
  const deviceId = typeof input.device_id === "string" ? input.device_id : "";
  if (!deviceId) return err(400, "missing_fields");
  const appVersion = typeof input.app_version === "string" ? input.app_version : "unknown";
  const platform = typeof input.platform === "string" ? input.platform : "unknown";

  const lic = await ctx.db.getLicense(deviceId);
  if (!lic) return err(404, "unknown_device");

  if (lic.status === "revoked") {
    return { status: 200, body: { status: "revoked", reason: lic.revoke_reason ?? "revoked" } };
  }

  await ctx.db.touchLicense(deviceId, appVersion, ctx.now);
  await recordFloor(deviceId, appVersion, platform, input.floor, ctx);

  const build = await ctx.db.getBuild(appVersion);
  if (build && build.eol_at != null && ctx.now > build.eol_at) {
    return {
      status: 200,
      body: {
        status: "build_eol",
        required: build.eol_required === 1,
        update_url: build.update_url ?? null,
      },
    };
  }

  const token = await signToken(
    makePayload(deviceId, lic.code, ctx.now, ctx.graceDays),
    ctx.signingKey,
  );
  return ok({ status: "active", token, grace_days: ctx.graceDays });
}

/** Unpack the floor object into `app_launch` / `session_summary` floor rows. */
async function recordFloor(
  deviceId: string,
  appVersion: string,
  platform: string,
  floor: unknown,
  ctx: HandlerCtx,
): Promise<void> {
  if (!floor || typeof floor !== "object") return;
  const f = floor as Record<string, unknown>;
  const rows = [];
  const launches = toCount(f.launches);
  const sessions = toCount(f.crash_free_sessions);
  if (launches > 0) {
    rows.push({ device_id: deviceId, event: "app_launch", count: launches, app_version: appVersion, platform, tier: "floor" });
  }
  if (sessions > 0) {
    rows.push({ device_id: deviceId, event: "session_summary", count: sessions, app_version: appVersion, platform, tier: "floor" });
  }
  await ctx.db.recordMetrics(rows, ctx.now);
}

function toCount(v: unknown): number {
  return typeof v === "number" && Number.isInteger(v) && v > 0 ? Math.min(v, 1_000_000) : 0;
}
