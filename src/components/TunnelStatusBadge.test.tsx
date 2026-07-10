// F-INFRA-12 Phase B Slice 9 — TunnelStatusBadge unit tests.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TunnelStatusBadge } from "./TunnelStatusBadge";

describe("TunnelStatusBadge", () => {
  it("renders 'Local' label for kind=down with role=status", () => {
    render(<TunnelStatusBadge state={{ kind: "down" }} />);
    const badge = screen.getByRole("status");
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveAttribute("data-kind", "down");
    expect(badge).toHaveTextContent("Local");
  });

  it("renders 'Connecting…' for kind=connecting", () => {
    render(<TunnelStatusBadge state={{ kind: "connecting" }} />);
    const badge = screen.getByRole("status");
    expect(badge).toHaveAttribute("data-kind", "connecting");
    expect(badge).toHaveTextContent("Connecting…");
  });

  it("renders 'Tunnel: up' for kind=up", () => {
    render(<TunnelStatusBadge state={{ kind: "up" }} />);
    const badge = screen.getByRole("status");
    expect(badge).toHaveAttribute("data-kind", "up");
    expect(badge).toHaveTextContent("Tunnel: up");
  });

  it("renders error label + detail for kind=error", () => {
    render(
      <TunnelStatusBadge
        state={{ kind: "error", detail: "permission denied" }}
      />,
    );
    const badge = screen.getByRole("status");
    expect(badge).toHaveAttribute("data-kind", "error");
    expect(badge).toHaveTextContent("Tunnel: error");
    expect(badge).toHaveTextContent("permission denied");
    expect(badge).toHaveAttribute(
      "title",
      "Tunnel: error — permission denied",
    );
  });

  it("uses polite aria-live so state changes are announced softly", () => {
    render(<TunnelStatusBadge state={{ kind: "up" }} />);
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
  });

  it("accepts legacy string tunnel states without crashing", () => {
    render(<TunnelStatusBadge state="down" />);
    const badge = screen.getByRole("status");
    expect(badge).toHaveAttribute("data-kind", "down");
    expect(badge).toHaveTextContent("Local");
  });

  it("falls back to down when tunnel state is nullish", () => {
    render(<TunnelStatusBadge state={null} />);
    const badge = screen.getByRole("status");
    expect(badge).toHaveAttribute("data-kind", "down");
    expect(badge).toHaveTextContent("Local");
  });

  it("renders unknown tunnel states as an inline error instead of throwing", () => {
    render(<TunnelStatusBadge state={{ kind: "stale" }} />);
    const badge = screen.getByRole("status");
    expect(badge).toHaveAttribute("data-kind", "error");
    expect(badge).toHaveTextContent("Unknown tunnel state: stale");
  });
});
