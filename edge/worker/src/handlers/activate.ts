// POST /v1/activate — redeem an invite code, bind it to a device, issue a token.
// Idempotent for a matching device_id+code (fresh token, no extra activation).

import { isValidCodeFormat, normalizeCode } from "../codes";
import { type HandlerCtx, type Result, err, ok } from "../result";
import { makePayload, signToken } from "../token";

interface ActivateInput {
  code?: unknown;
  device_id?: unknown;
  platform?: unknown;
  app_version?: unknown;
}

export async function handleActivate(input: ActivateInput, ctx: HandlerCtx): Promise<Result> {
  const deviceId = typeof input.device_id === "string" ? input.device_id : "";
  const rawCode = typeof input.code === "string" ? input.code : "";
  if (!deviceId || !rawCode) return err(400, "missing_fields");
  const code = normalizeCode(rawCode);
  if (!isValidCodeFormat(code)) return err(404, "code_not_found");

  const platform = typeof input.platform === "string" ? input.platform : "unknown";
  const appVersion = typeof input.app_version === "string" ? input.app_version : "unknown";

  const invite = await ctx.db.getInviteCode(code);
  if (!invite) return err(404, "code_not_found");

  const existing = await ctx.db.getLicense(deviceId);
  if (existing) {
    // Reinstall / retry on a device that already holds a seat.
    if (existing.code !== code) return err(409, "device_code_mismatch");
    if (existing.status === "revoked") return err(403, "license_revoked");
    return ok(await mintResponse(deviceId, code, ctx)); // idempotent: no increment
  }

  // Disabled/expired constrain new redemptions, not an already-bound active
  // device's idempotent reinstall path above.
  if (invite.disabled) return err(410, "code_disabled");
  if (invite.expires_at != null && ctx.now > invite.expires_at) return err(410, "code_expired");

  // Reserve capacity + create the seat in one transaction. The earlier read is
  // only for specific error reporting; this operation is the concurrency gate.
  let claimed = false;
  try {
    claimed = await ctx.db.claimLicense({
      device_id: deviceId,
      code,
      platform,
      app_version: appVersion,
      activated_at: ctx.now,
    });
  } catch {
    // A concurrent retry for this device can lose the UNIQUE(device_id) race.
    // Re-read and preserve idempotency without consuming another activation.
    const raced = await ctx.db.getLicense(deviceId);
    if (raced?.code === code && raced.status !== "revoked") {
      return ok(await mintResponse(deviceId, code, ctx));
    }
    if (raced?.status === "revoked") return err(403, "license_revoked");
    if (raced) return err(409, "device_code_mismatch");
    throw new Error("license claim failed");
  }
  if (!claimed) return err(409, "code_exhausted");
  return ok(await mintResponse(deviceId, code, ctx));
}

async function mintResponse(deviceId: string, code: string, ctx: HandlerCtx) {
  const token = await signToken(makePayload(deviceId, code, ctx.now, ctx.graceDays), ctx.signingKey);
  return {
    status: "active",
    token,
    grace_days: ctx.graceDays,
    message: "Welcome to the Errorta alpha.",
  };
}
