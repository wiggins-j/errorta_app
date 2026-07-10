import { describe, expect, it } from "vitest";
import { handleFeedback } from "../src/handlers/feedback";
import { FakeDb, ctxWith, testSigningKey } from "./fake-db";

class FakeR2 {
  puts: Array<{ key: string; bytes: number }> = [];
  async put(key: string, value: ArrayBuffer | Uint8Array | string) {
    const bytes = typeof value === "string" ? value.length : (value as Uint8Array).byteLength;
    this.puts.push({ key, bytes });
  }
  async get() {
    return null;
  }
}

async function ctx(db: FakeDb, r2: FakeR2, over = {}) {
  const { signingKey } = await testSigningKey();
  return { ...ctxWith(db, signingKey, over), r2: r2 as unknown as never };
}

describe("feedback", () => {
  it("stores the bundle in R2 and a row, returns a ticket id", async () => {
    const db = new FakeDb();
    const r2 = new FakeR2();
    const c = await ctx(db, r2);
    const r = await handleFeedback(
      { device_id: "dev-1", kind: "crash", message: "it crashed", bundle: new Uint8Array([1, 2, 3]) },
      c,
    );
    expect(r.status).toBe(201);
    expect(r.body?.ticket_id).toBe("ticket-fixed");
    expect(r2.puts.length).toBe(1);
    expect(db.feedback[0].bundle_r2_key).toBe("feedback/ticket-fixed.zip");
    expect(db.feedback[0].kind).toBe("crash");
  });

  it("fails closed when a bundle is supplied without an R2 binding", async () => {
    const db = new FakeDb();
    const { signingKey } = await testSigningKey();
    const r = await handleFeedback(
      { kind: "bug", bundle: new Uint8Array([1]) },
      ctxWith(db, signingKey),
    );
    expect(r.status).toBe(503);
    expect(r.body?.error).toBe("bundle_storage_unavailable");
    expect(db.feedback).toEqual([]);
  });

  it("still accepts text-only feedback without an R2 binding", async () => {
    const db = new FakeDb();
    const { signingKey } = await testSigningKey();
    const r = await handleFeedback(
      { kind: "suggestion", message: "add dark mode" },
      ctxWith(db, signingKey),
    );
    expect(r.status).toBe(201);
    expect(db.feedback).toHaveLength(1);
  });

  it("rejects overlong messages before writing a row", async () => {
    const db = new FakeDb();
    const r2 = new FakeR2();
    const r = await handleFeedback({ kind: "bug", message: "x".repeat(8_001) }, await ctx(db, r2));
    expect(r.status).toBe(413);
    expect(db.feedback).toEqual([]);
  });

  it("accepts an anonymous report with no device_id and no bundle", async () => {
    const db = new FakeDb();
    const r2 = new FakeR2();
    const r = await handleFeedback({ kind: "suggestion", message: "add dark mode" }, await ctx(db, r2));
    expect(r.status).toBe(201);
    expect(db.feedback[0].device_id).toBeNull();
    expect(db.feedback[0].bundle_r2_key).toBeNull();
    expect(r2.puts.length).toBe(0);
  });

  it("rejects an over-cap bundle", async () => {
    const db = new FakeDb();
    const r2 = new FakeR2();
    const c = await ctx(db, r2, { maxBundleBytes: 4 });
    const r = await handleFeedback({ kind: "bug", bundle: new Uint8Array(5) }, c);
    expect(r.status).toBe(413);
    expect(db.feedback.length).toBe(0);
    expect(r2.puts.length).toBe(0);
  });

  it("normalizes an unknown kind to 'bug'", async () => {
    const db = new FakeDb();
    const r2 = new FakeR2();
    await handleFeedback({ kind: "nonsense", message: "x" }, await ctx(db, r2));
    expect(db.feedback[0].kind).toBe("bug");
  });
});
