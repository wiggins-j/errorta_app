// F-DIST-01 check-in service — Cloudflare Worker entrypoint.
//
// Only /v1/* is served. No CORS (the desktop app is the only client and sets a
// static X-Errorta-Client header). Every dynamic endpoint is low-frequency and
// consented; the high-traffic paths (installers, updater feed) are static on
// GitHub Pages and never touch this Worker.

import { D1Db } from "./db";
import { handleActivate } from "./handlers/activate";
import { handleFeedback, type FeedbackInput } from "./handlers/feedback";
import { handleHeartbeat } from "./handlers/heartbeat";
import { handleMetrics } from "./handlers/metrics";
import { hasClientHeader, jsonResponse, readJsonCapped } from "./http";
import type { HandlerCtx, Result } from "./result";
import { importSigningKey } from "./token";
import type { Env } from "./worker-types";

const DEFAULT_GRACE_DAYS = 14;
const DEFAULT_MAX_BUNDLE_BYTES = 5 * 1024 * 1024; // 5 MiB (spec §12)

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    try {
      return await route(request, env);
    } catch (e) {
      return jsonResponse({ status: 500, body: { error: "internal" } });
    }
  },
};

async function route(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname;
  const method = request.method.toUpperCase();

  if (method === "GET" && path === "/v1/health") {
    return jsonResponse({ status: 200, body: { ok: true } });
  }
  if (!path.startsWith("/v1/")) return jsonResponse({ status: 404, body: { error: "not_found" } });
  if (!hasClientHeader(request)) {
    return jsonResponse({ status: 403, body: { error: "client_header_required" } });
  }
  if (method !== "POST") return jsonResponse({ status: 405, body: { error: "method_not_allowed" } });

  const ctx = await buildCtx(env);

  if (path === "/v1/feedback") return handleFeedbackRoute(request, ctx);

  // JSON endpoints share the read+cap path.
  const parsed = await readJsonCapped(request);
  if (!parsed.ok) return jsonResponse({ status: parsed.status, body: { error: "bad_request" } });

  let result: Result;
  switch (path) {
    case "/v1/activate":
      result = await handleActivate(parsed.value, ctx);
      break;
    case "/v1/heartbeat":
      result = await handleHeartbeat(parsed.value, ctx);
      break;
    case "/v1/metrics":
      result = await handleMetrics(parsed.value, ctx);
      break;
    default:
      result = { status: 404, body: { error: "not_found" } };
  }
  return jsonResponse(result);
}

async function buildCtx(env: Env): Promise<HandlerCtx> {
  return {
    db: new D1Db(env.DB),
    now: Math.floor(Date.now() / 1000),
    graceDays: intFromEnv(env.GRACE_DAYS, DEFAULT_GRACE_DAYS),
    signingKey: await importSigningKey(env.LICENSE_SIGNING_KEY),
    r2: env.BUNDLES,
    maxBundleBytes: intFromEnv(env.MAX_BUNDLE_BYTES, DEFAULT_MAX_BUNDLE_BYTES),
    uuid: () => crypto.randomUUID(),
  };
}

async function handleFeedbackRoute(request: Request, ctx: HandlerCtx): Promise<Response> {
  let form: FormData;
  try {
    form = await request.formData();
  } catch {
    return jsonResponse({ status: 400, body: { error: "bad_multipart" } });
  }
  const input: FeedbackInput = {
    device_id: strField(form, "device_id"),
    kind: strField(form, "kind") ?? undefined,
    message: strField(form, "message") ?? undefined,
    app_version: strField(form, "app_version") ?? undefined,
    bundle: null,
  };
  const bundle = form.get("bundle");
  if (bundle && typeof bundle !== "string") {
    const ab = await (bundle as unknown as Blob).arrayBuffer();
    input.bundle = new Uint8Array(ab);
  }
  return jsonResponse(await handleFeedback(input, ctx));
}

function strField(form: FormData, key: string): string | null {
  const v = form.get(key);
  return typeof v === "string" ? v : null;
}

function intFromEnv(raw: string | undefined, fallback: number): number {
  const n = Number.parseInt(raw ?? "", 10);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}
