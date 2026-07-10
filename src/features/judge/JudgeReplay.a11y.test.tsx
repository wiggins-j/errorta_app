// A11Y-JUDGE — keyboard + aria assertions for the Judge feature pane.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReplayResult } from "../../lib/api/judge";
import JudgeReplay from "./JudgeReplay";
import JudgeFeature from "./index";

vi.mock("../../lib/api/judge", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api/judge")>(
      "../../lib/api/judge",
    );
  return {
    ...actual,
    replayCorpusStream: vi.fn(),
    fetchModel: vi.fn(),
    fetchPreflight: vi.fn(),
    setModel: vi.fn(),
    fetchMetrics: vi.fn(),
  };
});

vi.mock("../../lib/api/onboarding", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api/onboarding")>(
    "../../lib/api/onboarding",
  );
  return {
    ...actual,
    listCorpora: vi.fn(),
  };
});

import {
  fetchMetrics,
  fetchModel,
  fetchPreflight,
  replayCorpusStream,
} from "../../lib/api/judge";
import { listCorpora } from "../../lib/api/onboarding";

const mockedReplay = replayCorpusStream as unknown as ReturnType<typeof vi.fn>;
const mockedListCorpora = listCorpora as unknown as ReturnType<typeof vi.fn>;
const mockedFetchModel = fetchModel as unknown as ReturnType<typeof vi.fn>;
const mockedFetchPreflight = fetchPreflight as unknown as ReturnType<typeof vi.fn>;
const mockedFetchMetrics = fetchMetrics as unknown as ReturnType<typeof vi.fn>;

function makeResult(overrides: Partial<ReplayResult> = {}): ReplayResult {
  return {
    prompt: "what is up",
    original_answer: "unknown",
    original_verdict: {
      rating: "fail",
      reason: "no idea",
      failure_tags: [],
      confidence: 0.2,
    },
    original_grounding_match: null,
    replay_answer: "the sky",
    replay_verdict: {
      rating: "pass",
      reason: "ok",
      failure_tags: [],
      confidence: 0.9,
    },
    replay_grounding_match: { kind: "exact" },
    score_delta: 0.5,
    grounding_change: "added",
    occurred_at: "2026-06-08T00:00:00+00:00",
    ...overrides,
  };
}

beforeEach(() => {
  mockedReplay.mockReset();
  mockedListCorpora.mockReset();
  mockedListCorpora.mockResolvedValue({
    corpora: [{ name: "kitchen", file_count: 1, ready_count: 1 }],
  });
  mockedFetchModel.mockResolvedValue({ judge_model: "llama3.1", source: "default" });
  mockedFetchPreflight.mockResolvedValue({
    aiar_available: true,
    ollama_reachable: true,
    judge_model: "llama3.1",
    model_available: true,
  });
  mockedFetchMetrics.mockResolvedValue({
    total: 0,
    total_7d: 0,
    pass_rate: null,
    pass_rate_7d: null,
    trend_7d: [],
    most_corrected_prompts: [],
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("JudgeReplay a11y", () => {
  it("sort header is focusable and Enter toggles sort direction", async () => {
    mockedReplay.mockImplementation(
      async (_corpus: string, onResult: (r: ReplayResult) => void) => {
        onResult(makeResult({ prompt: "low", score_delta: 0.1 }));
        onResult(makeResult({ prompt: "high", score_delta: 0.9 }));
      },
    );
    render(<JudgeReplay corpus="kitchen" />);
    await userEvent.click(screen.getByTestId("replay-button"));
    const sortHeader = await screen.findByTestId("sort-improvement");
    expect(sortHeader.getAttribute("tabIndex") ?? sortHeader.getAttribute("tabindex")).toBe(
      "0",
    );
    expect(sortHeader.getAttribute("role")).toBe("button");

    // Default sort desc -> high first.
    let rows = screen.getAllByTestId(/^replay-row-/);
    expect(rows[0]).toHaveTextContent(/high/);

    sortHeader.focus();
    fireEvent.keyDown(sortHeader, { key: "Enter" });
    await waitFor(() => {
      const rows2 = screen.getAllByTestId(/^replay-row-/);
      expect(rows2[0]).toHaveTextContent(/low/);
    });

    // Toggle back to desc with Enter.
    fireEvent.keyDown(sortHeader, { key: "Enter" });
    await waitFor(() => {
      rows = screen.getAllByTestId(/^replay-row-/);
      expect(rows[0]).toHaveTextContent(/high/);
    });
  });

  it("Space on sort header toggles sort and preventDefault is called", async () => {
    mockedReplay.mockImplementation(
      async (_corpus: string, onResult: (r: ReplayResult) => void) => {
        onResult(makeResult({ prompt: "a", score_delta: 0.1 }));
        onResult(makeResult({ prompt: "b", score_delta: 0.9 }));
      },
    );
    render(<JudgeReplay corpus="kitchen" />);
    await userEvent.click(screen.getByTestId("replay-button"));
    const sortHeader = await screen.findByTestId("sort-improvement");

    // Default desc: b (0.9) first.
    expect(screen.getAllByTestId(/^replay-row-/)[0]).toHaveTextContent(/b/);

    // Issue a Space keydown and capture defaultPrevented.
    const evt = new KeyboardEvent("keydown", {
      key: " ",
      bubbles: true,
      cancelable: true,
    });
    sortHeader.dispatchEvent(evt);
    expect(evt.defaultPrevented).toBe(true);

    await waitFor(() => {
      expect(screen.getAllByTestId(/^replay-row-/)[0]).toHaveTextContent(/a/);
    });
  });

  it("expand button has aria-expanded that flips on click", async () => {
    mockedReplay.mockImplementation(
      async (_corpus: string, onResult: (r: ReplayResult) => void) => {
        onResult(makeResult({ prompt: "row 0", score_delta: 0.3 }));
      },
    );
    render(<JudgeReplay corpus="kitchen" />);
    await userEvent.click(screen.getByTestId("replay-button"));
    const btn = await screen.findByTestId("expand-button-0");
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    expect(btn.getAttribute("aria-controls")).toBe("replay-diff-row-0");

    await userEvent.click(btn);
    expect(btn.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("replay-diff-0")).toBeInTheDocument();

    await userEvent.click(btn);
    expect(btn.getAttribute("aria-expanded")).toBe("false");
  });

  it("expand header has descriptive aria-label (not 'expand')", async () => {
    mockedReplay.mockImplementation(
      async (_corpus: string, onResult: (r: ReplayResult) => void) => {
        onResult(makeResult({ prompt: "p", score_delta: 0.1 }));
      },
    );
    render(<JudgeReplay corpus="kitchen" />);
    await userEvent.click(screen.getByTestId("replay-button"));
    await screen.findByTestId("replay-table");
    // The header cell is the first th; assert via aria-label lookup.
    expect(
      screen.getByLabelText("Row expansion toggle"),
    ).toBeInTheDocument();
  });
});

describe("JudgeFeature tablist a11y", () => {
  it("tablist has aria-label and active tab has tabIndex=0", async () => {
    render(<JudgeFeature />);
    const tablist = await screen.findByRole("tablist");
    expect(tablist.getAttribute("aria-label")).toBe("Judge section tabs");

    const metricsTab = screen.getByTestId("judge-tab-metrics");
    const replayTab = screen.getByTestId("judge-tab-replay");
    expect(metricsTab.getAttribute("tabIndex") ?? metricsTab.getAttribute("tabindex")).toBe(
      "0",
    );
    expect(replayTab.getAttribute("tabIndex") ?? replayTab.getAttribute("tabindex")).toBe(
      "-1",
    );
  });

  it("ArrowRight on tablist moves active tab to next", async () => {
    render(<JudgeFeature />);
    const tablist = await screen.findByRole("tablist");
    fireEvent.keyDown(tablist, { key: "ArrowRight" });
    await waitFor(() => {
      const replayTab = screen.getByTestId("judge-tab-replay");
      expect(replayTab.getAttribute("aria-selected")).toBe("true");
    });
  });

  it("End on tablist jumps to last tab, Home to first", async () => {
    render(<JudgeFeature />);
    const tablist = await screen.findByRole("tablist");
    fireEvent.keyDown(tablist, { key: "End" });
    await waitFor(() => {
      expect(
        screen.getByTestId("judge-tab-replay").getAttribute("aria-selected"),
      ).toBe("true");
    });
    fireEvent.keyDown(tablist, { key: "Home" });
    await waitFor(() => {
      expect(
        screen.getByTestId("judge-tab-metrics").getAttribute("aria-selected"),
      ).toBe("true");
    });
  });
});
