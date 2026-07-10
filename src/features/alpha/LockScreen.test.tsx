import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LockScreen from "./LockScreen";
import type { AlphaStatus } from "../../lib/api/alpha";
import { expectNoA11yViolations } from "../council/a11y-helpers";

afterEach(cleanup);

function status(over: Partial<AlphaStatus>): AlphaStatus {
  return {
    gateEnabled: true,
    state: "expired",
    locked: true,
    reason: null,
    graceUntil: null,
    deviceId: "d",
    buildEol: false,
    buildEolRequired: false,
    updateUrl: null,
    ...over,
  };
}

describe("LockScreen", () => {
  it("expired: offers Try again and reconnect copy", () => {
    const onRetry = vi.fn();
    render(<LockScreen status={status({ state: "expired", reason: "grace_expired" })} onRetry={onRetry} />);
    expect(screen.getByRole("heading")).toHaveTextContent(/reconnect/i);
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalled();
  });

  it("revoked: no Try again, contact link present", () => {
    render(<LockScreen status={status({ state: "revoked", reason: "left program" })} onRetry={vi.fn()} />);
    expect(screen.getByRole("heading")).toHaveTextContent(/access has ended/i);
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    expect(screen.getByRole("link", { name: /help@errorta\.app/ })).toHaveAttribute(
      "href",
      "mailto:help@errorta.app",
    );
  });

  it("build EOL: shows the update link", () => {
    render(
      <LockScreen
        status={status({ state: "active", buildEolRequired: true, updateUrl: "https://errorta.app/dl" })}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByRole("heading")).toHaveTextContent(/required update/i);
    expect(screen.getByRole("link", { name: "Get the update" })).toHaveAttribute(
      "href",
      "https://errorta.app/dl",
    );
  });

  it("does not render an unsafe update URL", () => {
    render(
      <LockScreen
        status={status({ state: "active", buildEolRequired: true, updateUrl: "javascript:alert(1)" })}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.queryByRole("link", { name: "Get the update" })).toBeNull();
  });

  it("has no serious/critical a11y violations (each state)", async () => {
    for (const st of [
      status({ state: "expired" }),
      status({ state: "revoked" }),
      status({ state: "active", buildEolRequired: true, updateUrl: "https://errorta.app/dl" }),
    ]) {
      const { container, unmount } = render(<LockScreen status={st} onRetry={vi.fn()} />);
      await expectNoA11yViolations(container);
      unmount();
    }
  });
});
