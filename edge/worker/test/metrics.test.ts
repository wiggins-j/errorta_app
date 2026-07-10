import { describe, expect, it } from "vitest";
import { handleMetrics } from "../src/handlers/metrics";
import { FakeDb, ctxWith, testSigningKey } from "./fake-db";

async function ctx(db: FakeDb) {
  const { signingKey } = await testSigningKey();
  return ctxWith(db, signingKey);
}

describe("metrics", () => {
  it("records allowlisted extras as tier=extra and returns 202", async () => {
    const db = new FakeDb();
    db.licenses.set("dev-1", { device_id: "dev-1", code: "ALPHA", status: "active", revoke_reason: null });
    const r = await handleMetrics(
      {
        device_id: "dev-1",
        events: [
          { event: "feature_used", name: "judge_run", count: 4 },
          { event: "perf_timing", name: "judge_verdict", bucket: "1-5s" },
        ],
      },
      await ctx(db),
    );
    expect(r.status).toBe(202);
    expect(r.body).toBeNull();
    expect(db.metrics.map((m) => m.event).sort()).toEqual(["feature_used", "perf_timing"]);
    expect(db.metrics.map((m) => m.bucket).sort()).toEqual(["judge_run", "judge_verdict:1-5s"]);
    expect(db.metrics.every((m) => m.tier === "extra")).toBe(true);
  });

  it("drops any event name not in the catalog (server-side allowlist)", async () => {
    const db = new FakeDb();
    db.licenses.set("dev-1", { device_id: "dev-1", code: "ALPHA", status: "active", revoke_reason: null });
    await handleMetrics(
      { device_id: "dev-1", events: [{ event: "exfiltrate_prompt" }, { event: "feature_used", name: "judge_run" }] },
      await ctx(db),
    );
    expect(db.metrics.map((m) => m.event)).toEqual(["feature_used"]);
  });

  it("drops arbitrary dimensions instead of persisting content", async () => {
    const db = new FakeDb();
    db.licenses.set("dev-1", { device_id: "dev-1", code: "ALPHA", status: "active", revoke_reason: null });
    await handleMetrics({ device_id: "dev-1", events: [
      { event: "feature_used", name: "/Users/example/secret.pdf", bucket: "prompt text" },
      { event: "perf_timing", name: "judge_verdict", bucket: "3.14159s" },
      { event: "crash_breadcrumb", bucket: "/Users/example/secret.py:12" },
    ] }, await ctx(db));
    expect(db.metrics).toEqual([]);
  });

  it("rejects unknown and revoked devices", async () => {
    const db = new FakeDb();
    expect((await handleMetrics({ device_id: "missing", events: [] }, await ctx(db))).status).toBe(404);
    db.licenses.set("revoked", { device_id: "revoked", code: "ALPHA", status: "revoked", revoke_reason: "done" });
    expect((await handleMetrics({ device_id: "revoked", events: [] }, await ctx(db))).status).toBe(403);
  });

  it("400 on missing device_id", async () => {
    const db = new FakeDb();
    expect((await handleMetrics({ events: [] }, await ctx(db))).status).toBe(400);
  });
});
