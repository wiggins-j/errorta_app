import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { MetricsTrendDay } from "../../lib/api/judge";
import PassRateChart, { getBarColor } from "../judge/PassRateChart";

function makeDays(): MetricsTrendDay[] {
  return [
    { date: "2026-06-02", total: 10, pass: 9, pass_rate: 0.9 },
    { date: "2026-06-03", total: 10, pass: 7, pass_rate: 0.7 },
    { date: "2026-06-04", total: 10, pass: 5, pass_rate: 0.5 },
    { date: "2026-06-05", total: 10, pass: 4, pass_rate: 0.4 },
    { date: "2026-06-06", total: 10, pass: 2, pass_rate: 0.2 },
    { date: "2026-06-07", total: 0, pass: 0, pass_rate: null },
    { date: "2026-06-08", total: 10, pass: 8, pass_rate: 0.8 },
  ];
}

describe("getBarColor", () => {
  it("returns var(--ok) for >= 0.7", () => {
    expect(getBarColor(0.7)).toBe("var(--ok)");
    expect(getBarColor(0.85)).toBe("var(--ok)");
    expect(getBarColor(1.0)).toBe("var(--ok)");
  });

  it("returns var(--warn) for [0.4, 0.7)", () => {
    expect(getBarColor(0.4)).toBe("var(--warn)");
    expect(getBarColor(0.55)).toBe("var(--warn)");
    expect(getBarColor(0.69)).toBe("var(--warn)");
  });

  it("returns var(--error) for < 0.4", () => {
    expect(getBarColor(0.0)).toBe("var(--error)");
    expect(getBarColor(0.2)).toBe("var(--error)");
    expect(getBarColor(0.39)).toBe("var(--error)");
    expect(getBarColor(0.39999)).toBe("var(--error)");
  });

  it("returns var(--bg-elevated) for null", () => {
    expect(getBarColor(null)).toBe("var(--bg-elevated)");
  });
});

describe("PassRateChart", () => {
  it("renders exactly 7 <rect> bars when given 7 days of data", () => {
    const { container } = render(<PassRateChart data={makeDays()} />);
    const rects = container.querySelectorAll("rect");
    expect(rects.length).toBe(7);
  });

  it("renders no-data day with height '4' and aria-label including 'no data'", () => {
    const data: MetricsTrendDay[] = [
      { date: "2026-06-08", total: 0, pass: 0, pass_rate: null },
    ];
    const { container } = render(<PassRateChart data={data} />);
    const rect = container.querySelector("rect");
    expect(rect).not.toBeNull();
    expect(rect!.getAttribute("height")).toBe("4");
    expect(rect!.getAttribute("aria-label")).toMatch(/no data/i);
  });

  it("renders pass-rate=0.8 bar with aria-label containing '80%' and '8/10'", () => {
    const data: MetricsTrendDay[] = [
      { date: "2026-06-08", total: 10, pass: 8, pass_rate: 0.8 },
    ];
    const { container } = render(<PassRateChart data={data} />);
    const rect = container.querySelector("rect");
    expect(rect).not.toBeNull();
    const label = rect!.getAttribute("aria-label") ?? "";
    expect(label).toContain("80%");
    expect(label).toContain("8/10");
    const title = rect!.querySelector("title");
    expect(title?.textContent).toContain("80%");
    expect(title?.textContent).toContain("8/10");
  });

  it("renders date labels as MM-DD beneath each bar", () => {
    const data: MetricsTrendDay[] = [
      { date: "2026-06-08", total: 10, pass: 8, pass_rate: 0.8 },
    ];
    const { container } = render(<PassRateChart data={data} />);
    const text = container.querySelector("text");
    expect(text?.textContent).toBe("06-08");
  });

  it("empty data array still renders an accessible svg with role='img'", () => {
    const { container } = render(<PassRateChart data={[]} />);
    const svg = screen.getByRole("img", { name: /7-day pass-rate/i });
    expect(svg).toBeInTheDocument();
    expect(container.querySelectorAll("rect").length).toBe(0);
  });
});
