import { beforeEach, describe, expect, it } from "vitest";
import { handleHeartbeat } from "../src/handlers/heartbeat";
import { FakeDb, ctxWith, testSigningKey } from "./fake-db";

const CODE = "ERRT-7F3K-9Q2M";
let db: FakeDb;
let ctx: Awaited<ReturnType<typeof mk>>;

async function mk(d: FakeDb) {
  const { signingKey } = await testSigningKey();
  return ctxWith(d, signingKey);
}

beforeEach(async () => {
  db = new FakeDb();
  db.licenses.set("dev-1", { device_id: "dev-1", code: CODE, status: "active", revoke_reason: null });
  ctx = await mk(db);
});

describe("heartbeat", () => {
  it("active -> fresh token + grace_days", async () => {
    const r = await handleHeartbeat({ device_id: "dev-1", app_version: "0.6.0-alpha.3" }, ctx);
    expect(r.status).toBe(200);
    expect(r.body?.status).toBe("active");
    expect(typeof r.body?.token).toBe("string");
    expect(r.body?.grace_days).toBe(14);
  });

  it("revoked license -> status revoked, no token", async () => {
    db.setRevoked("dev-1", "left program");
    const r = await handleHeartbeat({ device_id: "dev-1" }, ctx);
    expect(r.body?.status).toBe("revoked");
    expect(r.body?.reason).toBe("left program");
    expect(r.body?.token).toBeUndefined();
    expect(db.metrics).toEqual([]);
  });

  it("EOL build -> build_eol with required + update_url", async () => {
    db.addBuild({ build_id: "0.6.0-alpha.1", eol_at: ctx.now - 1, eol_required: 1, update_url: "https://errorta.app/dl" });
    const r = await handleHeartbeat({ device_id: "dev-1", app_version: "0.6.0-alpha.1" }, ctx);
    expect(r.body?.status).toBe("build_eol");
    expect(r.body?.required).toBe(true);
    expect(r.body?.update_url).toBe("https://errorta.app/dl");
  });

  it("unknown device -> 404 (never a lock)", async () => {
    const r = await handleHeartbeat({ device_id: "ghost" }, ctx);
    expect(r.status).toBe(404);
    expect(r.body?.error).toBe("unknown_device");
  });

  it("unpacks floor counters into metrics_events floor rows", async () => {
    await handleHeartbeat(
      { device_id: "dev-1", app_version: "0.6.0", floor: { launches: 3, crash_free_sessions: 2 } },
      ctx,
    );
    const events = db.metrics.map((m) => [m.event, m.count, m.tier]);
    expect(events).toContainEqual(["app_launch", 3, "floor"]);
    expect(events).toContainEqual(["session_summary", 2, "floor"]);
  });

  it("400 on missing device_id", async () => {
    expect((await handleHeartbeat({}, ctx)).status).toBe(400);
  });
});
