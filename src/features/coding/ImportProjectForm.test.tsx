import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/coding", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/coding")>();
  return {
    ...actual,
    importLocalProject: vi.fn(),
    importGithubAuthStatus: vi.fn(),
    importGithubClone: vi.fn(),
    importGithubCloneStatus: vi.fn(),
    importGithubBranches: vi.fn(),
  };
});

vi.mock("../shell/FilePickerDialog", () => ({
  pickPaths: vi.fn(async () => ["/picked/folder"]),
}));

import * as api from "../../lib/api/coding";
import ImportProjectForm from "./ImportProjectForm";

const importLocalProject = vi.mocked(api.importLocalProject);
const importGithubAuthStatus = vi.mocked(api.importGithubAuthStatus);
const importGithubClone = vi.mocked(api.importGithubClone);
const importGithubCloneStatus = vi.mocked(api.importGithubCloneStatus);
const importGithubBranches = vi.mocked(api.importGithubBranches);

beforeEach(() => {
  // default: branch listing unavailable -> free-text fallback
  importGithubBranches.mockResolvedValue({
    ok: false,
    branches: [],
    defaultBranch: null,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

// WS-B: source is a <select> now — switch to the GitHub source set.
function selectGithubSource() {
  fireEvent.change(screen.getByLabelText("Import source"), {
    target: { value: "github" },
  });
}

function project(over = {}) {
  return {
    id: "p",
    northStar: "",
    definitionOfDone: "",
    target: "existing",
    status: "active",
    revision: 1,
    ...over,
  } as api.CodingProject;
}

describe("ImportProjectForm — local", () => {
  it("imports a local folder and calls onCreated", async () => {
    importLocalProject.mockResolvedValue(project({ id: "mine" }));
    const onCreated = vi.fn();
    render(<ImportProjectForm onCreated={onCreated} onError={vi.fn()} />);

    fireEvent.change(screen.getByLabelText("Import project id"), {
      target: { value: "mine" },
    });
    fireEvent.change(screen.getByLabelText("Folder path"), {
      target: { value: "/repo" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Import project" }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("mine"));
    expect(importLocalProject).toHaveBeenCalledWith(
      expect.objectContaining({ projectId: "mine", folderPath: "/repo" }),
    );
  });

  it("Browse fills the folder field", async () => {
    render(<ImportProjectForm onCreated={vi.fn()} onError={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Browse for folder" }));
    await waitFor(() =>
      expect((screen.getByLabelText("Folder path") as HTMLInputElement).value).toBe(
        "/picked/folder",
      ),
    );
  });
});

describe("ImportProjectForm — github", () => {
  it("disables import until gh is connected", async () => {
    importGithubAuthStatus.mockResolvedValue({ ghPresent: false, login: null });
    render(<ImportProjectForm onCreated={vi.fn()} onError={vi.fn()} />);
    selectGithubSource();
    await waitFor(() => expect(importGithubAuthStatus).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "Import project" })).toBeDisabled();
    expect(screen.getByText(/Connect GitHub/)).toBeInTheDocument();
  });

  it("clones and polls to completion", async () => {
    importGithubAuthStatus.mockResolvedValue({ ghPresent: true, login: "octocat" });
    importGithubClone.mockResolvedValue({ jobId: "j1", status: "cloning", projectId: null });
    importGithubCloneStatus.mockResolvedValue({
      jobId: "j1",
      status: "done",
      projectId: "cloned",
    });
    const onCreated = vi.fn();
    render(<ImportProjectForm onCreated={onCreated} onError={vi.fn()} />);
    selectGithubSource();
    await waitFor(() => expect(importGithubAuthStatus).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText("Import project id"), {
      target: { value: "cloned" },
    });
    fireEvent.change(screen.getByLabelText("Repository URL"), {
      target: { value: "https://github.com/octocat/hello" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Import project" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("cloned"), { timeout: 3000 });
  });

  it("populates a branch dropdown from the remote, default preselected", async () => {
    importGithubAuthStatus.mockResolvedValue({ ghPresent: true, login: "octocat" });
    importGithubBranches.mockResolvedValue({
      ok: true,
      branches: ["main", "dev", "release"],
      defaultBranch: "dev",
    });
    render(<ImportProjectForm onCreated={vi.fn()} onError={vi.fn()} />);
    selectGithubSource();
    fireEvent.change(screen.getByLabelText("Repository URL"), {
      target: { value: "https://github.com/octocat/hello" },
    });
    await waitFor(
      () => expect(importGithubBranches).toHaveBeenCalledWith(
        "https://github.com/octocat/hello",
      ),
      { timeout: 2000 },
    );
    const branch = (await screen.findByLabelText("Branch")) as HTMLSelectElement;
    expect(branch.tagName).toBe("SELECT");
    await waitFor(() => expect(branch.value).toBe("dev"));
    expect(
      Array.from(branch.options).map((o) => o.value),
    ).toEqual(["main", "dev", "release"]);
  });

  it("falls back to a free-text branch field when listing fails", async () => {
    importGithubAuthStatus.mockResolvedValue({ ghPresent: true, login: "octocat" });
    importGithubBranches.mockResolvedValue({
      ok: false,
      branches: [],
      defaultBranch: null,
    });
    render(<ImportProjectForm onCreated={vi.fn()} onError={vi.fn()} />);
    selectGithubSource();
    fireEvent.change(screen.getByLabelText("Repository URL"), {
      target: { value: "https://github.com/octocat/hello" },
    });
    await waitFor(() => expect(importGithubBranches).toHaveBeenCalled(), {
      timeout: 2000,
    });
    const branch = (await screen.findByLabelText("Branch")) as HTMLInputElement;
    expect(branch.tagName).toBe("INPUT");
    expect(screen.getByText(/Couldn't list branches/)).toBeInTheDocument();
  });
});
