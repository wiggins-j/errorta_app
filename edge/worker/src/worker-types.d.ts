// Minimal ambient types for the Cloudflare bindings this Worker uses, so we can
// typecheck without pulling the full @cloudflare/workers-types package. Only the
// surface actually used is declared.

export interface D1Result<T = Record<string, unknown>> {
  results?: T[];
  success: boolean;
  meta?: { changes?: number };
}

export interface D1PreparedStatement {
  bind(...values: unknown[]): D1PreparedStatement;
  first<T = Record<string, unknown>>(): Promise<T | null>;
  run(): Promise<D1Result>;
  all<T = Record<string, unknown>>(): Promise<D1Result<T>>;
}

export interface D1Database {
  prepare(query: string): D1PreparedStatement;
  batch(statements: D1PreparedStatement[]): Promise<D1Result[]>;
}

export interface R2Bucket {
  put(key: string, value: ArrayBuffer | Uint8Array | string): Promise<unknown>;
  get(key: string): Promise<unknown>;
}

export interface Env {
  DB: D1Database;
  BUNDLES?: R2Bucket;
  LICENSE_SIGNING_KEY: string; // base64 PKCS8 Ed25519 private key (Worker secret)
  GRACE_DAYS?: string;
  MAX_BUNDLE_BYTES?: string;
  FEEDBACK_EMAIL?: string;
}
