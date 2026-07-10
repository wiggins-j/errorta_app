import { describe, expect, it } from "vitest";
import { CODE_ALPHABET, generateCode, isValidCodeFormat, normalizeCode } from "../src/codes";

describe("invite codes", () => {
  it("normalizes to upper + trimmed", () => {
    expect(normalizeCode("  errt-7f3k-9q2m ")).toBe("ERRT-7F3K-9Q2M");
  });

  it("accepts a well-formed code", () => {
    expect(isValidCodeFormat("ERRT-7F3K-9Q2M")).toBe(true);
  });

  it("rejects ambiguous letters and malformed codes", () => {
    expect(isValidCodeFormat("ERRT-IIII-OOOO")).toBe(false); // I and O excluded
    expect(isValidCodeFormat("ERRT-7F3K9Q2M")).toBe(false); // missing dash
    expect(isValidCodeFormat("XXXX-7F3K-9Q2M")).toBe(false); // wrong prefix
    expect(isValidCodeFormat("errt-7f3k-9q2m")).toBe(false); // must be normalized first
  });

  it("generated codes are well-formed and use only the alphabet", () => {
    const bytes = new Uint8Array(8).map((_, i) => i * 31 + 3);
    const code = generateCode(bytes);
    expect(isValidCodeFormat(code)).toBe(true);
    for (const ch of code.replace(/ERRT-|-/g, "")) expect(CODE_ALPHABET).toContain(ch);
  });
});
