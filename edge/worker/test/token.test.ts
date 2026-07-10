import { describe, expect, it } from "vitest";
import { canonicalJson, makePayload, signToken } from "../src/token";
import { testSigningKey } from "./fake-db";

function b64urlDecode(s: string): Uint8Array {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const b64 = (s + pad).replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

describe("token wire format", () => {
  it("canonicalJson sorts keys with no whitespace (matches the sidecar)", () => {
    const payload = makePayload("d", "C", 1, 0); // grace_until = 1
    expect(canonicalJson(payload)).toBe(
      '{"build_channel":"alpha","code":"C","device_id":"d","grace_until":1,"issued_at":1,"program":"alpha","v":1}',
    );
  });

  it("signToken produces payload.signature that verifies with the public key", async () => {
    const { signingKey, publicRaw } = await testSigningKey();
    const token = await signToken(makePayload("device-1", "ERRT-7F3K-9Q2M", 100, 14), signingKey);
    expect(token.split(".").length).toBe(2);

    const [payloadB64, sigB64] = token.split(".");
    const pub = await crypto.subtle.importKey("raw", publicRaw, { name: "Ed25519" }, false, [
      "verify",
    ]);
    const good = await crypto.subtle.verify(
      { name: "Ed25519" },
      pub,
      b64urlDecode(sigB64),
      new TextEncoder().encode(payloadB64),
    );
    expect(good).toBe(true);
  });

  it("grace_until is issued_at + graceDays*86400", () => {
    const p = makePayload("d", "C", 1_000, 14);
    expect(p.grace_until).toBe(1_000 + 14 * 86400);
  });
});
