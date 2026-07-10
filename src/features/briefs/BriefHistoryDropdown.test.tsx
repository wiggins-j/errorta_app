import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent } from "@testing-library/react";
import BriefHistoryDropdown from "./BriefHistoryDropdown";

vi.mock("../../lib/api/briefs", () => ({
  listBriefHistory: vi.fn(),
  getBriefHistorySnapshot: vi.fn(),
  restoreBriefSnapshot: vi.fn(),
}));

import {
  listBriefHistory,
  getBriefHistorySnapshot,
  restoreBriefSnapshot,
} from "../../lib/api/briefs";

const listMock = vi.mocked(listBriefHistory);
const getMock = vi.mocked(getBriefHistorySnapshot);
const restoreMock = vi.mocked(restoreBriefSnapshot);

function makeEntries(n: number) {
  // Generated descending so the API contract (sorted-desc) is honoured by the
  // mock as well — the component should not re-sort, only slice to 5.
  return Array.from({ length: n }, (_, i) => {
    const idx = n - i;
    return {
      timestamp: `2026-06-0${Math.min(idx, 9)}T12000${idx}.000000Z`,
      byte_size: 100 + idx,
      sha256: "a".repeat(64),
    };
  });
}

async function flushPromises() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  listMock.mockReset();
  getMock.mockReset();
  restoreMock.mockReset();
});

describe("BriefHistoryDropdown", () => {
  it("renders a History button by default and no dropdown", () => {
    render(<BriefHistoryDropdown briefId="b1" />);
    expect(screen.getByRole("button", { name: /history/i })).toBeInTheDocument();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("opens the dropdown and shows up to 5 most recent snapshots", async () => {
    listMock.mockResolvedValue(makeEntries(8));
    render(<BriefHistoryDropdown briefId="b1" />);
    fireEvent.click(screen.getByRole("button", { name: /history/i }));
    await flushPromises();
    const items = screen.getAllByRole("menuitem");
    expect(items.length).toBe(5);
  });

  it("shows an empty-state when no history exists", async () => {
    listMock.mockResolvedValue([]);
    render(<BriefHistoryDropdown briefId="b1" />);
    fireEvent.click(screen.getByRole("button", { name: /history/i }));
    await flushPromises();
    expect(screen.getByText(/no history yet/i)).toBeInTheDocument();
  });

  it("opens a read-only modal with the snapshot body when an entry is clicked", async () => {
    listMock.mockResolvedValue(makeEntries(2));
    getMock.mockResolvedValue("# snapshot content\nhello");
    render(<BriefHistoryDropdown briefId="b1" />);
    fireEvent.click(screen.getByRole("button", { name: /history/i }));
    await flushPromises();
    const items = screen.getAllByRole("menuitem");
    fireEvent.click(items[0]);
    await flushPromises();
    const dialog = screen.getByRole("dialog");
    expect(dialog).toBeInTheDocument();
    const textarea = screen.getByLabelText(/snapshot markdown/i) as HTMLTextAreaElement;
    expect(textarea.value).toBe("# snapshot content\nhello");
    expect(textarea).toBeDisabled();
    // Restore button is enabled once the snapshot body is loaded.
    const restore = screen.getByRole("button", { name: /restore snapshot/i });
    expect(restore).not.toBeDisabled();
  });

  it("prompts via window.confirm and calls restoreBriefSnapshot + onRestore on confirm", async () => {
    listMock.mockResolvedValue(makeEntries(1));
    getMock.mockResolvedValue("snapshot body");
    restoreMock.mockResolvedValue({
      brief_id: "b1",
      corpus_name: "demo",
      state: "DRAFT",
      created_at: "2026-06-01T00:00:00Z",
      last_run_at: null,
    });
    const onRestore = vi.fn();
    const origConfirm = window.confirm;
    const confirmSpy = vi.fn().mockReturnValue(true);
    window.confirm = confirmSpy as unknown as typeof window.confirm;

    render(<BriefHistoryDropdown briefId="b1" onRestore={onRestore} />);
    fireEvent.click(screen.getByRole("button", { name: /history/i }));
    await flushPromises();
    fireEvent.click(screen.getAllByRole("menuitem")[0]);
    await flushPromises();

    const restoreBtn = screen.getByRole("button", { name: /restore snapshot/i });
    fireEvent.click(restoreBtn);
    await flushPromises();

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(restoreMock).toHaveBeenCalledWith("b1", expect.any(String));
    expect(onRestore).toHaveBeenCalledWith("snapshot body");
    // Modal and dropdown both close after success.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    window.confirm = origConfirm;
  });

  it("does not call restoreBriefSnapshot when confirm is cancelled", async () => {
    listMock.mockResolvedValue(makeEntries(1));
    getMock.mockResolvedValue("snapshot body");
    const onRestore = vi.fn();
    const origConfirm = window.confirm;
    const confirmSpy = vi.fn().mockReturnValue(false);
    window.confirm = confirmSpy as unknown as typeof window.confirm;

    render(<BriefHistoryDropdown briefId="b1" onRestore={onRestore} />);
    fireEvent.click(screen.getByRole("button", { name: /history/i }));
    await flushPromises();
    fireEvent.click(screen.getAllByRole("menuitem")[0]);
    await flushPromises();

    fireEvent.click(screen.getByRole("button", { name: /restore snapshot/i }));
    await flushPromises();

    expect(restoreMock).not.toHaveBeenCalled();
    expect(onRestore).not.toHaveBeenCalled();
    // Modal stays open.
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    window.confirm = origConfirm;
  });

  it("surfaces a restore error banner when restoreBriefSnapshot rejects", async () => {
    listMock.mockResolvedValue(makeEntries(1));
    getMock.mockResolvedValue("snapshot body");
    restoreMock.mockRejectedValue(new Error("parse failed"));
    const origConfirm2 = window.confirm;
    const confirmSpy = vi.fn().mockReturnValue(true);
    window.confirm = confirmSpy as unknown as typeof window.confirm;

    render(<BriefHistoryDropdown briefId="b1" />);
    fireEvent.click(screen.getByRole("button", { name: /history/i }));
    await flushPromises();
    fireEvent.click(screen.getAllByRole("menuitem")[0]);
    await flushPromises();

    fireEvent.click(screen.getByRole("button", { name: /restore snapshot/i }));
    await flushPromises();

    // Modal stays open and shows the inline error.
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText(/parse failed/i)).toBeInTheDocument();
    window.confirm = origConfirm2;
  });

  it("closes the modal when Close is clicked", async () => {
    listMock.mockResolvedValue(makeEntries(1));
    getMock.mockResolvedValue("body");
    render(<BriefHistoryDropdown briefId="b1" />);
    fireEvent.click(screen.getByRole("button", { name: /history/i }));
    await flushPromises();
    fireEvent.click(screen.getAllByRole("menuitem")[0]);
    await flushPromises();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^close$/i }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("surfaces a load error when listBriefHistory rejects", async () => {
    listMock.mockRejectedValue(new Error("sidecar offline"));
    render(<BriefHistoryDropdown briefId="b1" />);
    fireEvent.click(screen.getByRole("button", { name: /history/i }));
    await flushPromises();
    expect(screen.getByRole("alert")).toHaveTextContent(/sidecar offline/i);
  });

  // F008-DIFF — compare-mode coverage.

  it("toggling compare mode reveals checkboxes and hides menuitem role on rows", async () => {
    listMock.mockResolvedValue(makeEntries(3));
    render(<BriefHistoryDropdown briefId="b1" />);
    fireEvent.click(screen.getByRole("button", { name: /history/i }));
    await flushPromises();
    // Pre-toggle: rows have role=menuitem.
    expect(screen.getAllByRole("menuitem").length).toBe(3);
    // Toggle compare mode on.
    fireEvent.click(screen.getByRole("button", { name: /compare snapshots/i }));
    // No more menuitem rows.
    expect(screen.queryAllByRole("menuitem").length).toBe(0);
    // Checkboxes visible.
    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes.length).toBe(3);
    expect(checkboxes[0]).toHaveAttribute(
      "aria-label",
      expect.stringMatching(/select snapshot/i),
    );
  });

  it("shows 'Compare selected' only when exactly two checkboxes are selected", async () => {
    listMock.mockResolvedValue(makeEntries(3));
    render(<BriefHistoryDropdown briefId="b1" />);
    fireEvent.click(screen.getByRole("button", { name: /history/i }));
    await flushPromises();
    fireEvent.click(screen.getByRole("button", { name: /compare snapshots/i }));

    const checkboxes = screen.getAllByRole("checkbox");
    expect(
      screen.queryByRole("button", { name: /compare selected/i }),
    ).not.toBeInTheDocument();
    fireEvent.click(checkboxes[0]);
    expect(
      screen.queryByRole("button", { name: /compare selected/i }),
    ).not.toBeInTheDocument();
    fireEvent.click(checkboxes[1]);
    expect(
      screen.getByRole("button", { name: /compare selected/i }),
    ).toBeInTheDocument();
  });

  it("clicking 'Compare selected' fires exactly two getBriefHistorySnapshot calls and renders the diff view", async () => {
    listMock.mockResolvedValue(makeEntries(3));
    getMock.mockImplementation(async (_id: string, ts: string) => `body-${ts}`);
    render(<BriefHistoryDropdown briefId="b1" />);
    fireEvent.click(screen.getByRole("button", { name: /history/i }));
    await flushPromises();
    fireEvent.click(screen.getByRole("button", { name: /compare snapshots/i }));

    const checkboxes = screen.getAllByRole("checkbox");
    fireEvent.click(checkboxes[0]);
    fireEvent.click(checkboxes[1]);

    expect(getMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /compare selected/i }));
    await flushPromises();

    expect(getMock).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("briefs-diff-view")).toBeInTheDocument();
  });

  it("selecting a 3rd checkbox replaces the oldest selected (FIFO)", async () => {
    listMock.mockResolvedValue(makeEntries(3));
    getMock.mockImplementation(async (_id: string, ts: string) => `body-${ts}`);
    render(<BriefHistoryDropdown briefId="b1" />);
    fireEvent.click(screen.getByRole("button", { name: /history/i }));
    await flushPromises();
    fireEvent.click(screen.getByRole("button", { name: /compare snapshots/i }));

    const checkboxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
    // makeEntries(3) is descending: idx 1..3 mapped onto checkboxes 0..2 in
    // render order. We don't rely on ordering — just identity.
    fireEvent.click(checkboxes[0]); // selection: [0]
    fireEvent.click(checkboxes[1]); // selection: [0, 1]
    fireEvent.click(checkboxes[2]); // FIFO drop -> selection: [1, 2]

    expect(checkboxes[0].checked).toBe(false);
    expect(checkboxes[1].checked).toBe(true);
    expect(checkboxes[2].checked).toBe(true);

    // The two timestamps that should be fetched on compare are those of
    // checkboxes[1] and checkboxes[2].
    fireEvent.click(screen.getByRole("button", { name: /compare selected/i }));
    await flushPromises();
    expect(getMock).toHaveBeenCalledTimes(2);
    const calledTimestamps = getMock.mock.calls.map((c) => c[1]);
    // The dropped (oldest) checkbox's label encodes its timestamp — assert it
    // was NOT one of the two fetched.
    const droppedLabel = checkboxes[0].getAttribute("aria-label") ?? "";
    const droppedTs = droppedLabel.replace(/^Select snapshot\s+/i, "");
    expect(calledTimestamps).not.toContain(droppedTs);
  });

  // BRIEF-DIFF-KBD — keyboard nav + Restore Left/Right on the diff view.

  // Helper: open dropdown, enter compare mode, click checkboxes by index (in
  // order — the FIRST click becomes leftTs, the SECOND becomes rightTs), then
  // click Compare selected. Returns the entries used.
  async function openDiffView(
    leftIdx: number,
    rightIdx: number,
    n = 3,
  ) {
    const entries = makeEntries(n);
    listMock.mockResolvedValue(entries);
    getMock.mockImplementation(async (_id: string, ts: string) => `body-${ts}`);
    render(<BriefHistoryDropdown briefId="b1" />);
    fireEvent.click(screen.getByRole("button", { name: /history/i }));
    await flushPromises();
    fireEvent.click(screen.getByRole("button", { name: /compare snapshots/i }));
    const checkboxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
    fireEvent.click(checkboxes[leftIdx]);
    fireEvent.click(checkboxes[rightIdx]);
    fireEvent.click(screen.getByRole("button", { name: /compare selected/i }));
    await flushPromises();
    return entries;
  }

  it("ArrowUp swaps the left snapshot to the older neighbor and fetches its body", async () => {
    // leftIdx=1 (middle), rightIdx=0 (newest). Older neighbor of middle is
    // entries[2] (oldest).
    const entries = await openDiffView(1, 0, 3);
    expect(screen.getByTestId("briefs-diff-view")).toBeInTheDocument();
    // Initial left timestamp shown.
    expect(screen.getByTestId("briefs-diff-left-ts")).toHaveTextContent(
      entries[1].timestamp,
    );
    const callsBefore = getMock.mock.calls.length;

    fireEvent.keyDown(document, { key: "ArrowUp" });
    await flushPromises();

    // One additional fetch with the older neighbor timestamp.
    expect(getMock.mock.calls.length).toBe(callsBefore + 1);
    expect(getMock.mock.calls[callsBefore][1]).toBe(entries[2].timestamp);
    expect(screen.getByTestId("briefs-diff-left-ts")).toHaveTextContent(
      entries[2].timestamp,
    );
  });

  it("ArrowDown swaps the left snapshot to the newer neighbor and fetches its body", async () => {
    // leftIdx=2 (oldest), rightIdx=0 (newest). Newer neighbor of oldest is
    // entries[1] (middle).
    const entries = await openDiffView(2, 0, 3);
    expect(screen.getByTestId("briefs-diff-left-ts")).toHaveTextContent(
      entries[2].timestamp,
    );
    const callsBefore = getMock.mock.calls.length;

    fireEvent.keyDown(document, { key: "ArrowDown" });
    await flushPromises();

    expect(getMock.mock.calls.length).toBe(callsBefore + 1);
    expect(getMock.mock.calls[callsBefore][1]).toBe(entries[1].timestamp);
    expect(screen.getByTestId("briefs-diff-left-ts")).toHaveTextContent(
      entries[1].timestamp,
    );
  });

  it("ArrowUp is a no-op when the left snapshot is the oldest entry", async () => {
    // leftIdx=2 (oldest of 3). No older neighbor available.
    await openDiffView(2, 0, 3);
    const callsBefore = getMock.mock.calls.length;
    fireEvent.keyDown(document, { key: "ArrowUp" });
    await flushPromises();
    // No additional fetch.
    expect(getMock.mock.calls.length).toBe(callsBefore);
  });

  it("Escape closes the diff view", async () => {
    await openDiffView(1, 0, 3);
    expect(screen.getByTestId("briefs-diff-view")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByTestId("briefs-diff-view")).not.toBeInTheDocument();
  });

  it("Restore Left confirms, calls restoreBriefSnapshot with leftTs and onRestore with leftBody", async () => {
    const entries = makeEntries(3);
    listMock.mockResolvedValue(entries);
    getMock.mockImplementation(async (_id: string, ts: string) => `body-${ts}`);
    restoreMock.mockResolvedValue({
      brief_id: "b1",
      corpus_name: "demo",
      state: "DRAFT",
      created_at: "2026-06-01T00:00:00Z",
      last_run_at: null,
    });
    const onRestore = vi.fn();
    const origConfirm = window.confirm;
    window.confirm = vi.fn().mockReturnValue(true) as unknown as typeof window.confirm;

    render(<BriefHistoryDropdown briefId="b1" onRestore={onRestore} />);
    fireEvent.click(screen.getByRole("button", { name: /history/i }));
    await flushPromises();
    fireEvent.click(screen.getByRole("button", { name: /compare snapshots/i }));
    const checkboxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
    fireEvent.click(checkboxes[1]); // becomes leftTs
    fireEvent.click(checkboxes[0]); // becomes rightTs
    fireEvent.click(screen.getByRole("button", { name: /compare selected/i }));
    await flushPromises();

    fireEvent.click(
      screen.getByRole("button", { name: /restore left snapshot/i }),
    );
    await flushPromises();

    expect(restoreMock).toHaveBeenCalledWith("b1", entries[1].timestamp);
    expect(onRestore).toHaveBeenCalledWith(`body-${entries[1].timestamp}`);
    expect(screen.queryByTestId("briefs-diff-view")).not.toBeInTheDocument();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    window.confirm = origConfirm;
  });

  it("Restore Right confirms, calls restoreBriefSnapshot with rightTs and onRestore with rightBody", async () => {
    const entries = makeEntries(3);
    listMock.mockResolvedValue(entries);
    getMock.mockImplementation(async (_id: string, ts: string) => `body-${ts}`);
    restoreMock.mockResolvedValue({
      brief_id: "b1",
      corpus_name: "demo",
      state: "DRAFT",
      created_at: "2026-06-01T00:00:00Z",
      last_run_at: null,
    });
    const onRestore = vi.fn();
    const origConfirm = window.confirm;
    window.confirm = vi.fn().mockReturnValue(true) as unknown as typeof window.confirm;

    render(<BriefHistoryDropdown briefId="b1" onRestore={onRestore} />);
    fireEvent.click(screen.getByRole("button", { name: /history/i }));
    await flushPromises();
    fireEvent.click(screen.getByRole("button", { name: /compare snapshots/i }));
    const checkboxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
    fireEvent.click(checkboxes[1]); // becomes leftTs
    fireEvent.click(checkboxes[0]); // becomes rightTs
    fireEvent.click(screen.getByRole("button", { name: /compare selected/i }));
    await flushPromises();

    fireEvent.click(
      screen.getByRole("button", { name: /restore right snapshot/i }),
    );
    await flushPromises();

    expect(restoreMock).toHaveBeenCalledWith("b1", entries[0].timestamp);
    expect(onRestore).toHaveBeenCalledWith(`body-${entries[0].timestamp}`);
    expect(screen.queryByTestId("briefs-diff-view")).not.toBeInTheDocument();
    window.confirm = origConfirm;
  });

  it("exiting compare mode clears selection and restores single-snapshot click behavior", async () => {
    listMock.mockResolvedValue(makeEntries(3));
    getMock.mockResolvedValue("snapshot body");
    render(<BriefHistoryDropdown briefId="b1" />);
    fireEvent.click(screen.getByRole("button", { name: /history/i }));
    await flushPromises();
    fireEvent.click(screen.getByRole("button", { name: /compare snapshots/i }));
    const checkboxes = screen.getAllByRole("checkbox");
    fireEvent.click(checkboxes[0]);
    fireEvent.click(checkboxes[1]);
    expect(
      screen.getByRole("button", { name: /compare selected/i }),
    ).toBeInTheDocument();

    // Exit compare mode.
    fireEvent.click(screen.getByRole("button", { name: /exit compare/i }));
    // Compare button gone, checkboxes gone, menuitems back.
    expect(
      screen.queryByRole("button", { name: /compare selected/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryAllByRole("checkbox").length).toBe(0);
    const items = screen.getAllByRole("menuitem");
    expect(items.length).toBe(3);

    // Single-snapshot click-to-open still works.
    fireEvent.click(items[0]);
    await flushPromises();
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // Re-enter compare mode and assert selection was cleared.
    // Close the dialog first via the modal Close button.
    fireEvent.click(screen.getAllByRole("button", { name: /^close$/i })[0]);
    fireEvent.click(screen.getByRole("button", { name: /compare snapshots/i }));
    expect(
      screen.queryByRole("button", { name: /compare selected/i }),
    ).not.toBeInTheDocument();
  });
});
