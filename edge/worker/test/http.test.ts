import { describe, expect, it } from "vitest";
import { hasClientHeader } from "../src/http";

describe("client header gate", () => {
  it("accepts only the desktop client's fixed header value", () => {
    const request = (value?: string) => new Request("https://api.errorta.app/v1/metrics", {
      headers: value ? { "X-Errorta-Client": value } : {},
    });
    expect(hasClientHeader(request("errorta-desktop"))).toBe(true);
    expect(hasClientHeader(request("anything"))).toBe(false);
    expect(hasClientHeader(request())).toBe(false);
  });
});
