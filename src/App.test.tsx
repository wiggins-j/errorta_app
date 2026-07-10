// Tests for the inline `AiarPinBadge` rendered by App.tsx.
//
// The badge is intentionally NOT extracted to its own module — these tests
// drive it through the full App render so the production code path is the
// only path exercised.
//
// We mock `./lib/api` at module scope (so SidecarStatusBadge AND App both see
// the same stubbed `sidecarHealth`) and replace every lazy-loaded feature
// module with a trivial stub so the Suspense boundary resolves synchronously
// in the test environment. Onboarding is marked complete via localStorage so
// the main shell — including AiarPinBadge — renders.
//
// Note: AiarPinSource in src/lib/api.ts includes local sources plus "remote".
// Class names follow `pin-${source}` so we assert on `pin-editable`,
// `pin-pinned`, and the null/absent branches.

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SidecarHealth } from "./lib/api";

// --- Module mocks --------------------------------------------------------

// Hoisted ref so tests can mutate the next health payload before render.
const healthRef: { current: SidecarHealth | null } = { current: null };

vi.mock("./lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("./lib/api")>("./lib/api");
  return {
    ...actual,
    sidecarHealth: vi.fn(async () => {
      if (healthRef.current === null) {
        // Mirror the real failure mode so SidecarStatusBadge's catch fires
        // and App's catch in `ping` swallows; AiarPinBadge then renders null.
        throw new Error("sidecar unreachable (test stub)");
      }
      return healthRef.current;
    }),
  };
});

// F-DIST-01 — App now polls /alpha/status on startup. Stub it to a gate-off
// (disabled, unlocked) status so these shell tests don't hit the network and
// the alpha gate never fires. Keeps the "no direct fetch" invariant honest.
vi.mock("./lib/api/alpha", async () => {
  const actual = await vi.importActual<typeof import("./lib/api/alpha")>("./lib/api/alpha");
  return {
    ...actual,
    getAlphaStatus: vi.fn(async () => ({
      gateEnabled: false,
      state: "disabled" as const,
      locked: false,
      reason: null,
      graceUntil: null,
      deviceId: null,
      buildEol: false,
      buildEolRequired: false,
      updateUrl: null,
    })),
  };
});

// Replace every lazy-loaded feature module with a trivial stub. Lazy imports
// need real modules at runtime; vi.mock intercepts the dynamic import.
vi.mock("./features/hardware/index", () => ({
  default: () => <div data-testid="feat-hardware" />,
}));
vi.mock("./features/ollama/index", () => ({
  default: () => <div data-testid="feat-ollama" />,
}));
vi.mock("./features/corpus/index", () => ({
  default: () => <div data-testid="feat-corpus" />,
}));
vi.mock("./features/watch/index", () => ({
  default: () => <div data-testid="feat-watch" />,
}));
vi.mock("./features/shell/index", () => ({
  default: () => <div data-testid="feat-shell" />,
}));
vi.mock("./features/judge/index", () => ({
  default: () => <div data-testid="feat-judge" />,
}));
vi.mock("./features/onboarding/index", () => ({
  default: ({ onComplete }: { onComplete: () => void }) => (
    <button type="button" onClick={onComplete}>
      finish-onboarding-stub
    </button>
  ),
}));

// F103 — these tests exercise the post-startup shell, so pin the startup gate
// to "ready" (its own behavior is covered in App.startup.test.tsx and
// useStartupGate.test.tsx). Without this, App would render the StartupSplash
// and the shell would never mount.
vi.mock("./lib/useStartupGate", () => ({
  useStartupGate: () => ({
    mode: "ready",
    state: {
      phase: "ready",
      elapsedMs: 0,
      sidecarPort: 1,
      developerMode: false,
      lastError: null,
    },
    actions: {
      retry: () => {},
      openLogs: async () => {},
      openLimited: () => {},
      quit: async () => {},
    },
  }),
}));

import App from "./App";

// --- Helpers -------------------------------------------------------------

function setHealth(health: SidecarHealth | null) {
  healthRef.current = health;
}

function baseHealth(overrides: Partial<SidecarHealth> = {}): SidecarHealth {
  return {
    service: "errorta-sidecar",
    version: "0.1.0",
    now: "2026-06-07T00:00:00+00:00",
    aiar_available: true,
    aiar_version: "0.1.0",
    ...overrides,
  };
}

// --- Setup ---------------------------------------------------------------

// happy-dom v20 doesn't expose a real localStorage by default — install a
// minimal in-memory polyfill on globalThis. App.tsx reads/writes through the
// global `localStorage` identifier so this is enough.
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
  return shim;
}

beforeEach(() => {
  installLocalStorageShim();
  // Skip the onboarding gate so AiarPinBadge actually mounts.
  localStorage.setItem("errorta.onboarding.complete", "1");
  setHealth(null);
});

afterEach(() => {
  vi.useRealTimers();
  try {
    localStorage.clear();
  } catch {
    /* shim removed */
  }
  healthRef.current = null;
});

// --- Tests ---------------------------------------------------------------

describe("App > AiarPinBadge", () => {
  it("orders Knowledge tabs as Corpus, Briefs, Folder Watcher", async () => {
    const { container } = render(<App />);

    await waitFor(() => {
      expect(container.querySelector(".shell-root")).not.toBeNull();
    });

    const knowledgeGroup = Array.from(container.querySelectorAll(".sidebar-group")).find(
      (node) => node.querySelector(".sidebar-group-label")?.textContent === "Knowledge",
    );
    expect(knowledgeGroup).toBeTruthy();
    const labels = Array.from(
      (knowledgeGroup as HTMLElement).querySelectorAll(".sidebar-sublist .sidebar-item-label"),
    ).map((node) => node.textContent);
    expect(labels).toEqual(["Corpus", "Briefs", "Folder Watcher"]);
  });

  it("orders Workspace tabs as Council, Coding Team, Rooms, Judge", async () => {
    const { container } = render(<App />);

    await waitFor(() => {
      expect(container.querySelector(".shell-root")).not.toBeNull();
    });

    const workspaceGroup = Array.from(container.querySelectorAll(".sidebar-group")).find(
      (node) => node.querySelector(".sidebar-group-label")?.textContent === "Workspace",
    );
    expect(workspaceGroup).toBeTruthy();
    const labels = Array.from(
      (workspaceGroup as HTMLElement).querySelectorAll(".sidebar-sublist .sidebar-item-label"),
    ).map((node) => node.textContent);
    expect(labels).toEqual(["Council", "Coding Team", "Rooms", "Judge"]);
  });

  it("collapses the left sidebar and persists the rail state", async () => {
    const { container } = render(<App />);

    await waitFor(() => {
      expect(container.querySelector(".shell-root")).not.toBeNull();
    });

    fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));

    expect(container.querySelector(".shell-root-sidebar-collapsed")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Expand sidebar" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Judge/ })).toBeNull();
    expect(localStorage.getItem("errorta.sidebar.collapsed")).toBe("1");
  });

  it("starts with the sidebar collapsed when persisted", async () => {
    localStorage.setItem("errorta.sidebar.collapsed", "1");

    const { container } = render(<App />);

    await waitFor(() => {
      expect(container.querySelector(".shell-root-sidebar-collapsed")).not.toBeNull();
    });
    expect(screen.getByRole("button", { name: "Expand sidebar" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Judge/ })).toBeNull();
  });

  it("uses a single-column shell while onboarding is active", async () => {
    localStorage.removeItem("errorta.onboarding.complete");
    setHealth(null);

    const { container } = render(<App />);

    await screen.findByRole("button", { name: /finish-onboarding-stub/i });
    expect(container.querySelector(".shell-root-onboarding")).not.toBeNull();
    expect(container.querySelector(".main-pane-onboarding")).not.toBeNull();
    expect(container.querySelector(".sidebar")).toBeNull();
  });

  it("transitions onboarding → shell without a hooks-order crash (blank-screen regression)", async () => {
    // Regression: a useMemo placed AFTER the onboarding early-return changed the
    // hook count when onboardingDone flipped false→true, throwing "rendered more
    // hooks than during the previous render" and blanking the whole app.
    localStorage.removeItem("errorta.onboarding.complete");
    setHealth(null);

    const { container } = render(<App />);
    const finish = await screen.findByRole("button", { name: /finish-onboarding-stub/i });

    // Completing onboarding (the skip/finish path) must mount the shell, not crash.
    fireEvent.click(finish);

    await waitFor(() => {
      expect(container.querySelector(".shell-root")).not.toBeNull();
    });
    expect(container.querySelector(".sidebar")).not.toBeNull();
    expect(container.querySelector(".shell-root-onboarding")).toBeNull();
  });

  it("renders no badge when sidecarHealth rejects (health stays null)", async () => {
    // healthRef.current === null => mocked sidecarHealth throws.
    const { container } = render(<App />);
    // Give the effect a chance to settle.
    await waitFor(() => {
      // The shell-root has mounted.
      expect(container.querySelector(".shell-root")).not.toBeNull();
    });
    // No aiar-pin-status node anywhere.
    expect(container.querySelector(".aiar-pin-status")).toBeNull();
  });

  it("renders no badge when health.aiar_pin is missing (undefined)", async () => {
    setHealth(baseHealth({ aiar_pin: undefined }));
    const { container } = render(<App />);
    // Wait long enough for the first ping to settle and React to commit.
    await waitFor(() => {
      expect(container.querySelector(".shell-root")).not.toBeNull();
    });
    // Give the async ping a tick to land; if a badge were going to render,
    // it would render here.
    await new Promise((r) => setTimeout(r, 0));
    expect(container.querySelector(".aiar-pin-status")).toBeNull();
  });

  it("renders version text and keeps the install source in the tooltip (no visible pill)", async () => {
    setHealth(
      baseHealth({
        aiar_version: "0.1.7",
        aiar_pin: { available: true, source: "editable", version: "0.1.7" },
      }),
    );
    const { container } = render(<App />);
    await waitFor(() => {
      expect(container.querySelector(".aiar-pin-status")).not.toBeNull();
    });
    // Version chip text.
    expect(screen.getByText(/aiar\s*0\.1\.7/i)).toBeInTheDocument();
    // The confusing dev-only "source" pill is removed from the chrome...
    expect(container.querySelector(".pin-badge")).toBeNull();
    // ...but the source remains discoverable in the tooltip.
    expect(container.querySelector(".aiar-pin-status")?.getAttribute("title")).toMatch(
      /editable/,
    );
  });

  it("surfaces the 'pinned' source in the tooltip", async () => {
    setHealth(
      baseHealth({
        aiar_version: "0.2.0",
        aiar_pin: { available: true, source: "pinned", version: "0.2.0" },
      }),
    );
    const { container } = render(<App />);
    await waitFor(() => {
      expect(container.querySelector(".aiar-pin-status")).not.toBeNull();
    });
    expect(container.querySelector(".pin-badge")).toBeNull();
    expect(container.querySelector(".aiar-pin-status")?.getAttribute("title")).toMatch(
      /pinned/,
    );
  });

  it("title attribute contains source and version", async () => {
    setHealth(
      baseHealth({
        aiar_version: "0.3.1",
        aiar_pin: { available: true, source: "pinned", version: "0.3.1" },
      }),
    );
    const { container } = render(<App />);
    await waitFor(() => {
      expect(container.querySelector(".aiar-pin-status")).not.toBeNull();
    });
    const status = container.querySelector(".aiar-pin-status") as HTMLElement;
    const title = status.getAttribute("title") ?? "";
    expect(title).toMatch(/pinned/);
    expect(title).toMatch(/0\.3\.1/);
    expect(title).toMatch(/local aiar package/i);
  });

  it("prefers active AIAR runtime status over local package pin", async () => {
    setHealth(
      baseHealth({
        aiar_version: null,
        aiar_pin: { available: false, source: "absent", version: null },
        aiar_runtime: {
          runtime_kind: "aiar-service",
          display_name: "example-host",
          connected: true,
          active_model: "qwen3.5:9b",
        },
      }),
    );
    const { container } = render(<App />);
    await waitFor(() => {
      expect(container.querySelector(".aiar-pin-status")).not.toBeNull();
    });
    expect(screen.getByText("AIAR example-host")).toBeInTheDocument();
    // Remote runtime is shown by the "AIAR <name>" chip; no separate pill.
    expect(container.querySelector(".pin-badge")).toBeNull();
    expect(container.querySelector(".aiar-pin-status")?.getAttribute("title")).toMatch(
      /example-host/,
    );
  });

  it("does NOT issue any direct fetch (uses mocked sidecarHealth)", async () => {
    setHealth(baseHealth({
      aiar_pin: { available: true, source: "editable", version: "0.1.0" },
    }));
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    try {
      render(<App />);
      await waitFor(() => {
        // Either the badge mounts or at least the shell does.
        expect(document.querySelector(".shell-root")).not.toBeNull();
      });
      // The API module is mocked, so no real fetch should fire from
      // anywhere in the App tree under test.
      expect(fetchSpy).not.toHaveBeenCalled();
    } finally {
      fetchSpy.mockRestore();
    }
  });
});
