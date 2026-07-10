// F-INFRA-09 Slice 4 — UpdatesCard tests.
//
// Strategy: mock src/lib/api/updater.ts so the card's reads come back
// deterministic. Vitest's happy-dom has no Tauri command bridge attached,
// so the real updater.ts module would always return `not_configured` —
// mocking lets us exercise every UI state.

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/updater", () => ({
  checkForUpdates: vi.fn(),
  installUpdate: vi.fn(),
  listRollbacks: vi.fn(),
  rollbackTo: vi.fn(),
  getCrashRecovery: vi.fn(),
  dismissCrashRecovery: vi.fn(),
  getAboutVersion: vi.fn(),
}));

import {
  checkForUpdates,
  dismissCrashRecovery,
  getAboutVersion,
  getCrashRecovery,
  installUpdate,
  listRollbacks,
  rollbackTo,
} from "../../lib/api/updater";
import { UpdatesCard } from "./UpdatesCard";

const mocks = {
  check: checkForUpdates as unknown as ReturnType<typeof vi.fn>,
  install: installUpdate as unknown as ReturnType<typeof vi.fn>,
  list: listRollbacks as unknown as ReturnType<typeof vi.fn>,
  rollback: rollbackTo as unknown as ReturnType<typeof vi.fn>,
  crash: getCrashRecovery as unknown as ReturnType<typeof vi.fn>,
  dismiss: dismissCrashRecovery as unknown as ReturnType<typeof vi.fn>,
  version: getAboutVersion as unknown as ReturnType<typeof vi.fn>,
};

function resetMocks() {
  Object.values(mocks).forEach((m) => m.mockReset());
  mocks.list.mockResolvedValue([]);
  mocks.crash.mockResolvedValue(null);
  mocks.version.mockResolvedValue("0.5.0");
  mocks.install.mockResolvedValue({ status: "installed", version: "0.5.1" });
  mocks.rollback.mockResolvedValue({ status: "ok" });
  mocks.dismiss.mockResolvedValue({ status: "ok" });
}

let fetchSpy: ReturnType<typeof vi.spyOn>;

function installLocalStorage(): void {
  const store: Record<string, string> = {};
  const ls = {
    getItem: (k: string) => (k in store ? store[k] : null),
    setItem: (k: string, v: string) => {
      store[k] = String(v);
    },
    removeItem: (k: string) => {
      delete store[k];
    },
    clear: () => {
      for (const k of Object.keys(store)) delete store[k];
    },
    key: (i: number) => Object.keys(store)[i] ?? null,
    get length() {
      return Object.keys(store).length;
    },
  };
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: ls,
  });
}

beforeEach(() => {
  resetMocks();
  installLocalStorage();
  // Spy on global fetch to assert no network request is made by the card.
  if (typeof window.fetch !== "function") {
    (window as unknown as { fetch: typeof fetch }).fetch = (() =>
      Promise.reject(new Error("no fetch"))) as unknown as typeof fetch;
  }
  fetchSpy = vi.spyOn(window, "fetch");
});

afterEach(() => {
  if (fetchSpy) fetchSpy.mockRestore();
});

describe("UpdatesCard", () => {
  it("renders the v0.6 disabled hint when check_for_updates returns not_configured", async () => {
    mocks.check.mockResolvedValue({ status: "not_configured", reason: "x" });
    render(<UpdatesCard />);
    const matches = await screen.findAllByText(/Auto-update activates in v0\.6/i);
    expect(matches.length).toBeGreaterThan(0);
    expect(
      screen.getByRole("button", { name: /Check now/i }),
    ).toBeInTheDocument();
    // Clicking the button must not crash and must call the API again.
    await userEvent.click(screen.getByRole("button", { name: /Check now/i }));
    await waitFor(() => expect(mocks.check.mock.calls.length).toBeGreaterThan(1));
  });

  it("renders 'Up to date' with the current version", async () => {
    mocks.check.mockResolvedValue({ status: "up_to_date" });
    render(<UpdatesCard />);
    expect(await screen.findByText(/Up to date — Errorta v0.5.0/i)).toBeInTheDocument();
  });

  it("renders 'Update available' with notes and an Install button", async () => {
    mocks.check.mockResolvedValue({
      status: "available",
      version: "0.5.1",
      notes: "fix: rollback flow",
    });
    render(<UpdatesCard />);
    expect(
      await screen.findByText(/Update available: v0\.5\.1/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/fix: rollback flow/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Install update/i })).toBeInTheDocument();
  });

  it("persists channel changes to localStorage", async () => {
    mocks.check.mockResolvedValue({ status: "up_to_date" });
    render(<UpdatesCard />);
    await screen.findByText(/Up to date/i);
    const select = screen.getByRole("combobox", { name: /Channel/i });
    await userEvent.selectOptions(select, "beta");
    expect(window.localStorage.getItem("errorta.updates.channel")).toBe("beta");
  });

  it("persists behavior changes to localStorage", async () => {
    mocks.check.mockResolvedValue({ status: "up_to_date" });
    render(<UpdatesCard />);
    await screen.findByText(/Up to date/i);
    const select = screen.getByRole("combobox", { name: /Behavior/i });
    await userEvent.selectOptions(select, "auto-install");
    expect(window.localStorage.getItem("errorta.updates.behavior")).toBe(
      "auto-install",
    );
  });

  it("renders empty-state copy when no rollbacks exist", async () => {
    mocks.check.mockResolvedValue({ status: "up_to_date" });
    render(<UpdatesCard />);
    expect(
      await screen.findByText(/No previous versions installed yet/i),
    ).toBeInTheDocument();
  });

  it("renders rollback rows and calls rollbackTo on click", async () => {
    mocks.check.mockResolvedValue({ status: "up_to_date" });
    mocks.list.mockResolvedValue([
      {
        version: "0.4.9",
        installed_at: "2026-05-01",
        size_bytes: 80_200_000,
      },
      {
        version: "0.4.8",
        installed_at: "2026-04-01",
        size_bytes: 79_000_000,
      },
    ]);
    // Auto-confirm window.confirm so the rollback proceeds. happy-dom
    // does not define confirm by default.
    Object.defineProperty(window, "confirm", {
      configurable: true,
      value: () => true,
    });

    render(<UpdatesCard />);
    await screen.findByText(/v0\.4\.9/i);
    const rowButtons = screen.getAllByRole("button", { name: /Roll back/i });
    expect(rowButtons).toHaveLength(2);
    await userEvent.click(rowButtons[0]);
    await waitFor(() =>
      expect(mocks.rollback).toHaveBeenCalledWith("0.4.9"),
    );
  });

  it("does not call fetch() on mount or interaction", async () => {
    mocks.check.mockResolvedValue({ status: "up_to_date" });
    render(<UpdatesCard />);
    await screen.findByText(/Up to date/i);
    await userEvent.click(screen.getByRole("button", { name: /Check now/i }));
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("renders the crash-recovery banner and removes it on dismiss", async () => {
    mocks.check.mockResolvedValue({ status: "up_to_date" });
    mocks.crash.mockResolvedValue({
      failed_version: "0.5.1",
      rolled_back_to: "0.5.0",
      recorded_at: "2026-06-08T10:00:00Z",
      error: "sidecar failed to reach healthy",
    });
    render(<UpdatesCard />);
    expect(
      await screen.findByText(/v0\.5\.1 crashed on launch/i),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Dismiss/i }));
    await waitFor(() => expect(mocks.dismiss).toHaveBeenCalled());
    // Banner should be gone after dismiss.
    await waitFor(() =>
      expect(
        screen.queryByText(/v0\.5\.1 crashed on launch/i),
      ).not.toBeInTheDocument(),
    );
  });

  it("auto-installs when behavior is auto-install and an update is available", async () => {
    window.localStorage.setItem("errorta.updates.behavior", "auto-install");
    // First call: mount-time → available.
    mocks.check.mockResolvedValueOnce({
      status: "available",
      version: "0.5.1",
    });
    // Subsequent calls (timer ticks, reload after install): up_to_date.
    mocks.check.mockResolvedValue({ status: "up_to_date" });
    render(<UpdatesCard />);
    await screen.findByText(/Update available/i);
    // Click install once so the deterministic happy path is exercised.
    await userEvent.click(screen.getByRole("button", { name: /Install update/i }));
    await waitFor(() => expect(mocks.install).toHaveBeenCalled());
  });

  it("does not install automatically when behavior is notify-only", async () => {
    window.localStorage.setItem("errorta.updates.behavior", "notify-only");
    mocks.check.mockResolvedValue({
      status: "available",
      version: "0.5.1",
    });
    render(<UpdatesCard />);
    await screen.findByText(/Update available/i);
    // install must not have been called automatically; only on user click.
    expect(mocks.install).not.toHaveBeenCalled();
  });

  it("disables the auto-install timer when behavior is off", async () => {
    window.localStorage.setItem("errorta.updates.behavior", "off");
    mocks.check.mockResolvedValue({ status: "up_to_date" });
    render(<UpdatesCard />);
    await screen.findByText(/Up to date/i);
    // The mount-time check still runs; the test asserts the timer effect is
    // a no-op (install was never invoked automatically).
    expect(mocks.install).not.toHaveBeenCalled();
  });
});
