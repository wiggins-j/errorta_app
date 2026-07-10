import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import BriefsList, { stateBadgeClass } from "./BriefsList";
import type { BriefSummary } from "../../lib/api/briefs";
import type { BriefStateValue } from "./types";

vi.mock("../../lib/api/briefs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/briefs")>();
  return {
    ...actual,
    createBrief: vi.fn(),
    fetchBriefTemplates: vi.fn(),
  };
});

import { createBrief, fetchBriefTemplates } from "../../lib/api/briefs";

const createBriefMock = vi.mocked(createBrief);
const fetchBriefTemplatesMock = vi.mocked(fetchBriefTemplates);

beforeEach(() => {
  createBriefMock.mockReset();
  fetchBriefTemplatesMock.mockReset();
  fetchBriefTemplatesMock.mockResolvedValue([]);
});

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function makeBriefSummary(overrides: Partial<BriefSummary> = {}): BriefSummary {
  return {
    brief_id: "brief-1",
    corpus_name: "demo-corpus",
    state: "DRAFT",
    created_at: "2026-06-01T12:00:00+00:00",
    last_run_at: null,
    ...overrides,
  };
}

describe("BriefsList", () => {
  it("renders empty state when no briefs are provided", () => {
    render(<BriefsList briefs={[]} selectedId={null} onSelect={() => {}} />);
    expect(
      screen.getByText(/no briefs yet\. create your first brief/i),
    ).toBeInTheDocument();
  });

  it("renders one list item per brief with the corpus name visible", () => {
    const briefs = [
      makeBriefSummary({ brief_id: "a", corpus_name: "alpha" }),
      makeBriefSummary({ brief_id: "b", corpus_name: "bravo" }),
    ];
    render(<BriefsList briefs={briefs} selectedId={null} onSelect={() => {}} />);
    expect(screen.getByText("alpha")).toBeInTheDocument();
    expect(screen.getByText("bravo")).toBeInTheDocument();
    // 2 brief item buttons + Templates + Import brief + Import bundle = 5
    expect(screen.getAllByRole("button")).toHaveLength(5);
  });

  it("applies aria-current=true on the selected item only", () => {
    const briefs = [
      makeBriefSummary({ brief_id: "a", corpus_name: "alpha" }),
      makeBriefSummary({ brief_id: "b", corpus_name: "bravo" }),
    ];
    render(<BriefsList briefs={briefs} selectedId="b" onSelect={() => {}} />);
    // Filter to only brief item buttons (those with aria-current attribute potential).
    const briefButtons = screen
      .getAllByRole("button")
      .filter((b) => b.classList.contains("briefs-list-item"));
    expect(briefButtons[0]).not.toHaveAttribute("aria-current");
    expect(briefButtons[1]).toHaveAttribute("aria-current", "true");
  });

  it("fires onSelect with the brief_id when an item is clicked", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    const briefs = [
      makeBriefSummary({ brief_id: "alpha-id", corpus_name: "alpha" }),
    ];
    render(<BriefsList briefs={briefs} selectedId={null} onSelect={onSelect} />);
    await user.click(screen.getByRole("button", { name: /alpha/ }));
    expect(onSelect).toHaveBeenCalledWith("alpha-id");
  });

  it("renders the state badge text for each brief", () => {
    const briefs = [
      makeBriefSummary({ brief_id: "a", corpus_name: "alpha", state: "RUNNING" }),
      makeBriefSummary({ brief_id: "b", corpus_name: "bravo", state: "COMPLETED" }),
    ];
    render(<BriefsList briefs={briefs} selectedId={null} onSelect={() => {}} />);
    expect(screen.getByText("RUNNING")).toBeInTheDocument();
    expect(screen.getByText("COMPLETED")).toBeInTheDocument();
  });

  it("opens the CreateBriefModal when the Templates header button is clicked", async () => {
    const user = userEvent.setup();
    render(<BriefsList briefs={[]} selectedId={null} onSelect={() => {}} />);
    expect(screen.queryByRole("dialog", { name: /create brief/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /templates/i }));
    expect(screen.getByRole("dialog", { name: /create brief/i })).toBeInTheDocument();
  });

  it("fires onBriefCreated and onSelect after the modal successfully creates a brief", async () => {
    const user = userEvent.setup();
    const onBriefCreated = vi.fn();
    const onSelect = vi.fn();
    createBriefMock.mockResolvedValue({
      brief_id: "new-id-42",
      corpus_name: "fresh",
      state: "DRAFT",
      created_at: "2026-06-01T00:00:00Z",
      last_run_at: null,
    });
    render(
      <BriefsList
        briefs={[]}
        selectedId={null}
        onSelect={onSelect}
        onBriefCreated={onBriefCreated}
      />,
    );
    await user.click(screen.getByRole("button", { name: /templates/i }));
    await user.click(screen.getByRole("button", { name: /^create$/i }));
    await flush();
    expect(onBriefCreated).toHaveBeenCalledWith("new-id-42", "fresh");
    expect(onSelect).toHaveBeenCalledWith("new-id-42");
    // Modal closes after success.
    expect(screen.queryByRole("dialog", { name: /create brief/i })).not.toBeInTheDocument();
  });

  it("closes the modal on cancel without firing onBriefCreated", async () => {
    const user = userEvent.setup();
    const onBriefCreated = vi.fn();
    render(
      <BriefsList
        briefs={[]}
        selectedId={null}
        onSelect={() => {}}
        onBriefCreated={onBriefCreated}
      />,
    );
    await user.click(screen.getByRole("button", { name: /templates/i }));
    expect(screen.getByRole("dialog", { name: /create brief/i })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /cancel/i }));
    expect(screen.queryByRole("dialog", { name: /create brief/i })).not.toBeInTheDocument();
    expect(onBriefCreated).not.toHaveBeenCalled();
    expect(createBriefMock).not.toHaveBeenCalled();
  });

  it("maps each FSM state to the correct badge class via stateBadgeClass", () => {
    const cases: Array<[BriefStateValue, string]> = [
      ["DRAFT", "pin-editable"],
      ["VALIDATING", "pin-editable"],
      ["RUNNING", "pin-editable"],
      ["COMPLETED", "pin-pinned"],
      ["FAILED", "pin-absent"],
      ["ARCHIVED", "pin-absent"],
      ["PAUSED", "pin-absent"],
    ];
    for (const [state, expected] of cases) {
      expect(stateBadgeClass(state)).toBe(expected);
    }
  });
});
