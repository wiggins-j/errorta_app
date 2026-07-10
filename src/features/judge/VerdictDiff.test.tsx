import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { PriorVerdictPayload, Verdict } from "../../lib/api/judge";
import VerdictDiff from "./VerdictDiff";

const baseCurrent: Verdict = {
  rating: "pass",
  reason: "current reason",
  failure_tags: ["accuracy"],
  confidence: 0.9,
};

function makePrior(overrides: Partial<Verdict> = {}): PriorVerdictPayload {
  return {
    verdict: {
      rating: "fail",
      reason: "old reason",
      failure_tags: ["staleness"],
      confidence: 0.5,
      ...overrides,
    },
    judge_model: "llama3.1:8b",
    created_at: "2026-06-01T12:00:00+00:00",
  };
}

describe("VerdictDiff", () => {
  it("renders Compared to your last run header", () => {
    render(<VerdictDiff current={baseCurrent} priors={[makePrior()]} />);
    expect(
      screen.getByRole("heading", { name: /compared to your last run/i }),
    ).toBeInTheDocument();
  });

  it("renders prior-picker when priors length >= 2", async () => {
    const priors: PriorVerdictPayload[] = [
      makePrior({ rating: "fail" }),
      {
        verdict: {
          rating: "partial",
          reason: "older reason",
          failure_tags: [],
          confidence: 0.4,
        },
        judge_model: "qwen2:7b",
        created_at: "2026-05-20T10:00:00+00:00",
      },
    ];
    const onSelect = vi.fn();
    render(
      <VerdictDiff
        current={baseCurrent}
        priors={priors}
        selectedIndex={0}
        onSelectPrior={onSelect}
      />,
    );
    const picker = screen.getByRole("combobox", { name: /pick a prior verdict/i });
    expect(picker).toBeInTheDocument();
    // Two options exposed.
    expect(screen.getAllByRole("option")).toHaveLength(2);
    await userEvent.selectOptions(picker, "1");
    expect(onSelect).toHaveBeenCalledWith(1);
  });

  it("renders empty-state copy when no prior exists", () => {
    render(<VerdictDiff current={baseCurrent} priors={[]} />);
    expect(
      screen.getByText(/re-run this prompt to see how the verdict changes/i),
    ).toBeInTheDocument();
    // The picker must not render when there are no priors.
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("renders rating arrow signed delta and tag chips", () => {
    render(<VerdictDiff current={baseCurrent} priors={[makePrior()]} />);
    // Rating arrow shows prior → current.
    expect(screen.getByText(/fail\s*→\s*pass/i)).toBeInTheDocument();
    // Confidence delta: 0.9 - 0.5 = 0.4 -> +40pp (positive class).
    const delta = screen.getByText(/\+40/);
    expect(delta).toBeInTheDocument();
    expect(delta.className).toMatch(/delta-pos/);
    // Tag chips: 'accuracy' is added (in current, not in prior); 'staleness' is removed.
    expect(screen.getByText(/\+\s*accuracy/i)).toBeInTheDocument();
    expect(screen.getByText(/−\s*staleness/i)).toBeInTheDocument();
  });

  it("does NOT make any network calls (presentational only)", () => {
    const fetchMock = vi.fn();
    const originalFetch = globalThis.fetch;
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    try {
      render(<VerdictDiff current={baseCurrent} priors={[makePrior()]} />);
      expect(fetchMock).not.toHaveBeenCalled();
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});
