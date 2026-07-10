// POST /v1/feedback — store the (already redacted) diagnostic bundle in a private
// R2 bucket + a feedback row; return a ticket id. Accepts no device_id (anonymous
// reports from the lock/activation screen). The multipart parse + size cap happen
// in index.ts; this handler receives the parsed pieces.

import { type HandlerCtx, type Result } from "../result";

const ALLOWED_KINDS = new Set(["crash", "suggestion", "bug"]);
const MAX_MESSAGE_CHARS = 8_000;

export interface FeedbackInput {
  device_id?: string | null;
  kind?: string;
  message?: string;
  app_version?: string;
  bundle?: Uint8Array | null;
}

export async function handleFeedback(input: FeedbackInput, ctx: HandlerCtx): Promise<Result> {
  const kind = ALLOWED_KINDS.has(input.kind ?? "") ? (input.kind as string) : "bug";
  if (typeof input.message === "string" && input.message.length > MAX_MESSAGE_CHARS) {
    return { status: 413, body: { error: "message_too_large" } };
  }
  const ticketId = ctx.uuid();

  let bundleKey: string | null = null;
  if (input.bundle && input.bundle.byteLength > 0) {
    if (input.bundle.byteLength > ctx.maxBundleBytes) {
      return { status: 413, body: { error: "bundle_too_large" } };
    }
    if (!ctx.r2) return { status: 503, body: { error: "bundle_storage_unavailable" } };
    bundleKey = `feedback/${ticketId}.zip`;
    await ctx.r2.put(bundleKey, input.bundle);
  }

  await ctx.db.insertFeedback(
    {
      ticket_id: ticketId,
      device_id: input.device_id ?? null,
      kind,
      message: typeof input.message === "string" ? input.message : null,
      bundle_r2_key: bundleKey,
      app_version: typeof input.app_version === "string" ? input.app_version : null,
    },
    ctx.now,
  );

  return { status: 201, body: { ticket_id: ticketId } };
}
