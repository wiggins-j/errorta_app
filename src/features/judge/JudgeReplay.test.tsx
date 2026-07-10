import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReplayResult } from "../../lib/api/judge";
import JudgeReplay from "./JudgeReplay";

vi.mock("../../lib/api/judge", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api/judge")>(
      "../../lib/api/judge",
    );
  return {
    ...actual,
    replayCorpusStream: vi.fn(),
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

import { replayCorpusStream } from "../../lib/api/judge";
import { listCorpora } from "../../lib/api/onboarding";

const mockedReplay = replayCorpusStream as unknown as ReturnType<typeof vi.fn>;
const mockedListCorpora = listCorpora as unknown as ReturnType<typeof vi.fn>;

// JR-STREAM-TEST — controllable-stream helper extracted from the
// "renders rows incrementally" case (lines 140-182 below). The new
// monotonic-growth / mid-stream-error / cancel cases reuse this; the
// existing incremental-render case is intentionally left untouched
// (this file extends but does not duplicate that case).
interface ControllableStream {
  emit: (r: ReplayResult) => void;
  resolve: () => void;
  reject: (err: unknown) => void;
  mockImpl: (
    corpus: string,
    onResult: (r: ReplayResult) => void,
    opts?: { dryRun?: boolean; signal?: AbortSignal },
  ) => Promise<void>;
}

function makeControllableStream(): ControllableStream {
  let emitFn: (r: ReplayResult) => void = () => {
    throw new Error("emit called before stream started");
  };
  let resolveFn: () => void = () => {};
  let rejectFn: (err: unknown) => void = () => {};
  const done = new Promise<void>((res, rej) => {
    resolveFn = res;
    rejectFn = rej;
  });
  const mockImpl = async (
    _corpus: string,
    onResult: (r: ReplayResult) => void,
    _opts?: { dryRun?: boolean; signal?: AbortSignal },
  ) => {
    emitFn = onResult;
    await done;
  };
  return {
    emit: (r) => emitFn(r),
    resolve: () => resolveFn(),
    reject: (err) => rejectFn(err),
    mockImpl,
  };
}

function flushPromises(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function makeResult(overrides: Partial<ReplayResult> = {}): ReplayResult {
  return {
    prompt: "what is the airspeed of a swallow?",
    original_answer: "unknown",
    original_verdict: {
      rating: "fail",
      reason: "no idea",
      failure_tags: [],
      confidence: 0.2,
    },
    original_grounding_match: null,
    replay_answer: "African or European?",
    replay_verdict: {
      rating: "pass",
      reason: "answered correctly",
      failure_tags: [],
      confidence: 0.9,
    },
    replay_grounding_match: { kind: "exact" },
    score_delta: 0.94,
    grounding_change: "added",
    occurred_at: "2026-06-08T00:00:00+00:00",
    ...overrides,
  };
}

beforeEach(() => {
  mockedReplay.mockReset();
  mockedListCorpora.mockReset();
  mockedListCorpora.mockResolvedValue({
    corpora: [
      { name: "kitchen", file_count: 3, ready_count: 3 },
      { name: "bedroom", file_count: 2, ready_count: 2 },
    ],
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("JudgeReplay", () => {
  it("renders empty state copy after a run returns zero verdicts", async () => {
    mockedReplay.mockImplementation(async () => {
      /* no frames */
    });
    render(<JudgeReplay corpus="kitchen" />);
    await userEvent.click(screen.getByTestId("replay-button"));
    await waitFor(() => {
      expect(
        screen.getByText(/no verdicts in this corpus — run a few prompts first/i),
      ).toBeInTheDocument();
    });
  });

  it("renders sortable table when results are present", async () => {
    mockedReplay.mockImplementation(
      async (
        _corpus: string,
        onResult: (r: ReplayResult) => void,
      ) => {
        onResult(makeResult({ prompt: "low improvement", score_delta: 0.1 }));
        onResult(makeResult({ prompt: "big improvement", score_delta: 0.9 }));
      },
    );
    render(<JudgeReplay corpus="kitchen" />);
    await userEvent.click(screen.getByTestId("replay-button"));

    await waitFor(() => {
      expect(screen.getByTestId("replay-table")).toBeInTheDocument();
    });
    // Default sort desc -> big improvement first.
    const rows = screen.getAllByTestId(/^replay-row-/);
    expect(rows[0]).toHaveTextContent(/big improvement/);
    expect(rows[1]).toHaveTextContent(/low improvement/);

    // Toggle sort direction -> small first.
    await userEvent.click(screen.getByTestId("sort-improvement"));
    const rows2 = screen.getAllByTestId(/^replay-row-/);
    expect(rows2[0]).toHaveTextContent(/low improvement/);
  });

  it("dry-run checkbox toggles the API call shape", async () => {
    mockedReplay.mockImplementation(async () => {
      /* no frames */
    });
    render(<JudgeReplay corpus="kitchen" />);

    // Default is dry-run on.
    await userEvent.click(screen.getByTestId("replay-button"));
    expect(mockedReplay).toHaveBeenLastCalledWith(
      "kitchen",
      expect.any(Function),
      expect.objectContaining({ dryRun: true }),
    );

    // Toggle off, click again -> dry-run false.
    await userEvent.click(screen.getByTestId("dry-run-toggle"));
    await userEvent.click(screen.getByTestId("replay-button"));
    expect(mockedReplay).toHaveBeenLastCalledWith(
      "kitchen",
      expect.any(Function),
      expect.objectContaining({ dryRun: false }),
    );
  });

  it("renders rows incrementally as the stream yields frames", async () => {
    // Resolve the promise only after we let the test inspect the DOM between frames.
    let resolveStream: () => void = () => {};
    const streamDone = new Promise<void>((res) => {
      resolveStream = res;
    });
    let emit: ((r: ReplayResult) => void) | null = null;
    mockedReplay.mockImplementation(
      async (
        _corpus: string,
        onResult: (r: ReplayResult) => void,
      ) => {
        emit = onResult;
        await streamDone;
      },
    );

    render(<JudgeReplay corpus="kitchen" />);
    await userEvent.click(screen.getByTestId("replay-button"));

    // First frame arrives mid-stream — UI should render row 0 before the
    // outer promise settles.
    await waitFor(() => expect(emit).not.toBeNull());
    emit!(makeResult({ prompt: "first frame", score_delta: 0.2 }));
    await waitFor(() => {
      expect(screen.getByTestId("replay-row-0")).toHaveTextContent(
        /first frame/,
      );
    });

    // Second frame — also rendered while still streaming.
    emit!(makeResult({ prompt: "second frame", score_delta: 0.5 }));
    await waitFor(() => {
      expect(screen.getAllByTestId(/^replay-row-/)).toHaveLength(2);
    });

    resolveStream();
    await waitFor(() => {
      expect(screen.getByTestId("replay-button")).toHaveTextContent(
        /replay all/i,
      );
    });
  });

  it("fetches corpora on mount and populates the select", async () => {
    render(<JudgeReplay corpus={null} />);
    await waitFor(() => {
      expect(mockedListCorpora).toHaveBeenCalledTimes(1);
    });
    const select = (await screen.findByTestId(
      "corpus-select",
    )) as HTMLSelectElement;
    await waitFor(() => {
      const names = Array.from(select.options).map((o) => o.value);
      expect(names).toContain("kitchen");
      expect(names).toContain("bedroom");
    });
  });

  it("invokes onCorpusChange when the select changes", async () => {
    const onCorpusChange = vi.fn();
    render(<JudgeReplay corpus={null} onCorpusChange={onCorpusChange} />);
    const select = (await screen.findByTestId(
      "corpus-select",
    )) as HTMLSelectElement;
    await waitFor(() => {
      expect(
        Array.from(select.options).map((o) => o.value),
      ).toContain("kitchen");
    });
    await userEvent.selectOptions(select, "kitchen");
    expect(onCorpusChange).toHaveBeenCalledWith("kitchen");
  });

  it("toggles aria-expanded and reveals the diff row when a row is clicked", async () => {
    mockedReplay.mockImplementation(
      async (
        _corpus: string,
        onResult: (r: ReplayResult) => void,
      ) => {
        onResult(makeResult({ prompt: "clickable row", score_delta: 0.3 }));
      },
    );
    render(<JudgeReplay corpus="kitchen" />);
    await userEvent.click(screen.getByTestId("replay-button"));
    const row = await screen.findByTestId("replay-row-0");
    expect(row.getAttribute("aria-expanded")).toBe("false");
    await userEvent.click(row);
    expect(row.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("replay-diff-0")).toBeInTheDocument();
    await userEvent.click(row);
    expect(row.getAttribute("aria-expanded")).toBe("false");
  });

  // JR-STREAM-TEST Case A
  it("state.results length grows monotonically across frames", async () => {
    const stream = makeControllableStream();
    mockedReplay.mockImplementation(stream.mockImpl);

    render(<JudgeReplay corpus="kitchen" />);
    await userEvent.click(screen.getByTestId("replay-button"));

    stream.emit(makeResult({ prompt: "alpha frame", score_delta: 0.2 }));
    await waitFor(() => {
      expect(screen.getByTestId("replay-row-0")).toHaveTextContent(
        /alpha frame/,
      );
    });
    // Exactly one row, no row-1 yet.
    expect(screen.getAllByTestId(/^replay-row-/)).toHaveLength(1);
    expect(screen.queryByTestId("replay-row-1")).toBeNull();
    const row0TextBefore = screen.getByTestId("replay-row-0").textContent;

    stream.emit(makeResult({ prompt: "beta frame", score_delta: 0.1 }));
    await waitFor(() => {
      expect(screen.getAllByTestId(/^replay-row-/)).toHaveLength(2);
    });
    // Append-only invariant: row-0 (sorted desc by delta -> alpha first)
    // text content has not been mutated.
    expect(screen.getByTestId("replay-row-0").textContent).toBe(row0TextBefore);

    stream.resolve();
    await waitFor(() => {
      expect(screen.getByTestId("replay-button")).toHaveTextContent(
        /replay all/i,
      );
    });
  });

  // JR-STREAM-TEST Case B
  it("stream error mid-way preserves prior results and shows error UI", async () => {
    const stream = makeControllableStream();
    mockedReplay.mockImplementation(stream.mockImpl);

    render(<JudgeReplay corpus="kitchen" />);
    await userEvent.click(screen.getByTestId("replay-button"));

    stream.emit(makeResult({ prompt: "survivor frame", score_delta: 0.3 }));
    await waitFor(() => {
      expect(screen.getByTestId("replay-row-0")).toBeInTheDocument();
    });

    stream.reject(new Error("connection reset"));
    await flushPromises();

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/connection reset/);
    });
    // Prior rows survived.
    expect(screen.getByTestId("replay-row-0")).toBeInTheDocument();
    // Button label back to idle.
    expect(screen.getByTestId("replay-button")).toHaveTextContent(
      /replay all/i,
    );
  });

  // JR-STREAM-TEST Case C — AbortError branch is silent per
  // JudgeReplay.tsx:92-94: no error banner should appear.
  it("cancel mid-stream preserves prior rows and suppresses error banner", async () => {
    const stream = makeControllableStream();
    mockedReplay.mockImplementation(stream.mockImpl);

    render(<JudgeReplay corpus="kitchen" />);
    await userEvent.click(screen.getByTestId("replay-button"));

    stream.emit(makeResult({ prompt: "pre-cancel frame", score_delta: 0.4 }));
    await waitFor(() => {
      expect(screen.getByTestId("replay-row-0")).toBeInTheDocument();
    });

    await userEvent.click(screen.getByTestId("cancel-button"));
    stream.reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
    await flushPromises();

    await waitFor(() => {
      expect(screen.getByTestId("replay-button")).toHaveTextContent(
        /replay all/i,
      );
    });
    // Row survives and no alert is rendered.
    expect(screen.getByTestId("replay-row-0")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("assigns delta-pos / delta-neg / delta-zero classes by sign", async () => {
    mockedReplay.mockImplementation(
      async (
        _corpus: string,
        onResult: (r: ReplayResult) => void,
      ) => {
        onResult(makeResult({ prompt: "pos", score_delta: 0.5 }));
        onResult(makeResult({ prompt: "neg", score_delta: -0.4 }));
        onResult(makeResult({ prompt: "zero", score_delta: 0 }));
      },
    );
    render(<JudgeReplay corpus="kitchen" />);
    await userEvent.click(screen.getByTestId("replay-button"));
    await waitFor(() => {
      expect(screen.getAllByTestId(/^replay-row-/)).toHaveLength(3);
    });
    // Default sort desc: pos, zero, neg.
    const d0 = screen.getByTestId("delta-0");
    const d1 = screen.getByTestId("delta-1");
    const d2 = screen.getByTestId("delta-2");
    expect(d0.className).toMatch(/delta-pos/);
    expect(d1.className).toMatch(/delta-zero/);
    expect(d2.className).toMatch(/delta-neg/);
  });
});
