import { beforeEach, describe, expect, it } from "vitest";
import { handleActivate } from "../src/handlers/activate";
import { FakeDb, ctxWith, testSigningKey } from "./fake-db";

const CODE = "ERRT-7F3K-9Q2M";
let db: FakeDb;
let ctx: Awaited<ReturnType<typeof mkCtx>>;

async function mkCtx(d: FakeDb) {
  const { signingKey } = await testSigningKey();
  return ctxWith(d, signingKey);
}

beforeEach(async () => {
  db = new FakeDb();
  ctx = await mkCtx(db);
});

describe("activate", () => {
  it("redeems a valid code and consumes exactly one activation", async () => {
    db.addCode({ code: CODE, max_activations: 1 });
    const r = await handleActivate({ code: CODE, device_id: "dev-1", platform: "macos-arm64" }, ctx);
    expect(r.status).toBe(200);
    expect(r.body?.status).toBe("active");
    expect(typeof r.body?.token).toBe("string");
    expect(db.codes.get(CODE)?.activations).toBe(1);
    expect(db.licenses.has("dev-1")).toBe(true);
  });

  it("is idempotent for the same device+code (no extra activation)", async () => {
    db.addCode({ code: CODE, max_activations: 1 });
    await handleActivate({ code: CODE, device_id: "dev-1" }, ctx);
    const again = await handleActivate({ code: CODE, device_id: "dev-1" }, ctx);
    expect(again.status).toBe(200);
    expect(db.codes.get(CODE)?.activations).toBe(1); // unchanged
  });

  it("does not let a revoked device mint a fresh active token", async () => {
    db.addCode({ code: CODE, max_activations: 1, activations: 1 });
    db.licenses.set("dev-1", {
      device_id: "dev-1", code: CODE, status: "revoked", revoke_reason: "left program",
    });
    const r = await handleActivate({ code: CODE, device_id: "dev-1" }, ctx);
    expect(r.status).toBe(403);
    expect(r.body?.error).toBe("license_revoked");
    expect(r.body?.token).toBeUndefined();
  });

  it("keeps an existing active seat idempotent after its invite expires", async () => {
    db.addCode({ code: CODE, max_activations: 1, activations: 1, expires_at: ctx.now - 1 });
    db.licenses.set("dev-1", {
      device_id: "dev-1", code: CODE, status: "active", revoke_reason: null,
    });
    const r = await handleActivate({ code: CODE, device_id: "dev-1" }, ctx);
    expect(r.status).toBe(200);
    expect(typeof r.body?.token).toBe("string");
    expect(db.codes.get(CODE)?.activations).toBe(1);
  });

  it("enforces the activation cap at the claim operation, not the stale read", async () => {
    db.addCode({ code: CODE, max_activations: 1 });
    const originalGet = db.getInviteCode.bind(db);
    db.getInviteCode = async (code: string) => {
      const row = await originalGet(code);
      return row ? { ...row, activations: 0 } : null;
    };
    await handleActivate({ code: CODE, device_id: "dev-1" }, ctx);
    const r = await handleActivate({ code: CODE, device_id: "dev-2" }, ctx);
    expect(r.status).toBe(409);
    expect(r.body?.error).toBe("code_exhausted");
    expect(db.licenses.has("dev-2")).toBe(false);
  });

  it("rejects a new device once the code is exhausted", async () => {
    db.addCode({ code: CODE, max_activations: 1, activations: 1 });
    const r = await handleActivate({ code: CODE, device_id: "dev-2" }, ctx);
    expect(r.status).toBe(409);
    expect(r.body?.error).toBe("code_exhausted");
  });

  it("rejects a device already bound to another code", async () => {
    db.addCode({ code: CODE });
    db.addCode({ code: "ERRT-AAAA-BBBB" });
    db.licenses.set("dev-1", { device_id: "dev-1", code: "ERRT-AAAA-BBBB", status: "active", revoke_reason: null });
    const r = await handleActivate({ code: CODE, device_id: "dev-1" }, ctx);
    expect(r.status).toBe(409);
    expect(r.body?.error).toBe("device_code_mismatch");
  });

  it("404 on unknown code, 410 on disabled/expired", async () => {
    expect((await handleActivate({ code: CODE, device_id: "d" }, ctx)).status).toBe(404);
    db.addCode({ code: CODE, disabled: 1 });
    expect((await handleActivate({ code: CODE, device_id: "d" }, ctx)).body?.error).toBe("code_disabled");
    db.addCode({ code: "ERRT-CCCC-DDDD", expires_at: ctx.now - 1 });
    expect((await handleActivate({ code: "ERRT-CCCC-DDDD", device_id: "d" }, ctx)).body?.error).toBe("code_expired");
  });

  it("400 on missing fields, 404 on malformed code", async () => {
    expect((await handleActivate({ device_id: "d" }, ctx)).status).toBe(400);
    expect((await handleActivate({ code: "not-a-code", device_id: "d" }, ctx)).status).toBe(404);
  });
});
