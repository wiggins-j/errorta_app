// In-memory Db for unit tests — no D1/workerd runtime required.

import type {
  BuildRow,
  Db,
  FeedbackRow,
  InviteCodeRow,
  LicenseRow,
  MetricRow,
} from "../src/db";

export class FakeDb implements Db {
  codes = new Map<string, InviteCodeRow>();
  licenses = new Map<string, LicenseRow>();
  builds = new Map<string, BuildRow>();
  metrics: Array<MetricRow & { received_at: number }> = [];
  feedback: Array<FeedbackRow & { created_at: number }> = [];

  addCode(row: Partial<InviteCodeRow> & { code: string }) {
    this.codes.set(row.code, {
      code: row.code,
      max_activations: row.max_activations ?? 1,
      activations: row.activations ?? 0,
      expires_at: row.expires_at ?? null,
      disabled: row.disabled ?? 0,
    });
  }

  addBuild(row: Partial<BuildRow> & { build_id: string }) {
    this.builds.set(row.build_id, {
      build_id: row.build_id,
      eol_at: row.eol_at ?? null,
      eol_required: row.eol_required ?? 0,
      update_url: row.update_url ?? null,
    });
  }

  async getInviteCode(code: string) {
    return this.codes.get(code) ?? null;
  }
  async getLicense(deviceId: string) {
    return this.licenses.get(deviceId) ?? null;
  }
  async claimLicense(row: { device_id: string; code: string }) {
    const code = this.codes.get(row.code);
    if (!code || code.activations >= code.max_activations) return false;
    if (this.licenses.has(row.device_id)) throw new Error("unique device_id");
    code.activations += 1;
    this.licenses.set(row.device_id, {
      device_id: row.device_id,
      code: row.code,
      status: "active",
      revoke_reason: null,
    });
    return true;
  }
  async touchLicense() {
    /* last_seen bookkeeping not asserted in these tests */
  }
  async getBuild(buildId: string) {
    return this.builds.get(buildId) ?? null;
  }
  async recordMetrics(rows: MetricRow[], receivedAt: number) {
    for (const r of rows) this.metrics.push({ ...r, received_at: receivedAt });
  }
  async insertFeedback(row: FeedbackRow, createdAt: number) {
    this.feedback.push({ ...row, created_at: createdAt });
  }

  setRevoked(deviceId: string, reason: string) {
    const l = this.licenses.get(deviceId);
    if (l) {
      l.status = "revoked";
      l.revoke_reason = reason;
    }
  }
}

/** Generate a real Ed25519 signing key for token tests. */
export async function testSigningKey(): Promise<{ signingKey: CryptoKey; publicRaw: Uint8Array }> {
  const pair = (await crypto.subtle.generateKey({ name: "Ed25519" }, true, [
    "sign",
    "verify",
  ])) as CryptoKeyPair;
  const publicRaw = new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey));
  return { signingKey: pair.privateKey, publicRaw };
}

export function ctxWith(
  db: Db,
  signingKey: CryptoKey,
  overrides: Partial<{ now: number; graceDays: number; uuid: () => string; maxBundleBytes: number }> = {},
) {
  return {
    db,
    signingKey,
    now: overrides.now ?? 1_751_328_000,
    graceDays: overrides.graceDays ?? 14,
    uuid: overrides.uuid ?? (() => "ticket-fixed"),
    maxBundleBytes: overrides.maxBundleBytes ?? 5 * 1024 * 1024,
  };
}
