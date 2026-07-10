// F103 — startup gate state-machine behavior. The sidecarPort module is mocked
// so we drive resolveSidecarBase / getStartupSnapshot deterministically; fetch
// is stubbed for the /healthz probe.
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = {
  resolveSidecarBase: vi.fn(),
  getStartupSnapshot: vi.fn(),
  resetSidecarBaseCache: vi.fn(),
  tauriInvoke: vi.fn(async (_cmd: string) => null as unknown),
};

vi.mock("./sidecarPort", () => ({
  resolveSidecarBase: () => mocks.resolveSidecarBase(),
  getStartupSnapshot: () => mocks.getStartupSnapshot(),
  resetSidecarBaseCache: () => mocks.resetSidecarBaseCache(),
  tauriInvoke: (cmd: string) => mocks.tauriInvoke(cmd),
}));

import { useStartupGate } from "./useStartupGate";

beforeEach(() => {
  mocks.resolveSidecarBase.mockReset();
  mocks.getStartupSnapshot.mockReset();
  mocks.resetSidecarBaseCache.mockReset();
  mocks.tauriInvoke.mockReset();
  mocks.tauriInvoke.mockResolvedValue(null);
  mocks.getStartupSnapshot.mockResolvedValue(null);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useStartupGate", () => {
  it("reaches ready in browser-dev once /healthz succeeds", async () => {
    mocks.resolveSidecarBase.mockResolvedValue({
      kind: "browser-dev",
      base: "http://127.0.0.1:8770",
    });
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true }) as Response));

    const { result } = renderHook(() => useStartupGate());
    await waitFor(() => expect(result.current.mode).toBe("ready"));
    expect(result.current.state.developerMode).toBe(true);
    // The base cache is dropped so the first feature call re-resolves the port.
    expect(mocks.resetSidecarBaseCache).toHaveBeenCalled();
  });

  it("stops elapsed polling after reaching ready", async () => {
    vi.useFakeTimers();
    mocks.resolveSidecarBase.mockResolvedValue({
      kind: "browser-dev",
      base: "http://127.0.0.1:8770",
    });
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true }) as Response));

    const { result } = renderHook(() => useStartupGate());
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(result.current.mode).toBe("ready");
    const elapsed = result.current.state.elapsedMs;
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(result.current.state.elapsedMs).toBe(elapsed);
  });

  it("fails fast when the lifecycle snapshot reports failed", async () => {
    mocks.resolveSidecarBase.mockResolvedValue({ kind: "tauri-starting" });
    mocks.getStartupSnapshot.mockResolvedValue({
      state: "failed",
      port: 0,
      elapsedMs: 5000,
      lastError: "spawn failed: binary missing",
    });

    const { result } = renderHook(() => useStartupGate());
    await waitFor(() => expect(result.current.mode).toBe("failed"));
    expect(result.current.state.lastError).toMatch(/binary missing/);
  });

  it("stays loading on the waiting-for-port phase while the sidecar starts", async () => {
    mocks.resolveSidecarBase.mockResolvedValue({ kind: "tauri-starting" });
    mocks.getStartupSnapshot.mockResolvedValue({
      state: "starting",
      port: 0,
      elapsedMs: 1000,
      lastError: null,
    });

    const { result } = renderHook(() => useStartupGate());
    await waitFor(() =>
      expect(result.current.state.phase).toBe("waiting_for_port"),
    );
    expect(result.current.mode).toBe("loading");
  });

  it("retry resets the base cache, asks Tauri to ensure a sidecar, and re-enters loading", async () => {
    mocks.resolveSidecarBase.mockResolvedValue({ kind: "tauri-starting" });
    mocks.getStartupSnapshot.mockResolvedValue({
      state: "failed",
      port: 0,
      elapsedMs: 5000,
      lastError: "boom",
    });

    const { result } = renderHook(() => useStartupGate());
    await waitFor(() => expect(result.current.mode).toBe("failed"));

    mocks.resetSidecarBaseCache.mockClear();
    act(() => {
      result.current.actions.retry();
    });

    expect(result.current.mode).toBe("loading");
    expect(mocks.resetSidecarBaseCache).toHaveBeenCalled();
    expect(mocks.tauriInvoke).toHaveBeenCalledWith("ensure_sidecar");
  });

  it("limited mode stops the loop and exposes limited mode", async () => {
    mocks.resolveSidecarBase.mockResolvedValue({ kind: "tauri-starting" });
    mocks.getStartupSnapshot.mockResolvedValue({
      state: "failed",
      port: 0,
      elapsedMs: 5000,
      lastError: "boom",
    });
    const { result } = renderHook(() => useStartupGate());
    await waitFor(() => expect(result.current.mode).toBe("failed"));

    act(() => {
      result.current.actions.openLimited();
    });
    expect(result.current.mode).toBe("limited");
  });
});
