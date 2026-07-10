// F143 / F143-01 — TokenUsagePanel renders the GENUINE (measured+estimated) total,
// tags estimated values distinctly, shows a coverage split, keeps cache out of the
// headline, and renders by_role. Honest about legacy/zero-token buckets.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/coding", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/coding")>();
  return { ...actual, getProjectUsageSummary: vi.fn() };
});

import { getProjectUsageSummary } from "../../lib/api/coding";
import TokenUsagePanel from "./TokenUsagePanel";

const mockGet = getProjectUsageSummary as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

interface BucketOver {
  input?: number;
  output?: number;
  measuredInput?: number;
  measuredOutput?: number;
  estimatedInput?: number;
  estimatedOutput?: number;
  cacheRead?: number;
  cacheWrite?: number;
  turns?: number;
  measuredTurns?: number;
  partialTurns?: number;
  estimatedTurns?: number;
  unreportedTurns?: number;
  coverage?: { measuredPct: number; estimatedPct: number };
}

function bucket(over: BucketOver = {}) {
  const { coverage, ...rest } = over;
  return {
    input: 0,
    output: 0,
    measuredInput: 0,
    measuredOutput: 0,
    estimatedInput: 0,
    estimatedOutput: 0,
    cacheRead: 0,
    cacheWrite: 0,
    turns: 0,
    measuredTurns: 0,
    partialTurns: 0,
    estimatedTurns: 0,
    unreportedTurns: 0,
    coverage: coverage ?? { measuredPct: 0, estimatedPct: 0 },
    ...rest,
  };
}

describe("TokenUsagePanel", () => {
  it("shows the genuine (measured+estimated) grand total and per-route breakdown", async () => {
    mockGet.mockResolvedValue({
      // measured 900/350 + estimated 100/50 = effective 1000/400 = 1,400 headline
      total: bucket({
        input: 1000,
        output: 400,
        measuredInput: 900,
        measuredOutput: 350,
        estimatedInput: 100,
        estimatedOutput: 50,
        turns: 3,
        measuredTurns: 2,
        estimatedTurns: 1,
        coverage: { measuredPct: 89, estimatedPct: 11 },
      }),
      byMember: {
        "m-dev-1": bucket({ input: 900, output: 350, turns: 2, measuredTurns: 2, coverage: { measuredPct: 100, estimatedPct: 0 } }),
      },
      byRoute: {
        "claude_cli.sonnet": bucket({ input: 900, output: 350, turns: 2, measuredTurns: 2, coverage: { measuredPct: 100, estimatedPct: 0 } }),
        "local.ollama": bucket({ input: 100, output: 50, turns: 1, measuredTurns: 1, coverage: { measuredPct: 100, estimatedPct: 0 } }),
      },
      byRole: {
        DEV: bucket({ input: 1000, output: 400, turns: 3, measuredTurns: 2, estimatedTurns: 1, coverage: { measuredPct: 89, estimatedPct: 11 } }),
      },
    });
    render(<TokenUsagePanel projectId="p1" />);
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("p1"));
    // headline effective total (1000 + 400 = 1,400) — the genuine total, not measured-only (1,250).
    await waitFor(() => expect(screen.getAllByText("1,400").length).toBeGreaterThanOrEqual(1));
    expect(screen.getByText(/tokens total/)).toBeInTheDocument();
    expect(screen.getByText("By member")).toBeInTheDocument();
    expect(screen.getByText("By model / route")).toBeInTheDocument();
    expect(screen.getByText("claude_cli.sonnet")).toBeInTheDocument();
    expect(screen.getByText("local.ollama")).toBeInTheDocument();
  });

  it("renders a coverage split beneath the headline", async () => {
    mockGet.mockResolvedValue({
      total: bucket({
        input: 780,
        output: 220,
        measuredInput: 620,
        measuredOutput: 160,
        estimatedInput: 160,
        estimatedOutput: 60,
        turns: 4,
        measuredTurns: 2,
        estimatedTurns: 2,
        coverage: { measuredPct: 78, estimatedPct: 22 },
      }),
      byMember: {},
      byRoute: {},
      byRole: {},
    });
    render(<TokenUsagePanel projectId="p1" />);
    await waitFor(() =>
      expect(screen.getByText(/78% measured/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/22% estimated/)).toBeInTheDocument();
  });

  it("marks an estimated value with ~ and the muted tooltip, distinct from measured", async () => {
    mockGet.mockResolvedValue({
      // dark DEV turn: wholly estimated, dominates the total.
      total: bucket({
        input: 5000,
        output: 1200,
        estimatedInput: 5000,
        estimatedOutput: 1200,
        turns: 1,
        estimatedTurns: 1,
        coverage: { measuredPct: 0, estimatedPct: 100 },
      }),
      byMember: {},
      byRoute: {
        "cursor_cli.default": bucket({
          input: 5000,
          output: 1200,
          estimatedInput: 5000,
          estimatedOutput: 1200,
          turns: 1,
          estimatedTurns: 1,
          coverage: { measuredPct: 0, estimatedPct: 100 },
        }),
      },
      byRole: {},
    });
    render(<TokenUsagePanel projectId="p1" />);
    // The estimated in-value renders as ~5,000 with the provenance tooltip.
    const est = await screen.findByText("~5,000");
    expect(est).toBeInTheDocument();
    expect(est).toHaveAttribute("title", "locally tokenized — not provider-reported");
  });

  it("does NOT tag a partly-measured bucket as estimated — only a wholly-estimated one", async () => {
    mockGet.mockResolvedValue({
      total: bucket({ input: 6600, output: 900, measuredInput: 3900, measuredOutput: 500, estimatedInput: 1800, estimatedOutput: 400, turns: 4, measuredTurns: 3, estimatedTurns: 1, coverage: { measuredPct: 70, estimatedPct: 30 } }),
      byMember: {},
      byRoute: {
        // 70% measured / 30% estimated — the number is mostly provider-reported, so
        // it must render PLAIN (tagging it "~ not provider-reported" would lie).
        "claude_cli.sonnet": bucket({
          input: 5600, output: 700, measuredInput: 3900, measuredOutput: 500,
          estimatedInput: 1700, estimatedOutput: 200, turns: 3, measuredTurns: 3,
          estimatedTurns: 1, coverage: { measuredPct: 70, estimatedPct: 30 },
        }),
        // 100% estimated — the whole number is a guess, so it IS tagged.
        "cursor_cli.default": bucket({
          input: 800, output: 200, estimatedInput: 800, estimatedOutput: 200,
          turns: 1, estimatedTurns: 1, coverage: { measuredPct: 0, estimatedPct: 100 },
        }),
      },
      byRole: {},
    });
    render(<TokenUsagePanel projectId="p1" />);
    // Partial bucket: totals render plain, never `~`-tagged.
    expect(await screen.findByText("5,600")).toBeInTheDocument();
    expect(screen.queryByText("~5,600")).toBeNull();
    expect(screen.queryByText("~4,900")).toBeNull();
    // ...but its coverage line still discloses the estimated share.
    expect(screen.getAllByText(/70% measured/).length).toBeGreaterThanOrEqual(1);
    // Wholly-estimated bucket: tagged with `~` + the provenance tooltip.
    const est = screen.getByText("~1,000");
    expect(est).toHaveAttribute("title", "locally tokenized — not provider-reported");
  });

  it("keeps cache out of the headline but surfaces it as detail", async () => {
    mockGet.mockResolvedValue({
      total: bucket({
        input: 300,
        output: 100,
        measuredInput: 300,
        measuredOutput: 100,
        cacheRead: 9000,
        cacheWrite: 1500,
        turns: 1,
        measuredTurns: 1,
        coverage: { measuredPct: 100, estimatedPct: 0 },
      }),
      byMember: {},
      byRoute: {},
      byRole: {},
    });
    render(<TokenUsagePanel projectId="p1" />);
    // Headline total is 300 + 100 = 400, NOT 400 + 10,500 cache.
    await waitFor(() => expect(screen.getAllByText("400").length).toBeGreaterThanOrEqual(1));
    // Cache appears only as a detail line.
    expect(screen.getByText(/9,000 cache read/)).toBeInTheDocument();
    expect(screen.getByText(/1,500 cache write/)).toBeInTheDocument();
    // And is never merged into the total token count.
    expect(screen.queryByText("10,900")).not.toBeInTheDocument();
  });

  it("renders the by_role subtotal table", async () => {
    mockGet.mockResolvedValue({
      total: bucket({ input: 600, output: 200, turns: 3, measuredTurns: 3, coverage: { measuredPct: 100, estimatedPct: 0 } }),
      byMember: {},
      byRoute: {},
      byRole: {
        PM: bucket({ input: 100, output: 40, turns: 1, measuredTurns: 1, coverage: { measuredPct: 100, estimatedPct: 0 } }),
        DEV: bucket({ input: 500, output: 160, turns: 2, measuredTurns: 2, coverage: { measuredPct: 100, estimatedPct: 0 } }),
      },
    });
    render(<TokenUsagePanel projectId="p1" />);
    await waitFor(() => expect(screen.getByText("By role")).toBeInTheDocument());
    expect(screen.getByText("PM")).toBeInTheDocument();
    expect(screen.getByText("DEV")).toBeInTheDocument();
  });

  it("annotates the legacy 'measured turn but 0% coverage' edge", async () => {
    mockGet.mockResolvedValue({
      total: bucket({
        input: 120,
        output: 30,
        // A pre-accounting turn was counted as measured but never token-attributed,
        // so the estimated portion fills the headline and coverage reads 0% measured.
        estimatedInput: 120,
        estimatedOutput: 30,
        turns: 2,
        measuredTurns: 1,
        estimatedTurns: 1,
        coverage: { measuredPct: 0, estimatedPct: 100 },
      }),
      byMember: {
        "m-old": bucket({
          input: 120,
          output: 30,
          estimatedInput: 120,
          estimatedOutput: 30,
          turns: 2,
          measuredTurns: 1,
          estimatedTurns: 1,
          coverage: { measuredPct: 0, estimatedPct: 100 },
        }),
      },
      byRoute: {},
      byRole: {},
    });
    render(<TokenUsagePanel projectId="p1" />);
    await waitFor(() => expect(screen.getByText("m-old")).toBeInTheDocument());
    expect(screen.getByText(/pre-accounting turns/)).toBeInTheDocument();
  });

  it("shows an em-dash (not 0%) for a zero-token bucket", async () => {
    mockGet.mockResolvedValue({
      total: bucket({ turns: 3, measuredTurns: 1, unreportedTurns: 2 }),
      byMember: {},
      byRoute: {
        // A route with turns but no tokens at all — em-dash + no misleading 0%.
        "cursor_cli.default": bucket({ turns: 2, unreportedTurns: 2 }),
      },
      byRole: {},
    });
    render(<TokenUsagePanel projectId="p1" />);
    await waitFor(() => expect(screen.getByText("cursor_cli.default")).toBeInTheDocument());
    // The zero-token route row shows "no tokens yet", never "0% measured".
    expect(screen.getByText("no tokens yet")).toBeInTheDocument();
    expect(screen.queryByText(/0% measured/)).not.toBeInTheDocument();
    // And the em-dash renders for the empty numeric cells.
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(1);
  });

  it("shows an empty state when no turns have been recorded", async () => {
    mockGet.mockResolvedValue({ total: bucket(), byMember: {}, byRoute: {}, byRole: {} });
    render(<TokenUsagePanel projectId="p1" />);
    await waitFor(() =>
      expect(screen.getByText("No token usage recorded yet.")).toBeInTheDocument(),
    );
  });
});
