import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AlphaGate from "./AlphaGate";
import type { AlphaStatus } from "../../lib/api/alpha";

afterEach(cleanup);

function status(over: Partial<AlphaStatus>): AlphaStatus {
  return {
    gateEnabled: true,
    state: "unactivated",
    locked: true,
    reason: null,
    graceUntil: null,
    deviceId: null,
    buildEol: false,
    buildEolRequired: false,
    updateUrl: null,
    ...over,
  };
}

describe("AlphaGate", () => {
  it("renders a blocking status check before the first response", () => {
    render(<AlphaGate status={null} onActivated={vi.fn()} />);
    expect(screen.getByRole("heading")).toHaveTextContent(/checking alpha access/i);
    expect(screen.queryByLabelText("Invite code")).toBeNull();
  });

  it("renders the activation screen when unactivated", () => {
    render(<AlphaGate status={status({ state: "unactivated" })} onActivated={vi.fn()} />);
    expect(screen.getByRole("heading")).toHaveTextContent(/welcome to the errorta alpha/i);
    expect(screen.getByLabelText("Invite code")).toBeInTheDocument();
  });

  it("renders the lock screen for revoked / expired / eol", () => {
    for (const st of [
      status({ state: "revoked" }),
      status({ state: "expired" }),
      status({ state: "active", buildEolRequired: true, updateUrl: "https://x" }),
    ]) {
      const { unmount } = render(<AlphaGate status={st} onActivated={vi.fn()} />);
      expect(screen.queryByLabelText("Invite code")).toBeNull();
      unmount();
    }
  });
});
