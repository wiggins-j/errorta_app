import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/coding", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/coding")>();
  return {
    ...actual,
    listFocuses: vi.fn(),
    addFocus: vi.fn(),
    reorderFocuses: vi.fn(),
    updateFocus: vi.fn(),
    acceptFocus: vi.fn(),
  };
});

import * as api from "../../lib/api/coding";
import CurrentFocusPanel from "./CurrentFocusPanel";

const listFocuses = vi.mocked(api.listFocuses);
const addFocus = vi.mocked(api.addFocus);
const reorderFocuses = vi.mocked(api.reorderFocuses);
const updateFocus = vi.mocked(api.updateFocus);
const acceptFocus = vi.mocked(api.acceptFocus);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function focus(over: Partial<api.Focus> = {}): api.Focus {
  return {
    id: "focus-1",
    title: "a focus",
    body: "",
    status: "active",
    order: 0,
    origin: "user",
    createdAt: "2026-07-02T00:00:00Z",
    completedAt: "",
    acceptedAt: "",
    archivedAt: "",
    completionSummary: "",
    ...over,
  };
}

// listFocuses is called for "active" then "archived" — resolve by argument.
function mockLists(
  active: api.Focus[],
  archived: api.Focus[] = [],
  completed: api.Focus[] = [],
) {
  listFocuses.mockImplementation(async (_p, status) =>
    status === "archived" ? archived : status === "completed" ? completed : active,
  );
}

describe("CurrentFocusPanel", () => {
  it("shows the empty state when there are no focuses", async () => {
    mockLists([]);
    render(<CurrentFocusPanel projectId="p" onError={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/No Current Focus yet/)).toBeInTheDocument());
  });

  it("lists active focuses in order", async () => {
    mockLists([
      focus({ id: "f1", title: "first" }),
      focus({ id: "f2", title: "second", order: 1 }),
    ]);
    render(<CurrentFocusPanel projectId="p" onError={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("first")).toBeInTheDocument());
    expect(screen.getByText("second")).toBeInTheDocument();
  });

  it("adds a focus", async () => {
    mockLists([]);
    addFocus.mockResolvedValue(focus({ title: "new one" }));
    const onChanged = vi.fn();
    render(<CurrentFocusPanel projectId="p" onError={vi.fn()} onChanged={onChanged} />);
    await waitFor(() => expect(listFocuses).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText("New focus"), {
      target: { value: "Make the rooms panel collapsible" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add focus" }));
    await waitFor(() =>
      expect(addFocus).toHaveBeenCalledWith("p", "Make the rooms panel collapsible"),
    );
    expect(onChanged).toHaveBeenCalled();
  });

  it("reorders via the down button", async () => {
    mockLists([
      focus({ id: "f1", title: "first" }),
      focus({ id: "f2", title: "second", order: 1 }),
    ]);
    reorderFocuses.mockResolvedValue([
      focus({ id: "f2", title: "second" }),
      focus({ id: "f1", title: "first", order: 1 }),
    ]);
    render(<CurrentFocusPanel projectId="p" onError={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("first")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Move focus 1 down" }));
    await waitFor(() => expect(reorderFocuses).toHaveBeenCalledWith("p", ["f2", "f1"]));
  });

  it("archives (drops) an active focus", async () => {
    mockLists([focus({ id: "f1", title: "drop me" })]);
    updateFocus.mockResolvedValue(focus({ id: "f1", status: "archived" }));
    render(<CurrentFocusPanel projectId="p" onError={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("drop me")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Archive" }));
    await waitFor(() =>
      expect(updateFocus).toHaveBeenCalledWith("p", "f1", { status: "archived" }),
    );
  });

  it("marks an active focus complete", async () => {
    mockLists([focus({ id: "f1", title: "finished work" })]);
    updateFocus.mockResolvedValue(focus({ id: "f1", status: "completed" }));
    render(<CurrentFocusPanel projectId="p" onError={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("finished work")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Mark complete" }));
    await waitFor(() =>
      expect(updateFocus).toHaveBeenCalledWith("p", "f1", { status: "completed" }),
    );
  });

  it("accepts a PM-completed focus and shows the badge", async () => {
    mockLists([], [], [focus({ id: "f1", title: "done work", status: "completed" })]);
    acceptFocus.mockResolvedValue(focus({ id: "f1", status: "archived" }));
    render(<CurrentFocusPanel projectId="p" onError={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("Ready for review")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(acceptFocus).toHaveBeenCalledWith("p", "f1"));
  });

  it("disables Accept while a run is live", async () => {
    mockLists([], [], [focus({ id: "f1", title: "done", status: "completed" })]);
    render(<CurrentFocusPanel projectId="p" running onError={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Accept" })).toBeDisabled());
  });

  it("renders archived history read-only", async () => {
    mockLists([], [focus({ id: "a1", title: "old goal", status: "archived", archivedAt: "2026-06-01T00:00:00Z" })]);
    render(<CurrentFocusPanel projectId="p" onError={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/Archived focuses/)).toBeInTheDocument());
    expect(screen.getByText("old goal")).toBeInTheDocument();
  });

  // F141 WS-I — phase gating.
  it("hides the panel and shows the North Star in the north_star phase with no focuses", async () => {
    mockLists([]);
    render(
      <CurrentFocusPanel
        projectId="p"
        onError={vi.fn()}
        phase="north_star"
        northStar="Ship the MVP"
      />,
    );
    await waitFor(() =>
      expect(screen.getByText("Building toward")).toBeInTheDocument(),
    );
    expect(screen.getByText("Ship the MVP")).toBeInTheDocument();
    expect(screen.queryByText(/No Current Focus yet/)).toBeNull();
  });

  it("shows the panel in the north_star phase when focuses already exist", async () => {
    mockLists([focus({ id: "f1", title: "keep going" })]);
    render(
      <CurrentFocusPanel projectId="p" onError={vi.fn()} phase="north_star" />,
    );
    await waitFor(() => expect(screen.getByText("keep going")).toBeInTheDocument());
    expect(screen.queryByText("Building toward")).toBeNull();
  });

  it("shows the panel with a Now chip in the steering phase", async () => {
    mockLists([focus({ id: "f1", title: "top thing" })]);
    render(
      <CurrentFocusPanel projectId="p" onError={vi.fn()} phase="steering" />,
    );
    await waitFor(() => expect(screen.getByText(/Now: top thing/)).toBeInTheDocument());
  });
});
