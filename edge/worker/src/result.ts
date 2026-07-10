import type { Db } from "./db";
import type { R2Bucket } from "./worker-types";

export type Json = Record<string, unknown>;

export interface Result {
  status: number;
  body: Json | null;
}

export interface HandlerCtx {
  db: Db;
  now: number;
  graceDays: number;
  signingKey: CryptoKey;
  r2?: R2Bucket;
  maxBundleBytes: number;
  /** UUID source — overridable in tests for deterministic ticket ids. */
  uuid: () => string;
}

export function ok(body: Json): Result {
  return { status: 200, body };
}

export function err(status: number, error: string, extra: Json = {}): Result {
  return { status, body: { error, ...extra } };
}
