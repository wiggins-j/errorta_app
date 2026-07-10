// F103 — App-level startup gating. The shell/Sidebar must not mount until the
// gate reports ready (or limited); the failure state shows the recovery
// actions. The gate itself is mocked here (its behavior lives in
// useStartupGate.test.tsx) so we can assert App's branching directly.
import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { StartupGate, StartupMode } from "./lib/useStartupGate";

const gateRef: { current: StartupGate } = {
  current: makeGate("loading"),
};

function makeGate(mode: StartupMode): StartupGate {
  return {
    mode,
    state: {
      phase: mode === "ready" ? "ready" : mode === "failed" ? "failed" : "waiting_for_healthz",
      elapsedMs: 0,
      sidecarPort: null,
      developerMode: false,
      lastError: mode === "failed" ? "spawn failed" : null,
    },
    actions: {
      retry: vi.fn(),
      openLogs: vi.fn(async () => {}),
      openLimited: vi.fn(),
      quit: vi.fn(async () => {}),
    },
  };
}

vi.mock("./lib/useStartupGate", () => ({
  useStartupGate: () => gateRef.current,
}));

vi.mock("./lib/api", async () => {
  const actual = await vi.importActual<typeof import("./lib/api")>("./lib/api");
  return {
    ...actual,
    // Mirror the real unreachable failure mode so SidecarStatusBadge's catch
    // fires (rather than rendering with a null health and crashing).
    sidecarHealth: vi.fn(async () => {
      throw new Error("sidecar unreachable (test stub)");
    }),
  };
});

vi.mock("./features/hardware/index", () => ({
  default: () => <div data-testid="feat-hardware" />,
}));
vi.mock("./features/judge/index", () => ({
  default: () => <div data-testid="feat-judge" />,
}));
vi.mock("./features/onboarding/index", () => ({
  default: () => <div data-testid="onboarding-stub" />,
}));

import App from "./App";
import { sidecarHealth } from "./lib/api";

function installLocalStorageShim() {
  const store = new Map<string, string>();
  const shim: Storage = {
    get length() {
      return store.size;
    },
    clear() {
      store.clear();
    },
    getItem(key: string) {
      return store.has(key) ? (store.get(key) as string) : null;
    },
    key(index: number) {
      return Array.from(store.keys())[index] ?? null;
    },
    removeItem(key: string) {
      store.delete(key);
    },
    setItem(key: string, value: string) {
      store.set(key, String(value));
    },
  };
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: shim,
  });
}

beforeEach(() => {
  installLocalStorageShim();
  localStorage.setItem("errorta.onboarding.complete", "1");
  vi.mocked(sidecarHealth).mockClear();
});

afterEach(() => {
  vi.useRealTimers();
  try {
    localStorage.clear();
  } catch {
    /* shim removed */
  }
});

describe("App startup gate", () => {
  it("shows the splash and NOT the shell while loading", async () => {
    gateRef.current = makeGate("loading");
    const { container } = render(<App />);
    expect(container.querySelector(".errorta-startup")).not.toBeNull();
    expect(screen.queryByText("Starting Errorta")).not.toBeInTheDocument();
    expect(screen.getByText(/Preparing the local backend/i)).toBeInTheDocument();
    // The shell / sidebar must not be present.
    expect(container.querySelector(".shell-root")).toBeNull();
    expect(container.querySelector(".sidebar")).toBeNull();
    await act(async () => {
      await Promise.resolve();
    });
    expect(sidecarHealth).not.toHaveBeenCalled();
  });

  it("shows the failure actions in the failed state", () => {
    gateRef.current = makeGate("failed");
    const { container } = render(<App />);
    expect(container.querySelector(".errorta-startup")).not.toBeNull();
    expect(
      screen.getByRole("button", { name: /Retry startup/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Open in limited mode/i }),
    ).toBeInTheDocument();
    expect(container.querySelector(".shell-root")).toBeNull();
  });

  it("mounts the shell once ready", async () => {
    gateRef.current = makeGate("ready");
    const { container } = render(<App />);
    await waitFor(() => {
      expect(container.querySelector(".shell-root")).not.toBeNull();
    });
    expect(container.querySelector(".errorta-startup")).toBeNull();
    expect(container.querySelector(".sidebar")).not.toBeNull();
  });

  it("mounts the shell in limited mode (recovery path)", async () => {
    gateRef.current = makeGate("limited");
    const { container } = render(<App />);
    await waitFor(() => {
      expect(container.querySelector(".shell-root")).not.toBeNull();
    });
    // Limited mode forces backend-not-ready, so the degraded banner shows.
    expect(container.querySelector(".backend-banner")).not.toBeNull();
  });
});
