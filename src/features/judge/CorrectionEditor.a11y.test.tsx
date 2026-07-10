// A11Y-EXTEND — accessibility assertions for CorrectionEditor, VerdictPanel,
// and MetricsDashboard. Co-located here because the rules of the slice cap
// new a11y test files at three.
import { act, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import CorrectionEditor from "./CorrectionEditor";
import VerdictPanel from "./VerdictPanel";
import MetricsDashboard from "./MetricsDashboard";
import type { Verdict } from "../../lib/api/judge";

vi.mock("../../lib/api/judge", () => ({
  draftCorrection: vi.fn(),
  acceptVerdict: vi.fn(),
  fetchMetrics: vi.fn(),
}));

import {
  acceptVerdict,
  draftCorrection,
  fetchMetrics,
} from "../../lib/api/judge";

const fetchMetricsMock = vi.mocked(fetchMetrics);

const draftMock = vi.mocked(draftCorrection);
const acceptMock = vi.mocked(acceptVerdict);

const verdict: Verdict = {
  rating: "fail",
  reason: "wrong",
  failure_tags: [],
  confidence: 0.3,
};

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  draftMock.mockReset();
  acceptMock.mockReset();
  draftMock.mockResolvedValue({ draft: "corrected answer text" });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CorrectionEditor a11y", () => {
  it("renders an aria-live=polite status region", async () => {
    render(
      <CorrectionEditor
        verdictId="v1"
        answer="original"
        verdict={verdict}
      />,
    );
    await flush();
    const region = screen.getByTestId("correction-status");
    expect(region.getAttribute("aria-live")).toBe("polite");
    expect(region.getAttribute("role")).toBe("status");
  });

  it("announces 'Saving…' inside the live region while submission is in flight", async () => {
    let resolveAccept: (v: import("../../lib/api/judge").AcceptResponse) => void = () => {};
    acceptMock.mockReturnValue(
      new Promise((res) => {
        resolveAccept = res;
      }),
    );
    render(
      <CorrectionEditor
        verdictId="v1"
        answer="original"
        verdict={verdict}
      />,
    );
    await flush();
    const acceptBtn = screen.getByRole("button", { name: /accept correction/i });
    act(() => {
      acceptBtn.click();
    });
    const region = screen.getByTestId("correction-status");
    expect(within(region).getByText(/saving/i)).toBeInTheDocument();
    await act(async () => {
      resolveAccept({
        id: "v1",
        prompt: "p",
        answer: "a",
        grounding_recorded: true,
        created_at: "2026-06-08T00:00:00Z",
      });
      await Promise.resolve();
    });
  });

  it("announces 'Saved' message inside the live region after accept resolves", async () => {
    acceptMock.mockResolvedValue({
      id: "v1",
      prompt: "p",
      answer: "a",
      grounding_recorded: true,
      created_at: "2026-06-08T00:00:00Z",
    });
    render(
      <CorrectionEditor
        verdictId="v1"
        answer="original"
        verdict={verdict}
      />,
    );
    await flush();
    const acceptBtn = screen.getByRole("button", { name: /accept correction/i });
    await act(async () => {
      acceptBtn.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    const region = screen.getByTestId("correction-status");
    expect(within(region).getByText(/saved\. future runs/i)).toBeInTheDocument();
  });

  it("VerdictPanel exposes latency as a human-readable 'X.X seconds' aria-label and wraps in role=region", () => {
    const v: Verdict = {
      rating: "pass",
      reason: "ok",
      failure_tags: [],
      confidence: 0.74,
      latency_ms: 1234,
    };
    render(<VerdictPanel verdict={v} />);
    const region = screen.getByRole("region", { name: /verdict/i });
    expect(region).toBeInTheDocument();
    // latency_ms=1234 -> "1.2 seconds"
    expect(
      screen.getByLabelText(/judge took 1\.2 seconds/i),
    ).toBeInTheDocument();
    // confidence aria-label is expanded too
    expect(
      screen.getByLabelText(/judge confidence: 74 percent/i),
    ).toBeInTheDocument();
  });

  it("MetricsDashboard stat values expose numeric aria-labels (pass rate all-time and total)", async () => {
    fetchMetricsMock.mockResolvedValue({
      total: 50,
      total_7d: 12,
      pass_rate: 0.74,
      pass_rate_7d: 0.66,
      rating_counts: {},
      trend_7d: [],
      most_corrected_prompts: [],
      log_path: "/tmp/log",
    });
    render(<MetricsDashboard />);
    await flush();
    expect(
      screen.getByLabelText(/74% pass rate all-time/i),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/50 verdicts total/i)).toBeInTheDocument();
  });

  it("textarea exposes aria-label and aria-disabled mirroring the disabled state", async () => {
    // Drafting is in flight (never resolves) so textarea stays disabled.
    draftMock.mockReturnValue(new Promise(() => {}));
    render(
      <CorrectionEditor
        verdictId="v1"
        answer="original"
        verdict={verdict}
      />,
    );
    const textarea = screen.getByLabelText(
      "Proposed correction text",
    ) as HTMLTextAreaElement;
    expect(textarea.getAttribute("aria-disabled")).toBe("true");
    expect(textarea).toBeDisabled();
  });
});
