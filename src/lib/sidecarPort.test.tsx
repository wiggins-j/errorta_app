// F103 — startup-safe resolver tests. The key invariant: while a Tauri sidecar
// is still starting (sidecar_port == 0), the resolver reports "tauri-starting"
// and NEVER the dev fallback base, so the startup gate keeps waiting instead of
// caching 127.0.0.1:8770 for a packaged build.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const invokeMock = vi.fn();

vi.mock("@tauri-apps/api/core", () => ({
  invoke: (cmd: string) => invokeMock(cmd),
}));

import { resolveSidecarBase, getStartupSnapshot } from "./sidecarPort";

function setTauri(on: boolean) {
  if (on) {
    (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
  } else {
    delete (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  }
}

beforeEach(() => {
  invokeMock.mockReset();
});

afterEach(() => {
  setTauri(false);
});

describe("resolveSidecarBase", () => {
  it("returns browser-dev outside Tauri", async () => {
    setTauri(false);
    const res = await resolveSidecarBase();
    expect(res.kind).toBe("browser-dev");
    if (res.kind === "browser-dev") {
      expect(res.base).toMatch(/^http:\/\//);
    }
    // The Tauri command must not even be consulted in browser-dev.
    expect(invokeMock).not.toHaveBeenCalled();
  });

  it("returns tauri-starting when the port is still 0", async () => {
    setTauri(true);
    invokeMock.mockResolvedValue(0);
    const res = await resolveSidecarBase();
    expect(res.kind).toBe("tauri-starting");
  });

  it("returns tauri-starting when the command rejects (not registered yet)", async () => {
    setTauri(true);
    invokeMock.mockRejectedValue(new Error("command not found"));
    const res = await resolveSidecarBase();
    expect(res.kind).toBe("tauri-starting");
  });

  it("returns the tauri base + port once a port is published", async () => {
    setTauri(true);
    invokeMock.mockResolvedValue(19342);
    const res = await resolveSidecarBase();
    expect(res.kind).toBe("tauri");
    if (res.kind === "tauri") {
      expect(res.port).toBe(19342);
      expect(res.base).toBe("http://127.0.0.1:19342");
    }
  });
});

describe("getStartupSnapshot", () => {
  it("returns null outside Tauri", async () => {
    setTauri(false);
    expect(await getStartupSnapshot()).toBeNull();
  });

  it("normalizes a valid snapshot", async () => {
    setTauri(true);
    invokeMock.mockResolvedValue({
      state: "failed",
      port: 0,
      elapsed_ms: 4200,
      last_error: "boom",
    });
    const snap = await getStartupSnapshot();
    expect(snap).toEqual({
      state: "failed",
      port: 0,
      elapsedMs: 4200,
      lastError: "boom",
    });
  });

  it("returns null on an unknown state value", async () => {
    setTauri(true);
    invokeMock.mockResolvedValue({ state: "weird" });
    expect(await getStartupSnapshot()).toBeNull();
  });
});
