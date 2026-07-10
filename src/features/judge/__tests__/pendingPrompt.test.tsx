import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  consumePendingPrompt,
  setPendingPrompt,
} from "../pendingPrompt";

// Stub the api module so JudgeFeature can mount without crossing the fetch
// boundary (PromptRunner imports it transitively).
vi.mock("../../../lib/api/judge", async () => {
  const actual =
    await vi.importActual<typeof import("../../../lib/api/judge")>(
      "../../../lib/api/judge",
    );
  return {
    ...actual,
    runVerdict: vi.fn(),
    fetchPriorVerdicts: vi.fn().mockResolvedValue({ signature: "", priors: [] }),
    fetchModel: vi.fn().mockResolvedValue({ model: null }),
    fetchPreflight: vi.fn().mockResolvedValue({ ok: true }),
  };
});

import JudgeFeature from "../index";

afterEach(() => {
  // Drain any leftover pending value so tests don't bleed into each other.
  consumePendingPrompt();
  vi.restoreAllMocks();
});

describe("pendingPrompt — one-shot store", () => {
  it("read-and-clears: a second consume returns null", () => {
    setPendingPrompt("hello");
    expect(consumePendingPrompt()).toBe("hello");
    expect(consumePendingPrompt()).toBeNull();
  });

  it("returns null when nothing is pending", () => {
    expect(consumePendingPrompt()).toBeNull();
  });
});

describe("JudgeFeature — F109 one-shot consumption on mount", () => {
  it("seeds the runner from a pending prompt and clears it", () => {
    setPendingPrompt("Pending welcome prompt");
    const { unmount } = render(<JudgeFeature />);
    const textarea = screen.getByLabelText(/prompt/i) as HTMLTextAreaElement;
    expect(textarea.value).toBe("Pending welcome prompt");
    unmount();

    // Second mount with no pending value starts empty (one-shot consumed).
    render(<JudgeFeature />);
    const textarea2 = screen.getByLabelText(/prompt/i) as HTMLTextAreaElement;
    expect(textarea2.value).toBe("");
  });
});
