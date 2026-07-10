import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, fireEvent } from "@testing-library/react";
import BriefEditor from "./BriefEditor";

vi.mock("../../lib/api/briefs", () => ({
  updateBrief: vi.fn(),
  validateBrief: vi.fn(),
  listBriefHistory: vi.fn().mockResolvedValue([]),
  getBriefHistorySnapshot: vi.fn(),
  restoreBriefSnapshot: vi.fn(),
}));

import {
  updateBrief,
  validateBrief,
  listBriefHistory,
  getBriefHistorySnapshot,
  restoreBriefSnapshot,
} from "../../lib/api/briefs";

const updateBriefMock = vi.mocked(updateBrief);
const validateBriefMock = vi.mocked(validateBrief);

beforeEach(() => {
  updateBriefMock.mockReset();
  validateBriefMock.mockReset();
  updateBriefMock.mockResolvedValue({
    brief_id: "b1",
    corpus_name: "demo",
    state: "DRAFT",
    created_at: "2026-06-01T00:00:00Z",
    last_run_at: null,
  });
  validateBriefMock.mockResolvedValue({ ok: true, errors: [], connectors: {} });
});

afterEach(() => {
  vi.useRealTimers();
});

async function flushPromises() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("BriefEditor", () => {
  it("renders the initial markdown as a controlled textarea value", () => {
    render(
      <BriefEditor briefId="b1" initialMarkdown="hello world" initialParseErrors={[]} />,
    );
    const textarea = screen.getByLabelText(/brief markdown/i) as HTMLTextAreaElement;
    expect(textarea.value).toBe("hello world");
  });

  it("updates textarea value on change (controlled input)", () => {
    render(<BriefEditor briefId="b1" initialMarkdown="" initialParseErrors={[]} />);
    const textarea = screen.getByLabelText(/brief markdown/i) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "new content" } });
    expect(textarea.value).toBe("new content");
  });

  it("debounces validateBrief by 500ms after a change", async () => {
    vi.useFakeTimers();
    render(<BriefEditor briefId="b1" initialMarkdown="" initialParseErrors={[]} />);
    const textarea = screen.getByLabelText(/brief markdown/i) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "abc" } });

    // Before 500ms, neither call has fired.
    expect(updateBriefMock).not.toHaveBeenCalled();
    expect(validateBriefMock).not.toHaveBeenCalled();

    await act(async () => {
      vi.advanceTimersByTime(499);
    });
    expect(updateBriefMock).not.toHaveBeenCalled();

    await act(async () => {
      vi.advanceTimersByTime(1);
    });
    // updateBrief runs first (synchronously kicked off).
    expect(updateBriefMock).toHaveBeenCalledWith("b1", "abc");

    // Flush the pending awaits so validateBrief is invoked.
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(validateBriefMock).toHaveBeenCalledWith("b1");
  });

  it("displays parse errors from the initial prop", () => {
    render(
      <BriefEditor
        briefId="b1"
        initialMarkdown=""
        initialParseErrors={[{ message: "missing required field" }]}
      />,
    );
    expect(screen.getByText(/parse errors/i)).toBeInTheDocument();
    expect(screen.getByText(/missing required field/)).toBeInTheDocument();
  });

  it("renders connector OK and ERROR pills after validation completes", async () => {
    validateBriefMock.mockResolvedValue({
      ok: false,
      errors: [],
      connectors: {
        arxiv: { ok: true },
        ntrs: { ok: false, reason: "auth failed" },
      },
    });
    render(<BriefEditor briefId="b1" initialMarkdown="" initialParseErrors={[]} />);
    const textarea = screen.getByLabelText(/brief markdown/i);
    fireEvent.change(textarea, { target: { value: "foo" } });

    // Wait for the real debounce + promise chain.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 600));
    });
    await flushPromises();

    expect(screen.getByText("OK")).toBeInTheDocument();
    expect(screen.getByText("ERROR")).toBeInTheDocument();
    expect(screen.getByText("arxiv")).toBeInTheDocument();
    expect(screen.getByText("ntrs")).toBeInTheDocument();
    expect(screen.getByText(/auth failed/)).toBeInTheDocument();
  });

  it("displays saveError when updateBrief rejects", async () => {
    updateBriefMock.mockRejectedValue(new Error("network down"));
    render(<BriefEditor briefId="b1" initialMarkdown="" initialParseErrors={[]} />);
    const textarea = screen.getByLabelText(/brief markdown/i);
    fireEvent.change(textarea, { target: { value: "foo" } });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 600));
    });
    await flushPromises();
    expect(screen.getByText(/network down/)).toBeInTheDocument();
  });

  it("onRestore handler updates textarea, clears parse errors, and cancels pending debounced validate", async () => {
    vi.useFakeTimers();
    const listMock = vi.mocked(listBriefHistory);
    const getMock = vi.mocked(getBriefHistorySnapshot);
    const restoreMock = vi.mocked(restoreBriefSnapshot);
    listMock.mockResolvedValue([
      { timestamp: "2026-06-01T120000.000000Z", byte_size: 10, sha256: "a".repeat(64) },
    ]);
    getMock.mockResolvedValue("restored body");
    restoreMock.mockResolvedValue({
      brief_id: "b1",
      corpus_name: "demo",
      state: "DRAFT",
      created_at: "2026-06-01T00:00:00Z",
      last_run_at: null,
    });
    const origConfirm = window.confirm;
    const confirmSpy = vi.fn().mockReturnValue(true);
    window.confirm = confirmSpy as unknown as typeof window.confirm;

    render(
      <BriefEditor
        briefId="b1"
        initialMarkdown="initial"
        initialParseErrors={[{ message: "boom" }]}
      />,
    );

    // Verify the initial parse error is visible.
    expect(screen.getByText(/boom/)).toBeInTheDocument();

    // Schedule a debounced validate that we expect to be cancelled.
    const textarea = screen.getByLabelText(/brief markdown/i) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "edited but stale" } });

    // Open dropdown -> snapshot -> restore.
    fireEvent.click(screen.getByRole("button", { name: /history/i }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    fireEvent.click(screen.getAllByRole("menuitem")[0]);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    fireEvent.click(screen.getByRole("button", { name: /restore snapshot/i }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    // Textarea reflects the restored body.
    expect((screen.getByLabelText(/brief markdown/i) as HTMLTextAreaElement).value).toBe(
      "restored body",
    );
    // Parse-error banner is gone.
    expect(screen.queryByText(/boom/)).not.toBeInTheDocument();

    // Reset mocks before advancing time so we can prove the pending debounced
    // validate did NOT fire (it should have been cancelled by onRestore).
    updateBriefMock.mockClear();
    validateBriefMock.mockClear();
    await act(async () => {
      vi.advanceTimersByTime(2000);
    });
    expect(updateBriefMock).not.toHaveBeenCalled();
    expect(validateBriefMock).not.toHaveBeenCalled();
    window.confirm = origConfirm;
    expect(confirmSpy).toHaveBeenCalled();
  });

  it("clears the debounce timer on unmount", async () => {
    vi.useFakeTimers();
    const clearSpy = vi.spyOn(globalThis, "clearTimeout");
    const { unmount } = render(
      <BriefEditor briefId="b1" initialMarkdown="" initialParseErrors={[]} />,
    );
    const textarea = screen.getByLabelText(/brief markdown/i);
    fireEvent.change(textarea, { target: { value: "abc" } });
    unmount();
    expect(clearSpy).toHaveBeenCalled();
    // And no calls fired post-unmount.
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    expect(updateBriefMock).not.toHaveBeenCalled();
    clearSpy.mockRestore();
  });
});
