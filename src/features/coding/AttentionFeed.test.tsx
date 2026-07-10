import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AttentionFeed from "./AttentionFeed";
import type { AttentionSignal } from "../../lib/api/coding";

vi.mock("../../lib/api/coding", () => {
  // Self-contained stand-in for the real ResolveSignalError (F123) so the
  // component's `e instanceof api.ResolveSignalError` check works under mock.
  class ResolveSignalError extends Error {
    status: number;
    detail: string;
    constructor(status: number, detail: string) {
      super(
        detail
          ? `resolve attention signal failed (${status}): ${detail}`
          : `resolve attention signal failed (${status})`,
      );
      this.name = "ResolveSignalError";
      this.status = status;
      this.detail = detail;
    }
  }
  return {
    getAttention: vi.fn(),
    resolveSignal: vi.fn(),
    ResolveSignalError,
  };
});

// eslint-disable-next-line @typescript-eslint/no-var-requires
import * as api from "../../lib/api/coding";
const mockGet = api.getAttention as unknown as ReturnType<typeof vi.fn>;
const mockResolve = api.resolveSignal as unknown as ReturnType<typeof vi.fn>;

afterEach(cleanup);

function problem(over: Partial<AttentionSignal> = {}): AttentionSignal {
  return {
    id: "sig-p1", kind: "problem", blocking: true, source: "pm",
    stage: "drafting_spec", title: "Pick storage", summary: "DB vs file?",
    pmEvaluation: "The spec is ambiguous on storage.",
    suggestions: [{ id: "s1", label: "Use SQLite", detail: "local db" }],
    state: "open", resolution: null, createdAt: "t1", ...over,
  };
}
function alert(over: Partial<AttentionSignal> = {}): AttentionSignal {
  return {
    id: "sig-a1", kind: "alert", blocking: false, source: "reviewer",
    stage: "reviewing_build", title: "button vs autosave",
    summary: "No guidance on save UX", pmEvaluation: null,
    suggestions: [], state: "open", resolution: null, createdAt: "t2", ...over,
  };
}

beforeEach(() => {
  mockGet.mockReset();
  mockResolve.mockReset();
});

describe("AttentionFeed", () => {
  it("renders Problems before Alerts with their actions", async () => {
    mockGet.mockResolvedValue({ signals: [alert(), problem()], blocksStage: true });
    const { container } = render(<AttentionFeed projectId="p" />);

    await screen.findByText("Pick storage");
    const cards = Array.from(container.querySelectorAll("article"));
    // Problem first despite Alert being first in the payload.
    expect(cards[0]).toHaveAttribute("aria-label", "Problem: Pick storage");
    expect(cards[1]).toHaveAttribute("aria-label", "Alert: button vs autosave");
    expect(screen.getByRole("alert", { name: "Problem: Pick storage" })).toBeInTheDocument();
    expect(screen.getByText("stage paused")).toBeInTheDocument();
    expect(screen.getByText("The spec is ambiguous on storage.")).toBeInTheDocument();
  });

  it("F128: renders a completion_blocked Problem with its open-work summary", async () => {
    mockGet.mockResolvedValue({
      signals: [problem({
        id: "sig-cb", source: "completion_blocked",
        title: "Run can't complete: open work remains",
        summary: "The team reported done, but 2 item(s) are still open: task Main Loop Integration [blocked] (human-required); +1 more.",
        suggestions: [{ id: "stop", label: "Stop and let me look", detail: "" }],
      })],
      blocksStage: true,
    });
    render(<AttentionFeed projectId="p" />);

    await screen.findByText("Run can't complete: open work remains");
    expect(
      screen.getByRole("alert", { name: "Problem: Run can't complete: open work remains" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/2 item\(s\) are still open/)).toBeInTheDocument();
    expect(screen.getByText("stage paused")).toBeInTheDocument();
  });

  it("accepting a suggestion resolves and reloads", async () => {
    mockGet
      .mockResolvedValueOnce({ signals: [problem()], blocksStage: true })
      .mockResolvedValueOnce({ signals: [], blocksStage: false });
    mockResolve.mockResolvedValue({ signal: problem({ state: "accepted" }), createdTaskId: "t-1" });
    const onChange = vi.fn();
    render(<AttentionFeed projectId="p" onChange={onChange} />);

    await screen.findByText("Pick storage");
    await userEvent.click(screen.getByRole("button", { name: /Accept: Use SQLite/ }));

    expect(mockResolve).toHaveBeenCalledWith("p", "sig-p1", {
      action: "accept", suggestionId: "s1",
    });
    await waitFor(() => expect(screen.getByText("Nothing needs you right now.")).toBeInTheDocument());
    expect(onChange).toHaveBeenCalled();
  });

  it("a correction submits the typed text", async () => {
    mockGet.mockResolvedValue({ signals: [problem()], blocksStage: true });
    mockResolve.mockResolvedValue({ signal: problem(), createdTaskId: "t-2" });
    render(<AttentionFeed projectId="p" />);

    await screen.findByText("Pick storage");
    await userEvent.click(screen.getByRole("button", { name: "Provide correction" }));
    await userEvent.type(screen.getByLabelText("Correction"), "Use Postgres");
    await userEvent.click(screen.getByRole("button", { name: "Submit correction" }));

    expect(mockResolve).toHaveBeenCalledWith("p", "sig-p1", {
      action: "correct", correctionText: "Use Postgres",
    });
  });

  it("alert defer/dismiss call resolve; alert never shows stage-paused", async () => {
    mockGet.mockResolvedValue({ signals: [alert()], blocksStage: false });
    mockResolve.mockResolvedValue({ signal: alert({ state: "deferred" }), createdTaskId: null });
    render(<AttentionFeed projectId="p" />);

    await screen.findByText("button vs autosave");
    await userEvent.click(screen.getByText("Needs attention"));
    await userEvent.click(screen.getByText("Alerts"));
    expect(screen.queryByText("stage paused")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Defer to PM" }));
    expect(mockResolve).toHaveBeenCalledWith("p", "sig-a1", { action: "defer" });
  });

  it("bulk 'Dismiss All' resolves every alert-kind signal, not problems", async () => {
    mockGet
      .mockResolvedValueOnce({
        signals: [
          alert({ id: "a1", title: "alpha" }),
          alert({ id: "a2", title: "beta" }),
          // A non-blocking problem lands in the Alerts group but is NOT a valid
          // defer/dismiss target — bulk must skip it.
          problem({ id: "p9", blocking: false, title: "soft problem" }),
        ],
        blocksStage: false,
      })
      .mockResolvedValueOnce({ signals: [], blocksStage: false });
    mockResolve.mockResolvedValue({ signal: alert({ state: "dismissed" }), createdTaskId: null });
    render(<AttentionFeed projectId="p" />);

    await screen.findByText("alpha");
    await userEvent.click(screen.getByRole("button", { name: "Dismiss All" }));

    expect(mockResolve).toHaveBeenCalledWith("p", "a1", { action: "dismiss" });
    expect(mockResolve).toHaveBeenCalledWith("p", "a2", { action: "dismiss" });
    expect(mockResolve).not.toHaveBeenCalledWith("p", "p9", expect.anything());
    expect(mockResolve).toHaveBeenCalledTimes(2);
  });

  it("bulk 'Accept All' / 'Defer All' use the right action; hidden when no alerts", async () => {
    mockGet.mockResolvedValue({
      signals: [alert({ id: "a1", title: "alpha" }), alert({ id: "a2", title: "beta" })],
      blocksStage: false,
    });
    mockResolve.mockResolvedValue({ signal: alert({ state: "accepted" }), createdTaskId: null });
    render(<AttentionFeed projectId="p" />);
    await screen.findByText("alpha");
    await userEvent.click(screen.getByRole("button", { name: "Accept All" }));
    expect(mockResolve).toHaveBeenCalledWith("p", "a1", { action: "accept" });
    expect(mockResolve).toHaveBeenCalledWith("p", "a2", { action: "accept" });
  });

  it("bulk buttons are absent when there are no alert-kind signals", async () => {
    mockGet.mockResolvedValue({ signals: [problem()], blocksStage: true });
    render(<AttentionFeed projectId="p" />);
    await screen.findByText("Pick storage");
    expect(screen.queryByRole("button", { name: "Dismiss All" })).toBeNull();
  });

  it("renders a calm empty state with no open signals", async () => {
    mockGet.mockResolvedValue({ signals: [], blocksStage: false });
    render(<AttentionFeed projectId="p" />);
    expect(await screen.findByText("Nothing needs you right now.")).toBeInTheDocument();
  });

  // --- F123 ---

  it("F123: header reads 'Needs attention' (not 'Needs your attention')", async () => {
    mockGet.mockResolvedValue({ signals: [problem(), alert()], blocksStage: true });
    render(<AttentionFeed projectId="p" />);
    expect(await screen.findByText("Needs attention")).toBeInTheDocument();
    expect(screen.queryByText("Needs your attention")).not.toBeInTheDocument();
  });

  it("F123: showstoppers auto-open while alerts stay collapsed, with counts", async () => {
    mockGet.mockResolvedValue({
      signals: [problem(), problem({ id: "sig-p2" }), alert()],
      blocksStage: true,
    });
    const { container } = render(<AttentionFeed projectId="p" />);
    await screen.findByText("Needs attention");

    const detailsEls = Array.from(container.querySelectorAll("details"));
    // outer panel + two sub-panels.
    expect(detailsEls.length).toBe(3);
    expect(detailsEls[0]).toHaveAttribute("open");
    expect(screen.getByText("Showstoppers").closest("details")).toHaveAttribute("open");
    expect(screen.getByText("Alerts").closest("details")).not.toHaveAttribute("open");

    // counts: total 3, Showstoppers 2, Alerts 1.
    expect(screen.getByText("Showstoppers").closest("summary")).toHaveTextContent("2");
    expect(screen.getByText("Alerts").closest("summary")).toHaveTextContent("1");
    expect(screen.getByText("Needs attention").closest("summary")).toHaveTextContent("3");
  });

  it("F123: newest signal renders first within a group", async () => {
    mockGet.mockResolvedValue({
      signals: [
        problem({ id: "old", title: "Older problem", createdAt: "2026-06-01T00:00:00Z" }),
        problem({ id: "new", title: "Newer problem", createdAt: "2026-06-02T00:00:00Z" }),
      ],
      blocksStage: true,
    });
    const { container } = render(<AttentionFeed projectId="p" />);
    await screen.findByText("Newer problem");
    const cards = Array.from(container.querySelectorAll("article"));
    expect(cards[0]).toHaveAttribute("aria-label", "Problem: Newer problem");
    expect(cards[1]).toHaveAttribute("aria-label", "Problem: Older problem");
  });

  it("F123: non-blocking Problems are advisory and keep Problem actions", async () => {
    mockGet.mockResolvedValue({
      signals: [
        problem({
          id: "sig-p-nonblocking",
          blocking: false,
          title: "Advisory problem",
        }),
      ],
      blocksStage: false,
    });
    const { container } = render(<AttentionFeed projectId="p" />);
    await screen.findByText("Needs attention");

    expect(screen.getByText("Showstoppers").closest("summary")).toHaveTextContent("0");
    expect(screen.getByText("Alerts").closest("summary")).toHaveTextContent("1");
    expect(screen.getByText("Needs attention").closest("details")).not.toHaveAttribute("open");

    const card = container.querySelector('article[aria-label="Problem: Advisory problem"]');
    expect(card).toBeInTheDocument();
    expect(card).not.toHaveAttribute("role", "alert");
    expect(screen.getByRole("button", { name: /Accept: Use SQLite/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Dismiss" })).not.toBeInTheDocument();
  });

  it("F123: a failing resolve surfaces the backend's structured reason", async () => {
    mockGet.mockResolvedValue({ signals: [alert()], blocksStage: false });
    mockResolve.mockRejectedValue(
      new api.ResolveSignalError(422, "action 'defer' not valid for this signal"),
    );
    render(<AttentionFeed projectId="p" />);

    await screen.findByText("button vs autosave");
    await userEvent.click(screen.getByText("Needs attention"));
    await userEvent.click(screen.getByText("Alerts"));
    await userEvent.click(screen.getByRole("button", { name: "Defer to PM" }));

    await waitFor(() =>
      expect(
        screen.getByText(/action 'defer' not valid for this signal/),
      ).toBeInTheDocument(),
    );
  });

  it("F123: an already-resolved (409) signal refreshes instead of erroring", async () => {
    mockGet
      .mockResolvedValueOnce({ signals: [alert()], blocksStage: false })
      .mockResolvedValueOnce({ signals: [], blocksStage: false });
    mockResolve.mockRejectedValue(new api.ResolveSignalError(409, "signal is not open (state=dismissed)"));
    render(<AttentionFeed projectId="p" />);

    await screen.findByText("button vs autosave");
    await userEvent.click(screen.getByText("Needs attention"));
    await userEvent.click(screen.getByText("Alerts"));
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    // refreshed to empty; no raw error surfaced.
    await waitFor(() =>
      expect(screen.getByText("Nothing needs you right now.")).toBeInTheDocument(),
    );
    expect(screen.queryByText(/signal is not open/)).not.toBeInTheDocument();
    expect(screen.queryByText(/failed \(409\)/)).not.toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledTimes(2);
  });
});
