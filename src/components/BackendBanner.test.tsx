import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BackendBanner } from "./BackendBanner";

describe("BackendBanner", () => {
  it("shows a non-blocking degraded message while not ready", () => {
    // F103 — cold-launch is now handled by the StartupSplash; this banner
    // covers in-shell degraded state (backend loss / limited mode).
    render(<BackendBanner ready={false} />);
    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent(/local backend unavailable/i);
    expect(banner).toHaveAttribute("aria-live", "polite");
  });

  it("uses a status dot, not a circular spinner (F103)", () => {
    const { container } = render(<BackendBanner ready={false} />);
    expect(container.querySelector('[class*="spinner"]')).toBeNull();
    expect(container.querySelector(".backend-banner-dot")).not.toBeNull();
  });

  it("renders nothing once ready", () => {
    const { container } = render(<BackendBanner ready={true} />);
    expect(container).toBeEmptyDOMElement();
  });
});
