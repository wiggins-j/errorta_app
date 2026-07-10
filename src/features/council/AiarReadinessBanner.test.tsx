// F031-DEMO-CORPUS Task 4 — AIAR readiness banner tests.
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import AiarReadinessBanner from "./AiarReadinessBanner";

afterEach(() => cleanup());

describe("AiarReadinessBanner", () => {
  it("renders nothing when aiar_pin.available is true", () => {
    const { container } = render(<AiarReadinessBanner available={true} />);
    expect(container.innerHTML).toBe("");
  });

  it("renders the banner copy when aiar_pin.available is false", () => {
    render(<AiarReadinessBanner available={false} />);
    const banner = screen.getByTestId("aiar-readiness-banner");
    expect(banner).toBeTruthy();
    expect(banner.textContent).toContain(
      "Council retrieval needs AIAR",
    );
  });

  it("renders the banner copy when aiar_pin probe defaults to false (missing/unknown)", () => {
    // Passing `available={false}` mirrors the missing-aiar_pin code path
    // (the production effect maps `aiar_pin?.available ?? false` → false).
    render(<AiarReadinessBanner available={false} />);
    expect(screen.getByTestId("aiar-readiness-banner")).toBeTruthy();
  });

  it("does not capture clicks intended for the Seed button when rendered", () => {
    let clicked = 0;
    const { container } = render(
      <div>
        <AiarReadinessBanner available={false} />
        <button
          type="button"
          onClick={() => {
            clicked += 1;
          }}
        >
          Seed demo room
        </button>
      </div>,
    );
    const seed = screen.getByRole("button", { name: /seed demo room/i });
    fireEvent.click(seed);
    expect(clicked).toBe(1);
    // Banner and button coexist in the DOM (not modal, not pointer-events:none).
    expect(container.querySelector(".aiar-readiness-banner")).toBeTruthy();
  });

  it("links to the onboarding route", () => {
    render(<AiarReadinessBanner available={false} />);
    const link = screen.getByRole("link", { name: /open onboarding/i });
    expect(link.getAttribute("href")).toContain("onboarding");
  });
});
