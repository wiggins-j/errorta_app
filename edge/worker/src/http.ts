// Request/response helpers: JSON body cap, client-header gate, response building.

import type { Result } from "./result";

export const MAX_JSON_BYTES = 64 * 1024; // 64 KiB (spec §6)
export const CLIENT_HEADER_VALUE = "errorta-desktop";

export function jsonResponse(result: Result): Response {
  const status = result.status;
  if (result.body === null) return new Response(null, { status });
  return new Response(JSON.stringify(result.body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export function hasClientHeader(request: Request): boolean {
  return request.headers.get("x-errorta-client") === CLIENT_HEADER_VALUE;
}

/** Read + parse a JSON body, rejecting anything over the cap. Returns
 *  `{ ok:false }` on oversize/invalid so the caller can 400/413. */
export async function readJsonCapped(
  request: Request,
): Promise<{ ok: true; value: Record<string, unknown> } | { ok: false; status: number }> {
  const buf = await request.arrayBuffer();
  if (buf.byteLength > MAX_JSON_BYTES) return { ok: false, status: 413 };
  try {
    const value = JSON.parse(new TextDecoder().decode(buf));
    if (!value || typeof value !== "object") return { ok: false, status: 400 };
    return { ok: true, value: value as Record<string, unknown> };
  } catch {
    return { ok: false, status: 400 };
  }
}
