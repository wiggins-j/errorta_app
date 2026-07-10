import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import Skeleton from "../Skeleton";

describe("Skeleton", () => {
  it("renders requested rows with pulse class", () => {
    render(<Skeleton variant="verdict" rows={4} />);
    const rows = screen.getAllByTestId("skeleton-row");
    expect(rows).toHaveLength(4);
    rows.forEach((r) => {
      expect(r.className).toMatch(/skeleton-row/);
    });
  });

  it("variant verdict vs metrics renders distinct layouts", () => {
    const { container, rerender } = render(
      <Skeleton variant="verdict" rows={3} />,
    );
    const verdictRoot = container.querySelector(".skeleton") as HTMLElement;
    expect(verdictRoot.getAttribute("data-variant")).toBe("verdict");
    expect(verdictRoot.className).toMatch(/skeleton-verdict/);

    rerender(<Skeleton variant="metrics" rows={3} />);
    const metricsRoot = container.querySelector(".skeleton") as HTMLElement;
    expect(metricsRoot.getAttribute("data-variant")).toBe("metrics");
    expect(metricsRoot.className).toMatch(/skeleton-metrics/);
  });
});
