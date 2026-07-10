import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import AlphaUpdateBanner from "./AlphaUpdateBanner";
import type { AlphaStatus } from "../../lib/api/alpha";
import { expectNoA11yViolations } from "../council/a11y-helpers";

afterEach(cleanup);

function status(over: Partial<AlphaStatus>): AlphaStatus {
  return {
    gateEnabled: true,
    state: "active",
    locked: false,
    reason: null,
    graceUntil: null,
    deviceId: "d",
    buildEol: false,
    buildEolRequired: false,
    updateUrl: null,
    ...over,
  };
}

describe("AlphaUpdateBanner", () => {
  it("renders nothing when there's no soft EOL", () => {
    const { container } = render(<AlphaUpdateBanner status={status({})} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing for null status", () => {
    const { container } = render(<AlphaUpdateBanner status={null} />);
    expect(container.firstChild).toBeNull();
  });

  it("shows a non-blocking banner + link for a soft EOL", () => {
    render(<AlphaUpdateBanner status={status({ buildEol: true, updateUrl: "https://errorta.app/dl" })} />);
    expect(screen.getByRole("status")).toHaveTextContent(/newer errorta alpha build/i);
    expect(screen.getByRole("link", { name: "Get the update" })).toHaveAttribute(
      "href",
      "https://errorta.app/dl",
    );
  });

  it("drops a non-https update_url (no javascript:/data: link injection)", () => {
    render(
      <AlphaUpdateBanner
        status={status({ buildEol: true, updateUrl: "javascript:alert(1)" })}
      />,
    );
    // Banner still shows, but the unsafe URL is not rendered as a link.
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("does NOT render when locked (the LockScreen handles required EOL)", () => {
    const { container } = render(
      <AlphaUpdateBanner status={status({ buildEol: true, buildEolRequired: true, locked: true })} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("has no serious/critical a11y violations", async () => {
    // No updateUrl: happy-dom can't evaluate link-in-text-block (a known
    // limitation, like color-contrast), so a11y-check the linkless structure;
    // the link render is covered by the functional test above.
    const { container } = render(<AlphaUpdateBanner status={status({ buildEol: true })} />);
    await expectNoA11yViolations(container);
  });
});
