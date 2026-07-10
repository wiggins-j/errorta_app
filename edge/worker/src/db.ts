// Data-access seam. Handlers depend on the `Db` interface, so they unit-test
// against an in-memory fake (test/fake-db.ts) with no workerd/D1 runtime. The
// D1Db implementation is the thin production wiring over env.DB.

import type { D1Database } from "./worker-types";

export interface InviteCodeRow {
  code: string;
  max_activations: number;
  activations: number;
  expires_at: number | null;
  disabled: number;
}

export interface LicenseRow {
  device_id: string;
  code: string;
  status: string; // active | revoked
  revoke_reason: string | null;
}

export interface BuildRow {
  build_id: string;
  eol_at: number | null;
  eol_required: number;
  update_url: string | null;
}

export interface MetricRow {
  device_id: string;
  event: string;
  count: number;
  bucket?: string | null;
  app_version?: string | null;
  platform?: string | null;
  tier: string; // floor | extra
}

export interface FeedbackRow {
  ticket_id: string;
  device_id: string | null;
  kind: string;
  message: string | null;
  bundle_r2_key: string | null;
  app_version: string | null;
}

export interface Db {
  getInviteCode(code: string): Promise<InviteCodeRow | null>;
  getLicense(deviceId: string): Promise<LicenseRow | null>;
  claimLicense(row: {
    device_id: string;
    code: string;
    platform: string;
    app_version: string;
    activated_at: number;
  }): Promise<boolean>;
  touchLicense(deviceId: string, appVersion: string, seenAt: number): Promise<void>;
  getBuild(buildId: string): Promise<BuildRow | null>;
  recordMetrics(rows: MetricRow[], receivedAt: number): Promise<void>;
  insertFeedback(row: FeedbackRow, createdAt: number): Promise<void>;
}

export class D1Db implements Db {
  constructor(private readonly d1: D1Database) {}

  async getInviteCode(code: string): Promise<InviteCodeRow | null> {
    return this.d1
      .prepare(
        "SELECT code, max_activations, activations, expires_at, disabled FROM invite_codes WHERE code = ?",
      )
      .bind(code)
      .first<InviteCodeRow>();
  }

  async getLicense(deviceId: string): Promise<LicenseRow | null> {
    return this.d1
      .prepare("SELECT device_id, code, status, revoke_reason FROM licenses WHERE device_id = ?")
      .bind(deviceId)
      .first<LicenseRow>();
  }

  async claimLicense(row: {
    device_id: string;
    code: string;
    platform: string;
    app_version: string;
    activated_at: number;
  }): Promise<boolean> {
    // D1 batch is transactional. The first statement reserves one activation
    // only when capacity remains; changes() carries that result into the seat
    // insert. A uniqueness failure rolls the reservation back with the batch.
    const results = await this.d1.batch([
      this.d1
        .prepare(
          "UPDATE invite_codes SET activations = activations + 1 WHERE code = ? AND activations < max_activations",
        )
        .bind(row.code),
      this.d1
        .prepare(
          "INSERT INTO licenses (device_id, code, status, platform, app_version, activated_at) SELECT ?, ?, 'active', ?, ?, ? WHERE changes() = 1",
        )
        .bind(row.device_id, row.code, row.platform, row.app_version, row.activated_at),
    ]);
    return (results[1]?.meta?.changes ?? 0) === 1;
  }

  async touchLicense(deviceId: string, appVersion: string, seenAt: number): Promise<void> {
    await this.d1
      .prepare("UPDATE licenses SET last_seen_at = ?, app_version = ? WHERE device_id = ?")
      .bind(seenAt, appVersion, deviceId)
      .run();
  }

  async getBuild(buildId: string): Promise<BuildRow | null> {
    return this.d1
      .prepare("SELECT build_id, eol_at, eol_required, update_url FROM builds WHERE build_id = ?")
      .bind(buildId)
      .first<BuildRow>();
  }

  async recordMetrics(rows: MetricRow[], receivedAt: number): Promise<void> {
    if (rows.length === 0) return;
    const stmts = rows.map((r) =>
      this.d1
        .prepare(
          "INSERT INTO metrics_events (device_id, event, count, bucket, app_version, platform, received_at, tier) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        )
        .bind(
          r.device_id,
          r.event,
          r.count,
          r.bucket ?? null,
          r.app_version ?? null,
          r.platform ?? null,
          receivedAt,
          r.tier,
        ),
    );
    await this.d1.batch(stmts);
  }

  async insertFeedback(row: FeedbackRow, createdAt: number): Promise<void> {
    await this.d1
      .prepare(
        "INSERT INTO feedback (ticket_id, device_id, kind, message, bundle_r2_key, app_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
      )
      .bind(
        row.ticket_id,
        row.device_id,
        row.kind,
        row.message,
        row.bundle_r2_key,
        row.app_version,
        createdAt,
      )
      .run();
  }
}
