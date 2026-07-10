// F103 — StartupSplash rendering + the marquee "no circular spinner" invariant
// + failure actions + a11y.
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { StartupSplash } from "./StartupSplash";
import { expectNoA11yViolations } from "../features/council/a11y-helpers";
import type { StartupActions, StartupState } from "../lib/useStartupGate";

function makeState(overrides: Partial<StartupState> = {}): StartupState {
  return {
    phase: "waiting_for_healthz",
    elapsedMs: 0,
    sidecarPort: null,
    developerMode: false,
    lastError: null,
    ...overrides,
  };
}

function makeActions(overrides: Partial<StartupActions> = {}): StartupActions {
  return {
    retry: vi.fn(),
    openLogs: vi.fn(async () => {}),
    openLimited: vi.fn(),
    quit: vi.fn(async () => {}),
    ...overrides,
  };
}

describe("StartupSplash", () => {
  it("renders the brand, detail copy, and phase list while loading", () => {
    render(
      <StartupSplash failed={false} state={makeState()} actions={makeActions()} />,
    );
    expect(screen.queryByText("Starting Errorta")).not.toBeInTheDocument();
    expect(screen.getByText(/Preparing the local backend/i)).toBeInTheDocument();
    expect(screen.getByText("Opening desktop shell")).toBeInTheDocument();
    expect(screen.getByText("Loading local AI services")).toBeInTheDocument();
    // Status region is a polite live region.
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
  });

  it("uses NO circular spinner element or class", () => {
    const { container } = render(
      <StartupSplash failed={false} state={makeState()} actions={makeActions()} />,
    );
    expect(container.querySelector('[class*="spinner"]')).toBeNull();
    // The cold-start signal is a progress rail, not a spinner.
    expect(container.querySelector(".errorta-startup-rail")).not.toBeNull();
  });

  it("shows developer-mode copy when running in browser dev", () => {
    render(
      <StartupSplash
        failed={false}
        state={makeState({ developerMode: true })}
        actions={makeActions()}
      />,
    );
    expect(screen.getByText(/Developer mode/i)).toBeInTheDocument();
  });

  it("shows the long-boot reassurance after 12s without an elapsed counter", () => {
    const { container } = render(
      <StartupSplash
        failed={false}
        state={makeState({ elapsedMs: 13_000 })}
        actions={makeActions()}
      />,
    );
    expect(screen.getByText(/Still starting/i)).toBeInTheDocument();
    // The "Ns elapsed" counter was removed — reassurance copy only, no ticking clock.
    expect(screen.queryByText(/elapsed/i)).not.toBeInTheDocument();
    expect(container.querySelector(".errorta-startup-elapsed")).toBeNull();
  });

  it("renders the four failure actions and wires their handlers", () => {
    const actions = makeActions();
    render(
      <StartupSplash
        failed
        state={makeState({ phase: "failed", lastError: "spawn failed" })}
        actions={actions}
      />,
    );
    expect(screen.getByText(/couldn't start the local backend/i)).toBeInTheDocument();
    expect(screen.getByText(/spawn failed/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Retry startup/i }));
    fireEvent.click(screen.getByRole("button", { name: /Open logs/i }));
    fireEvent.click(screen.getByRole("button", { name: /Open in limited mode/i }));
    fireEvent.click(screen.getByRole("button", { name: /Quit Errorta/i }));
    expect(actions.retry).toHaveBeenCalledTimes(1);
    expect(actions.openLogs).toHaveBeenCalledTimes(1);
    expect(actions.openLimited).toHaveBeenCalledTimes(1);
    expect(actions.quit).toHaveBeenCalledTimes(1);
  });

  it("does not render the phase list / rail in the failed state", () => {
    const { container } = render(
      <StartupSplash
        failed
        state={makeState({ phase: "failed" })}
        actions={makeActions()}
      />,
    );
    expect(container.querySelector(".errorta-startup-rail")).toBeNull();
    expect(container.querySelector(".errorta-startup-steps")).toBeNull();
  });

  it("has no serious/critical a11y violations (loading)", async () => {
    const { container } = render(
      <StartupSplash failed={false} state={makeState()} actions={makeActions()} />,
    );
    await expectNoA11yViolations(container);
  });

  it("has no serious/critical a11y violations (failed)", async () => {
    const { container } = render(
      <StartupSplash
        failed
        state={makeState({ phase: "failed", lastError: "boom" })}
        actions={makeActions()}
      />,
    );
    await expectNoA11yViolations(container);
  });
});
