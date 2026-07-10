import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/coding", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/coding")>();
  return {
    ...actual,
    getNorthStarProposal: vi.fn(),
    startOrientationScan: vi.fn(),
    orientationScanStatus: vi.fn(),
    acceptNorthStarProposal: vi.fn(),
    getRefreshPreview: vi.fn(),
    refreshProject: vi.fn(),
    refreshProjectStatus: vi.fn(),
    // F137: OnboardingPanel now embeds CurrentFocusPanel, which loads focuses.
    listFocuses: vi.fn().mockResolvedValue([]),
  };
});

import * as api from "../../lib/api/coding";
import OnboardingPanel from "./OnboardingPanel";

const getNorthStarProposal = vi.mocked(api.getNorthStarProposal);
const startOrientationScan = vi.mocked(api.startOrientationScan);
const orientationScanStatus = vi.mocked(api.orientationScanStatus);
const acceptNorthStarProposal = vi.mocked(api.acceptNorthStarProposal);
const getRefreshPreview = vi.mocked(api.getRefreshPreview);

function stalePreview(over: Partial<api.RefreshPreview> = {}): api.RefreshPreview {
  return {
    target: "existing", repoPathExists: true, snapshotRef: "a", repoHead: "b",
    repoDirty: false, repoDiffers: true, workspaceHasUnacceptedChanges: false,
    originPresent: true, defaultBranch: "main", shallow: false,
    localAhead: 0, remoteAhead: 3, ...over,
  };
}

beforeEach(() => {
  getRefreshPreview.mockResolvedValue(stalePreview());
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function project(over: Partial<api.CodingProject> = {}): api.CodingProject {
  return {
    id: "p",
    northStar: "",
    definitionOfDone: "",
    target: "existing",
    status: "active",
    revision: 1,
    importSource: { kind: "github_clone", originUrl: "https://github.com/o/r", clonedRef: "main@abc" },
    workRequest: "",
    ...over,
  } as api.CodingProject;
}

function proposal(over = {}): api.NorthStarProposal {
  return {
    northStar: "Ship the widget",
    definitionOfDone: "green tests",
    summary: "a widget",
    detectedStack: ["python"],
    suggestedFirstTasks: ["add tests"],
    sourceRefs: ["README.md"],
    model: "local.qwen",
    lowSignal: false,
    accepted: false,
    ...over,
  };
}

describe("OnboardingPanel", () => {
  it("shows import provenance", async () => {
    getNorthStarProposal.mockResolvedValue(null);
    render(<OnboardingPanel project={project()} onChanged={vi.fn()} onError={vi.fn()} />);
    await waitFor(() => expect(getNorthStarProposal).toHaveBeenCalled());
    expect(screen.getByText(/Imported from/)).toBeInTheDocument();
    expect(screen.getByText(/github\.com\/o\/r/)).toBeInTheDocument();
  });

  it("runs the scan and shows the proposal, then accepts it", async () => {
    getNorthStarProposal.mockResolvedValueOnce(null).mockResolvedValueOnce(proposal());
    startOrientationScan.mockResolvedValue({ jobId: "j", status: "scanning", projectId: null });
    orientationScanStatus.mockResolvedValue({ jobId: "j", status: "done", projectId: null });
    acceptNorthStarProposal.mockResolvedValue(project({ northStar: "Ship the widget" }));
    const onChanged = vi.fn();
    render(<OnboardingPanel project={project()} onChanged={onChanged} onError={vi.fn()} />);
    await waitFor(() => expect(getNorthStarProposal).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "Understand this project" }));
    await waitFor(() => expect(screen.getByText("Ship the widget")).toBeInTheDocument(), {
      timeout: 3000,
    });

    fireEvent.click(screen.getByRole("button", { name: "Accept as North Star" }));
    await waitFor(() => expect(acceptNorthStarProposal).toHaveBeenCalledWith("p"));
    expect(onChanged).toHaveBeenCalled();
  });

  it("embeds the Current Focus panel", async () => {
    getNorthStarProposal.mockResolvedValue(null);
    render(<OnboardingPanel project={project()} onChanged={vi.fn()} onError={vi.fn()} />);
    await waitFor(() => expect(api.listFocuses).toHaveBeenCalledWith("p", "active"));
    // F141 WS-I: the panel is a collapsible .coding-panel now (summary text).
    expect(screen.getByText("Current Focus")).toBeInTheDocument();
  });

  it("F141 WS-I: hides Current Focus and shows the North Star in the north_star phase", async () => {
    getNorthStarProposal.mockResolvedValue(null);
    render(
      <OnboardingPanel
        project={project({ phase: "north_star", northStar: "Ship it", importSource: undefined })}
        onChanged={vi.fn()}
        onError={vi.fn()}
      />,
    );
    await waitFor(() => expect(api.listFocuses).toHaveBeenCalledWith("p", "active"));
    expect(screen.getByText("Building toward")).toBeInTheDocument();
    expect(screen.queryByText("Current Focus")).toBeNull();
  });

  it("does not offer Understand once a North Star exists", async () => {
    getNorthStarProposal.mockResolvedValue(null);
    render(
      <OnboardingPanel
        project={project({ northStar: "already set" })}
        onChanged={vi.fn()}
        onError={vi.fn()}
      />,
    );
    await waitFor(() => expect(getNorthStarProposal).toHaveBeenCalled());
    expect(
      screen.queryByRole("button", { name: "Understand this project" }),
    ).not.toBeInTheDocument();
  });

  it("F138: shows a staleness badge + Refresh for an imported project", async () => {
    getNorthStarProposal.mockResolvedValue(null as unknown as api.NorthStarProposal);
    render(
      <OnboardingPanel project={project()} onChanged={vi.fn()} onError={vi.fn()} />,
    );
    await waitFor(() => expect(getRefreshPreview).toHaveBeenCalled());
    expect(screen.getByText(/snapshot is 3 behind origin\/main/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
  });

  it("F138: opens the refresh modal", async () => {
    getNorthStarProposal.mockResolvedValue(null as unknown as api.NorthStarProposal);
    render(
      <OnboardingPanel project={project()} onChanged={vi.fn()} onError={vi.fn()} />,
    );
    await waitFor(() => expect(getRefreshPreview).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText(/remote is 3 commits ahead/i)).toBeInTheDocument();
  });

  it("F138: no refresh badge for a non-imported project", async () => {
    getNorthStarProposal.mockResolvedValue(null as unknown as api.NorthStarProposal);
    render(
      <OnboardingPanel
        project={project({ importSource: undefined })}
        onChanged={vi.fn()}
        onError={vi.fn()}
      />,
    );
    await waitFor(() => expect(getNorthStarProposal).toHaveBeenCalled());
    expect(getRefreshPreview).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Refresh" })).not.toBeInTheDocument();
  });
});
