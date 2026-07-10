import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { LatencyHistogram as LatencyHistogramData } from "../../lib/api/judge";
import LatencyHistogram from "./LatencyHistogram";

function makeHistogram(
  counts: number[],
  percentiles: { p50?: number | null; p95?: number | null; p99?: number | null } = {},
): LatencyHistogramData {
  const labels = ["0-250", "250-500", "500-750", "750-1000", "1000-2000", "2000+"];
  return {
    buckets: labels.map((label, i) => ({ label, count: counts[i] ?? 0 })),
    p50_ms: percentiles.p50 ?? null,
    p95_ms: percentiles.p95 ?? null,
    p99_ms: percentiles.p99 ?? null,
  };
}

describe("LatencyHistogram", () => {
  it("renders exactly 6 rect bars when given non-empty histogram", () => {
    const data = makeHistogram([3, 2, 1, 4, 0, 5], { p50: 300, p95: 1500, p99: 2100 });
    const { container } = render(<LatencyHistogram data={data} />);
    const rects = container.querySelectorAll("rect");
    expect(rects.length).toBe(6);
  });

  it("bar count matches data length", () => {
    const data = makeHistogram([1, 1, 1, 1, 1, 1], { p50: 100, p95: 100, p99: 100 });
    const { container } = render(<LatencyHistogram data={data} />);
    expect(container.querySelectorAll("rect").length).toBe(data.buckets.length);
  });

  it("bar heights scale proportional to counts", () => {
    const data = makeHistogram([1, 2, 4, 0, 0, 0], { p50: 200, p95: 600, p99: 700 });
    const { container } = render(<LatencyHistogram data={data} />);
    const rects = Array.from(container.querySelectorAll("rect"));
    const h0 = parseFloat(rects[0].getAttribute("height") ?? "0");
    const h1 = parseFloat(rects[1].getAttribute("height") ?? "0");
    const h2 = parseFloat(rects[2].getAttribute("height") ?? "0");
    const h3 = parseFloat(rects[3].getAttribute("height") ?? "0");
    expect(h3).toBe(0);
    expect(h1).toBeCloseTo(2 * h0, 5);
    expect(h2).toBeCloseTo(4 * h0, 5);
  });

  it("renders p50/p95/p99 marker lines with correct labels", () => {
    const data = makeHistogram([1, 1, 1, 1, 1, 1], { p50: 350, p95: 1200, p99: 1900 });
    const { container } = render(<LatencyHistogram data={data} />);
    const lines = container.querySelectorAll("line");
    expect(lines.length).toBe(3);
    const text = container.textContent ?? "";
    expect(text).toContain("p50: 350 ms");
    expect(text).toContain("p95: 1200 ms");
    expect(text).toContain("p99: 1900 ms");
    expect(container.querySelector('[data-testid="latency-marker-p50"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="latency-marker-p95"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="latency-marker-p99"]')).not.toBeNull();
  });

  it("renders fallback message when histogram is null", () => {
    render(<LatencyHistogram data={null} />);
    expect(screen.getByText(/Run prompts to populate\./i)).toBeInTheDocument();
  });

  it("renders fallback message when histogram missing", () => {
    render(<LatencyHistogram />);
    expect(screen.getByText(/Run prompts to populate\./i)).toBeInTheDocument();
  });

  it("renders fallback message when all buckets are zero", () => {
    const data = makeHistogram([0, 0, 0, 0, 0, 0]);
    const { container } = render(<LatencyHistogram data={data} />);
    expect(screen.getByText(/Run prompts to populate\./i)).toBeInTheDocument();
    expect(container.querySelectorAll("rect").length).toBe(0);
  });

  it("svg root has aria-label", () => {
    const data = makeHistogram([1, 0, 0, 0, 0, 0], { p50: 100, p95: 100, p99: 100 });
    render(<LatencyHistogram data={data} />);
    expect(screen.getByRole("img", { name: /judge latency/i })).toBeInTheDocument();
  });
});
