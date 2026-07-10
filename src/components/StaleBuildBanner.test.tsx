import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StaleBuildBanner } from "./StaleBuildBanner";
import * as api from "../lib/api";
import type { SidecarHealth } from "../lib/api";

function health(over: Partial<SidecarHealth>): SidecarHealth {
  return {
    service: "errorta-sidecar",
    version: "0.1.0",
    now: "2026-06-17T00:00:00Z",
    aiar_available: true,
    ...over,
  };
}

afterEach(() => vi.restoreAllMocks());

describe("appLooksStale", () => {
  it("is stale when there is no build stamp", () => {
    expect(api.appLooksStale(health({}))).toBe(true);
    expect(api.appLooksStale(health({ build: { commit: null } }))).toBe(true);
  });
  it("is stale when grounding is explicitly unsupported", () => {
    expect(
      api.appLooksStale(
        health({ build: { commit: "abc" }, features: { grounding: false } }),
      ),
    ).toBe(true);
  });
  it("is current when stamped and grounding supported", () => {
    expect(
      api.appLooksStale(
        health({ build: { commit: "abc" }, features: { grounding: true } }),
      ),
    ).toBe(false);
  });
  it("does not cry wolf when health is unknown", () => {
    expect(api.appLooksStale(null)).toBe(false);
  });
});

describe("StaleBuildBanner", () => {
  it("shows the rebuild hint for a stale build", async () => {
    vi.spyOn(api, "sidecarHealth").mockResolvedValue(
      health({ build: { commit: null }, features: { grounding: false } }),
    );
    render(<StaleBuildBanner />);
    expect(
      await screen.findByText(/out of date/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/rebuild-app\.sh/)).toBeInTheDocument();
  });

  it("renders nothing for a current build", async () => {
    vi.spyOn(api, "sidecarHealth").mockResolvedValue(
      health({ build: { commit: "abc123", commit_short: "abc123" }, features: { grounding: true } }),
    );
    const { container } = render(<StaleBuildBanner />);
    // give the effect a tick; nothing should appear
    await waitFor(() => expect(api.sidecarHealth).toHaveBeenCalled());
    expect(container.querySelector(".stale-build-banner")).toBeNull();
  });

  it("can be dismissed", async () => {
    vi.spyOn(api, "sidecarHealth").mockResolvedValue(
      health({ build: { commit: null } }),
    );
    render(<StaleBuildBanner />);
    const btn = await screen.findByRole("button", { name: /dismiss/i });
    await userEvent.click(btn);
    expect(screen.queryByText(/out of date/i)).toBeNull();
  });
});
