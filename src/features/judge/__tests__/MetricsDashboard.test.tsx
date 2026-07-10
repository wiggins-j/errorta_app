import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MetricsResponse } from "../../../lib/api/judge";
import MetricsDashboard from "../MetricsDashboard";
import { ToastProvider } from "../toast";

vi.mock("../../../lib/api/judge", async () => {
  const actual =
    await vi.importActual<typeof import("../../../lib/api/judge")>(
      "../../../lib/api/judge",
    );
  return {
    ...actual,
    fetchMetrics: vi.fn(),
  };
});

import { fetchMetrics } from "../../../lib/api/judge";

const mockedFetch = fetchMetrics as unknown as ReturnType<typeof vi.fn>;

function makeMetrics(overrides: Partial<MetricsResponse> = {}): MetricsResponse {
  return {
    total: 0,
    pass_rate: null,
    total_7d: 0,
    pass_rate_7d: null,
    rating_counts: {},
    trend_7d: [],
    most_corrected_prompts: [],
    log_path: "/tmp/x.jsonl",
    ...overrides,
  };
}

beforeEach(() => {
  mockedFetch.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("MetricsDashboard", () => {
  it("skeleton during initial fetch", async () => {
    let resolve: (r: MetricsResponse) => void = () => {};
    mockedFetch.mockReturnValue(
      new Promise<MetricsResponse>((res) => {
        resolve = res;
      }),
    );
    render(
      <ToastProvider>
        <MetricsDashboard />
      </ToastProvider>,
    );
    await waitFor(() => {
      expect(screen.getAllByTestId("skeleton-row").length).toBeGreaterThan(0);
    });
    resolve(makeMetrics({ total: 0 }));
    await waitFor(() => {
      expect(screen.queryByTestId("skeleton-row")).toBeNull();
    });
  });

  it("empty-state when total equals zero", async () => {
    mockedFetch.mockResolvedValue(makeMetrics({ total: 0 }));
    render(
      <ToastProvider>
        <MetricsDashboard />
      </ToastProvider>,
    );
    await waitFor(() => {
      expect(screen.getByText(/no verdicts yet/i)).toBeInTheDocument();
    });
  });
});
