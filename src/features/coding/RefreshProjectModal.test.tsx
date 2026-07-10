import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/coding", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/coding")>();
  return { ...actual, refreshProject: vi.fn(), refreshProjectStatus: vi.fn() };
});

import * as api from "../../lib/api/coding";
import RefreshProjectModal from "./RefreshProjectModal";

const refreshProject = vi.mocked(api.refreshProject);
const refreshProjectStatus = vi.mocked(api.refreshProjectStatus);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function preview(over: Partial<api.RefreshPreview> = {}): api.RefreshPreview {
  return {
    target: "existing",
    repoPathExists: true,
    snapshotRef: "aaa",
    repoHead: "bbb",
    repoDirty: false,
    repoDiffers: true,
    workspaceHasUnacceptedChanges: false,
    originPresent: true,
    defaultBranch: "main",
    shallow: false,
    localAhead: 0,
    remoteAhead: 2,
    ...over,
  };
}

function open(over: Partial<api.RefreshPreview> = {}, handlers = {}) {
  const onClose = vi.fn();
  const onRefreshed = vi.fn();
  render(
    <RefreshProjectModal
      isOpen
      projectId="p"
      preview={preview(over)}
      onClose={onClose}
      onRefreshed={onRefreshed}
      {...handlers}
    />,
  );
  return { onClose, onRefreshed };
}

describe("RefreshProjectModal", () => {
  it("shows the remote-ahead summary", () => {
    open();
    expect(screen.getByText(/remote is 2 commits ahead/i)).toBeInTheDocument();
    expect(screen.getByText(/origin\/main/)).toBeInTheDocument();
  });

  it("runs a pull+re-seed and calls onRefreshed + onClose on success", async () => {
    refreshProject.mockResolvedValue({ jobId: "j", status: "done", message: null, projectId: "p" });
    const { onClose, onRefreshed } = open();
    fireEvent.click(screen.getByRole("button", { name: /Pull and re-seed/ }));
    await waitFor(() => expect(onRefreshed).toHaveBeenCalled());
    expect(onClose).toHaveBeenCalled();
    expect(refreshProject).toHaveBeenCalledWith("p", { pull: true, discardWorkspace: false });
  });

  it("maps a backend refusal reason to actionable copy", async () => {
    refreshProject.mockResolvedValue({ jobId: "j", status: "error", message: "repo_dirty", projectId: null });
    const { onRefreshed } = open();
    fireEvent.click(screen.getByRole("button", { name: /Pull and re-seed/ }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/uncommitted changes/i),
    );
    expect(onRefreshed).not.toHaveBeenCalled();
  });

  it("gates the action behind Discard when the snapshot has un-accepted work", async () => {
    refreshProject.mockResolvedValue({ jobId: "j", status: "done", message: null, projectId: "p" });
    open({ workspaceHasUnacceptedChanges: true });
    const btn = screen.getByRole("button", { name: /Pull and re-seed/ });
    expect(btn).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(btn).toBeEnabled();
    fireEvent.click(btn);
    await waitFor(() =>
      expect(refreshProject).toHaveBeenCalledWith("p", { pull: true, discardWorkspace: true }),
    );
  });

  it("labels the action 'Re-seed from folder' with no origin", () => {
    open({ originPresent: false, remoteAhead: null });
    expect(screen.getByRole("button", { name: /Re-seed from folder/ })).toBeInTheDocument();
  });

  it("does not allow a refresh without a loaded preview", () => {
    render(
      <RefreshProjectModal
        isOpen
        projectId="p"
        preview={null}
        onClose={vi.fn()}
        onRefreshed={vi.fn()}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(/preview is unavailable/i);
    expect(screen.getByRole("button", { name: /Re-seed from folder/ })).toBeDisabled();
  });

  it("does not fire stale callbacks after the modal is dismissed", async () => {
    vi.useFakeTimers();
    try {
      refreshProject.mockResolvedValue({
        jobId: "j",
        status: "refreshing",
        message: null,
        projectId: "p",
      });
      refreshProjectStatus.mockResolvedValue({
        jobId: "j",
        status: "done",
        message: null,
        projectId: "p",
      });
      const onClose = vi.fn();
      const onRefreshed = vi.fn();
      const view = render(
        <RefreshProjectModal
          isOpen
          projectId="p"
          preview={preview()}
          onClose={onClose}
          onRefreshed={onRefreshed}
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: /Pull and re-seed/ }));
      await act(async () => {
        await Promise.resolve();
      });
      view.rerender(
        <RefreshProjectModal
          isOpen={false}
          projectId="p"
          preview={preview()}
          onClose={onClose}
          onRefreshed={onRefreshed}
        />,
      );
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(refreshProjectStatus).not.toHaveBeenCalled();
      expect(onRefreshed).not.toHaveBeenCalled();
      expect(onClose).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("closes on Escape", () => {
    const { onClose } = open();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });
});
