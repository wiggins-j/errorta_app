import { describe, expect, it, vi, beforeEach } from "vitest";
import { activateAlpha, AlphaActivationError, getAlphaStatus } from "./alpha";
import * as api from "../api";

describe("alpha api client", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("adapts /alpha/status snake_case to camelCase", async () => {
    vi.spyOn(api, "getJSON").mockResolvedValue({
      gate_enabled: true,
      state: "unactivated",
      locked: true,
      reason: "not_activated",
      grace_until: null,
      device_id: null,
      build_eol_required: false,
      update_url: null,
    } as never);
    const s = await getAlphaStatus();
    expect(s.gateEnabled).toBe(true);
    expect(s.state).toBe("unactivated");
    expect(s.locked).toBe(true);
    expect(s.buildEolRequired).toBe(false);
  });

  it("activateAlpha returns adapted status on 200", async () => {
    vi.spyOn(api, "sidecarFetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          gate_enabled: true,
          state: "active",
          locked: false,
          reason: null,
          grace_until: 123,
          device_id: "d",
          build_eol_required: false,
          update_url: null,
        }),
        { status: 200 },
      ),
    );
    const s = await activateAlpha("ERRT-7F3K-9Q2M");
    expect(s.state).toBe("active");
    expect(s.locked).toBe(false);
  });

  it("throws AlphaActivationError carrying the backend error code on 400", async () => {
    vi.spyOn(api, "sidecarFetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: { error: "code_exhausted", message: "used up" } }), {
        status: 400,
      }),
    );
    await expect(activateAlpha("X")).rejects.toMatchObject({
      name: "AlphaActivationError",
      code: "code_exhausted",
    });
  });

  it("sends the tauri origin header + json body", async () => {
    const spy = vi
      .spyOn(api, "sidecarFetch")
      .mockResolvedValue(new Response(JSON.stringify({ state: "active", locked: false }), { status: 200 }));
    await activateAlpha("ERRT-7F3K-9Q2M");
    const [path, init] = spy.mock.calls[0];
    expect(path).toBe("/alpha/activate");
    expect((init?.headers as Record<string, string>)["x-errorta-origin"]).toBe("tauri-ui");
    expect(String(init?.body)).toContain("ERRT-7F3K-9Q2M");
  });

  it("falls back to http_<status> when the error body isn't JSON", async () => {
    vi.spyOn(api, "sidecarFetch").mockResolvedValue(new Response("boom", { status: 500 }));
    const err = await activateAlpha("X").catch((e) => e);
    expect(err).toBeInstanceOf(AlphaActivationError);
    expect(err.code).toBe("http_500");
  });

  it("retries a transient sidecar-unreachable failure, then succeeds", async () => {
    vi.useFakeTimers();
    try {
      const spy = vi
        .spyOn(api, "sidecarFetch")
        .mockRejectedValueOnce(new api.SidecarUnreachableError())
        .mockRejectedValueOnce(new api.SidecarUnreachableError())
        .mockResolvedValueOnce(
          new Response(JSON.stringify({ state: "active", locked: false }), { status: 200 }),
        );
      const p = activateAlpha("ERRT-7F3K-9Q2M");
      await vi.runAllTimersAsync();
      const s = await p;
      expect(s.state).toBe("active");
      expect(spy).toHaveBeenCalledTimes(3); // 1 + 2 retries
    } finally {
      vi.useRealTimers();
    }
  });

  it("does NOT retry a server-side rejection (single attempt, no seat re-claim)", async () => {
    const spy = vi
      .spyOn(api, "sidecarFetch")
      .mockResolvedValue(
        new Response(JSON.stringify({ detail: { error: "code_exhausted" } }), { status: 400 }),
      );
    await expect(activateAlpha("X")).rejects.toMatchObject({ code: "code_exhausted" });
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("gives up with SidecarUnreachableError after exhausting retries", async () => {
    vi.useFakeTimers();
    try {
      const spy = vi
        .spyOn(api, "sidecarFetch")
        .mockRejectedValue(new api.SidecarUnreachableError());
      const p = activateAlpha("X").catch((e) => e);
      await vi.runAllTimersAsync();
      const err = await p;
      expect(err).toBeInstanceOf(api.SidecarUnreachableError);
      expect(spy).toHaveBeenCalledTimes(4); // 1 initial + 3 backoff retries
    } finally {
      vi.useRealTimers();
    }
  });
});
