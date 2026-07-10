import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/coding", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/coding")>();
  return {
    ...actual,
    getTeamLog: vi.fn(),
  };
});

import { getTeamLog } from "../../lib/api/coding";
import TeamLog from "./TeamLog";

const mockGetTeamLog = getTeamLog as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const ENTRIES = [
  { at: "2026-06-20T22:48:44Z", role: "pm", member: "", kind: "north_star",
    message: "reviewed the North Star: Build a shipping calculator" },
  { at: "2026-06-20T22:49:23Z", role: "dev", member: "m-2", kind: "pr_opened",
    message: "completed the work and opened a PR for: Create shipping.py" },
  { at: "2026-06-20T22:51:05Z", role: "pm", member: "", kind: "pr_merged",
    message: "merged Create shipping.py into the project" },
];

describe("TeamLog", () => {
  it("renders the narrative entries with the title and count", async () => {
    mockGetTeamLog.mockResolvedValue(ENTRIES);
    render(<TeamLog projectId="p1" />);
    await waitFor(() => expect(mockGetTeamLog).toHaveBeenCalledWith("p1"));
    expect(screen.getByText("Team Log")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("3")).toBeInTheDocument());
    expect(
      screen.getByText(/reviewed the North Star: Build a shipping calculator/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/completed the work and opened a PR/),
    ).toBeInTheDocument();
    expect(screen.getByText(/merged Create shipping.py into the project/)).toBeInTheDocument();
  });

  it("renders newest on top (descending order)", async () => {
    mockGetTeamLog.mockResolvedValue(ENTRIES);
    const { container } = render(<TeamLog projectId="p1" />);
    await waitFor(() => expect(mockGetTeamLog).toHaveBeenCalled());
    const messages = Array.from(container.querySelectorAll(".coding-tl-msg")).map(
      (n) => n.textContent,
    );
    // ENTRIES are chronological (north_star -> pr_opened -> pr_merged); the panel
    // shows the most recent first.
    expect(messages[0]).toMatch(/merged Create shipping.py/);
    expect(messages[messages.length - 1]).toMatch(/reviewed the North Star/);
  });

  it("shows a role tag + member name without doubling the actor", async () => {
    mockGetTeamLog.mockResolvedValue(ENTRIES);
    const { container } = render(<TeamLog projectId="p1" />);
    await waitFor(() => expect(mockGetTeamLog).toHaveBeenCalled());
    // role tags rendered as badges
    const tags = Array.from(container.querySelectorAll(".coding-tl-tag")).map(
      (n) => n.textContent,
    );
    expect(tags).toEqual(["PM", "DEV", "PM"]);
    // the dev entry shows its member name once
    expect(screen.getByText("m-2")).toBeInTheDocument();
    expect(container.querySelectorAll(".coding-tl-name")).toHaveLength(1);
    // the doubled "Developer (m-2) Developer (m-2)" must never appear
    expect(container.textContent).not.toMatch(/Developer \(m-2\)/);
  });

  it("is collapsed by default", async () => {
    mockGetTeamLog.mockResolvedValue(ENTRIES);
    const { container } = render(<TeamLog projectId="p1" />);
    await waitFor(() => expect(mockGetTeamLog).toHaveBeenCalled());
    const details = container.querySelector("details.coding-team-log") as HTMLDetailsElement;
    expect(details).toBeTruthy();
    expect(details.open).toBe(false);
  });

  it("renders a human file edit as a YOU badge without doubling the message", async () => {
    mockGetTeamLog.mockResolvedValue([
      { at: "2026-06-20T23:00:00Z", role: "user", member: "", kind: "human_file_edit",
        message: "edited src/app.py" },
    ]);
    const { container } = render(<TeamLog projectId="p1" />);
    await waitFor(() => expect(mockGetTeamLog).toHaveBeenCalled());
    const tags = Array.from(container.querySelectorAll(".coding-tl-tag")).map((n) => n.textContent);
    expect(tags).toEqual(["YOU"]);
    // the message renders exactly once (no actor-prefix doubling)
    expect(screen.getByText("edited src/app.py")).toBeInTheDocument();
    expect(container.querySelectorAll(".coding-tl-msg")).toHaveLength(1);
    // no member name for a user entry
    expect(container.querySelectorAll(".coding-tl-name")).toHaveLength(0);
    // the user-role styling hook is applied
    expect(container.querySelector(".coding-tl-user")).toBeTruthy();
    expect(container.querySelector(".coding-tl-tag-user")).toBeTruthy();
  });

  it("shows an empty state when there is no activity", async () => {
    mockGetTeamLog.mockResolvedValue([]);
    render(<TeamLog projectId="p1" />);
    await waitFor(() => expect(screen.getByText("No team activity yet.")).toBeInTheDocument());
  });

  it("surfaces an error without crashing", async () => {
    mockGetTeamLog.mockRejectedValue(new Error("boom"));
    render(<TeamLog projectId="p1" />);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("boom"));
  });
});
