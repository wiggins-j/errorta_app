import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

vi.mock("../../lib/api/council", () => ({
  getApplyWorkspace: vi.fn(),
  acceptApplyWorkspace: vi.fn(),
}));

import { acceptApplyWorkspace, getApplyWorkspace } from "../../lib/api/council";
import ApplyWorkspacePanel from "./ApplyWorkspacePanel";

const _get = getApplyWorkspace as unknown as ReturnType<typeof vi.fn>;
const _accept = acceptApplyWorkspace as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ApplyWorkspacePanel", () => {
  it("renders nothing when there is no apply workspace", async () => {
    _get.mockResolvedValue(null);
    const { container } = render(<ApplyWorkspacePanel runId="r-1" />);
    await waitFor(() => expect(_get).toHaveBeenCalled());
    expect(container.querySelector(".apply-workspace-panel")).toBeNull();
  });

  it("renders nothing when there are no changes", async () => {
    _get.mockResolvedValue({
      runId: "r-1", source: "/p", hasChanges: false,
      changedFiles: [], conflicts: [], diff: "",
    });
    const { container } = render(<ApplyWorkspacePanel runId="r-1" />);
    await waitFor(() => expect(_get).toHaveBeenCalled());
    expect(container.querySelector(".apply-workspace-panel")).toBeNull();
  });

  it("shows changes and applies on explicit click (the human gate)", async () => {
    _get.mockResolvedValue({
      runId: "r-1", source: "/proj", hasChanges: true,
      changedFiles: [{ path: "app.py", status: "M" }],
      conflicts: [], diff: "--- a/app.py\n+++ b/app.py\n+x = 9\n",
    });
    _accept.mockResolvedValue({
      applied: true, written: ["app.py"], deleted: [], conflicts: [],
    });
    render(<ApplyWorkspacePanel runId="r-1" />);
    await waitFor(() => screen.getByTestId("apply-changed-files"));
    expect(screen.getByText("app.py")).toBeInTheDocument();
    // Nothing applied until the user clicks.
    expect(_accept).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("apply-to-files"));
    await waitFor(() => expect(_accept).toHaveBeenCalledWith("r-1", { allowConflicts: false }));
    await waitFor(() => screen.getByTestId("apply-applied"));
  });

  it("offers an explicit overwrite path only when there are conflicts", async () => {
    _get.mockResolvedValue({
      runId: "r-1", source: "/proj", hasChanges: true,
      changedFiles: [{ path: "app.py", status: "M" }],
      conflicts: ["app.py"], diff: "",
    });
    _accept.mockResolvedValue({
      applied: true, written: ["app.py"], deleted: [], conflicts: ["app.py"],
    });
    render(<ApplyWorkspacePanel runId="r-1" />);
    await waitFor(() => screen.getByTestId("apply-conflict-flag"));
    // The plain apply button is hidden; only the explicit overwrite is offered.
    expect(screen.queryByTestId("apply-to-files")).toBeNull();
    fireEvent.click(screen.getByTestId("apply-overwrite"));
    await waitFor(() =>
      expect(_accept).toHaveBeenCalledWith("r-1", { allowConflicts: true }),
    );
  });
});
