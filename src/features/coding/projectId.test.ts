import { describe, expect, it } from "vitest";

import { PROJECT_ID_RE, validateProjectId } from "./projectId";

describe("validateProjectId", () => {
  it("accepts the backend-allowed characters", () => {
    for (const ok of ["my-project", "app_1", "Foo.Bar-2", "a", "x".repeat(64)]) {
      expect(validateProjectId(ok)).toBeNull();
      expect(PROJECT_ID_RE.test(ok)).toBe(true);
    }
  });

  it("treats empty / whitespace-only as no-error (submit stays disabled separately)", () => {
    expect(validateProjectId("")).toBeNull();
    expect(validateProjectId("   ")).toBeNull();
  });

  it("ignores a stray leading/trailing space (submit trims)", () => {
    expect(validateProjectId("  my-project  ")).toBeNull();
  });

  it("rejects an internal space and names it", () => {
    const msg = validateProjectId("my project");
    expect(msg).not.toBeNull();
    expect(msg).toMatch(/spaces/i);
  });

  it("rejects other disallowed characters and names them", () => {
    expect(validateProjectId("a/b")).toMatch(/\//);
    expect(validateProjectId("hi!")).toMatch(/!/);
  });

  it("rejects an over-length id", () => {
    expect(validateProjectId("x".repeat(65))).toMatch(/64 characters/);
  });
});
