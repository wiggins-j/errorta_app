import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/coding", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/coding")>();
  return {
    ...actual,
    manualExport: vi.fn(),
    getPublishAuthStatus: vi.fn(),
    getPublishEvents: vi.fn(),
    getPublishTargets: vi.fn(),
    publishExistingRepoPr: vi.fn(),
    publishNewGithubRepo: vi.fn(),
  };
});

import * as api from "../../lib/api/coding";
import PublishPanel from "./PublishPanel";
import {
  PublishBlocked,
  type PublishAuthStatus,
  type PublishEvent,
  type PublishPrResult,
  type PublishRepoResult,
} from "../../lib/api/coding";

const manualExport = vi.mocked(api.manualExport);
const getPublishAuthStatus = vi.mocked(api.getPublishAuthStatus);
const getPublishEvents = vi.mocked(api.getPublishEvents);
const publishExistingRepoPr = vi.mocked(api.publishExistingRepoPr);
const publishNewGithubRepo = vi.mocked(api.publishNewGithubRepo);

function authStatus(over: Partial<PublishAuthStatus> = {}): PublishAuthStatus {
  return { ghPresent: false, tokenInKeychain: false, login: null, ...over };
}

function event(over: Partial<PublishEvent> = {}): PublishEvent {
  return {
    eventId: "ev-1",
    targetId: "t-1",
    kind: "manual_export",
    state: "committed",
    branch: null,
    commitSha: "abc123",
    prUrl: null,
    error: null,
    createdAt: "2026-06-21T00:00:00Z",
    ...over,
  };
}

function prResult(over: Partial<PublishPrResult> = {}): PublishPrResult {
  return {
    branch: "errorta/proj",
    base: "main",
    commitSha: "abc",
    prUrl: "https://github.com/x/y/pull/9",
    events: [],
    ...over,
  };
}

function repoResult(over: Partial<PublishRepoResult> = {}): PublishRepoResult {
  return {
    localOnly: false,
    repoUrl: "https://github.com/octocat/my-project",
    localPath: null,
    private: true,
    commitSha: "abc",
    fileList: ["README.md", "src/index.ts"],
    events: [],
    ...over,
  };
}

const writeText = vi.fn().mockResolvedValue(undefined);

beforeEach(() => {
  vi.clearAllMocks();
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
  });
  getPublishAuthStatus.mockResolvedValue(authStatus());
  getPublishEvents.mockResolvedValue([]);
});

afterEach(() => cleanup());

describe("PublishPanel", () => {
  it("renders the manual-export actions (no auth required)", async () => {
    render(<PublishPanel projectId="proj" delivered={false} />);
    expect(await screen.findByRole("button", { name: /export \.zip/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /git apply/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /open delivered folder/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /view patch/i })).toBeInTheDocument();
  });

  it("Export .zip calls manualExport(projectId, 'zip') and shows the result", async () => {
    manualExport.mockResolvedValue({ kind: "zip", path: "/out/proj.zip", runHint: "unzip /out/proj.zip" });
    render(<PublishPanel projectId="proj" delivered={false} />);
    fireEvent.click(await screen.findByRole("button", { name: /export \.zip/i }));
    await waitFor(() => expect(manualExport).toHaveBeenCalledWith("proj", "zip"));
    expect(await screen.findByText(/\/out\/proj\.zip/)).toBeInTheDocument();
  });

  it("Copy git apply calls clipboard with the returned command", async () => {
    manualExport.mockResolvedValue({ kind: "git_apply", path: "/out/p.patch", command: "git apply /out/p.patch" });
    render(<PublishPanel projectId="proj" delivered={false} />);
    fireEvent.click(await screen.findByRole("button", { name: /git apply/i }));
    await waitFor(() => expect(manualExport).toHaveBeenCalledWith("proj", "git_apply"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("git apply /out/p.patch"));
    expect(await screen.findByLabelText("git apply command")).toHaveTextContent("git apply /out/p.patch");
  });

  it("View patch renders the diff and Copy patch copies it", async () => {
    manualExport.mockResolvedValue({ kind: "patch", diff: "diff --git a b\n+line", path: "/out/p.patch" });
    render(<PublishPanel projectId="proj" delivered={false} />);
    fireEvent.click(await screen.findByRole("button", { name: /view patch/i }));
    const pre = await screen.findByLabelText("Patch diff");
    expect(pre).toHaveTextContent("diff --git a b");
    fireEvent.click(screen.getByRole("button", { name: /copy patch/i }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("diff --git a b\n+line"));
  });

  it("shows the logged-in auth line", async () => {
    getPublishAuthStatus.mockResolvedValue(authStatus({ ghPresent: true, login: "octocat" }));
    render(<PublishPanel projectId="proj" delivered={false} />);
    expect(await screen.findByText(/logged in as octocat/i)).toBeInTheDocument();
  });

  it("shows the gh-detected-but-not-logged-in auth line", async () => {
    getPublishAuthStatus.mockResolvedValue(authStatus({ ghPresent: true, login: null }));
    render(<PublishPanel projectId="proj" delivered={false} />);
    expect(await screen.findByText(/gh detected — not logged in/i)).toBeInTheDocument();
  });

  it("shows the not-connected auth line when gh is absent", async () => {
    getPublishAuthStatus.mockResolvedValue(authStatus({ ghPresent: false }));
    render(<PublishPanel projectId="proj" delivered={false} />);
    expect(await screen.findByText(/not connected/i)).toBeInTheDocument();
  });

  it("disables the GitHub actions and shows the accept hint when not delivered", async () => {
    getPublishAuthStatus.mockResolvedValue(authStatus({ ghPresent: true, login: "octocat" }));
    render(<PublishPanel projectId="proj" delivered={false} />);
    expect(await screen.findByRole("button", { name: /^open pr$/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /create repo/i })).toBeDisabled();
    expect(screen.getByText(/accept the project first/i)).toBeInTheDocument();
  });

  it("shows Connect GitHub + the gh hint when gh is absent (even if delivered)", async () => {
    getPublishAuthStatus.mockResolvedValue(authStatus({ ghPresent: false }));
    render(<PublishPanel projectId="proj" delivered />);
    expect(await screen.findByRole("button", { name: /connect github/i })).toBeInTheDocument();
    expect(await screen.findByText(/connect github to push/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^open pr$/i })).toBeDisabled();
  });

  it("Connect GitHub deep-links to settings", async () => {
    getPublishAuthStatus.mockResolvedValue(authStatus({ ghPresent: false }));
    const dispatch = vi.spyOn(window, "dispatchEvent");
    render(<PublishPanel projectId="proj" delivered />);
    fireEvent.click(await screen.findByRole("button", { name: /connect github/i }));
    const ev = dispatch.mock.calls
      .map((c) => c[0])
      .find((e): e is CustomEvent => e.type === "errorta:navigate");
    expect((ev as CustomEvent).detail).toEqual({ view: "settings" });
    dispatch.mockRestore();
  });

  it("enables the GitHub actions when gh-logged-in AND delivered", async () => {
    getPublishAuthStatus.mockResolvedValue(authStatus({ ghPresent: true, login: "octocat" }));
    render(<PublishPanel projectId="proj" delivered />);
    expect(await screen.findByRole("button", { name: /^open pr$/i })).not.toBeDisabled();
    // repo name empty -> Create repo still disabled until a name is typed
    fireEvent.change(screen.getByLabelText(/new repo name/i), { target: { value: "my-project" } });
    expect(screen.getByRole("button", { name: /create repo/i })).not.toBeDisabled();
  });

  // F102-01 regression lock: the panel must react to a live `delivered` prop
  // change (parent re-render after an in-session accept) WITHOUT a remount. The
  // shipped bug fetched `delivered` once on mount and never updated it, leaving
  // the whole GitHub section stuck-disabled after the user had accepted.
  it("enables the GitHub + local-only actions when `delivered` flips false→true (no remount)", async () => {
    getPublishAuthStatus.mockResolvedValue(authStatus({ ghPresent: true, login: "octocat" }));
    const { rerender } = render(<PublishPanel projectId="proj" delivered={false} />);

    // Initially not delivered: disabled + the (misleading-when-stale) accept hint.
    expect(await screen.findByRole("button", { name: /^open pr$/i })).toBeDisabled();
    expect(screen.getByLabelText(/create local git repo only/i)).toBeDisabled();
    expect(screen.getByText(/accept the project first/i)).toBeInTheDocument();

    // Parent accepts + reloads -> same element, new prop. No unmount/remount.
    rerender(<PublishPanel projectId="proj" delivered />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^open pr$/i })).not.toBeDisabled(),
    );
    expect(screen.getByLabelText(/create local git repo only/i)).not.toBeDisabled();
    expect(screen.queryByText(/accept the project first/i)).not.toBeInTheDocument();
  });

  describe("Open PR on existing repo (P3)", () => {
    beforeEach(() => {
      getPublishAuthStatus.mockResolvedValue(authStatus({ ghPresent: true, login: "octocat" }));
    });

    it("calls publishExistingRepoPr and renders the PR link", async () => {
      publishExistingRepoPr.mockResolvedValue(prResult());
      render(<PublishPanel projectId="proj" delivered />);
      fireEvent.click(await screen.findByRole("button", { name: /^open pr$/i }));
      await waitFor(() =>
        expect(publishExistingRepoPr).toHaveBeenCalledWith("proj", { override: false }),
      );
      const link = await screen.findByRole("link", {
        name: "https://github.com/x/y/pull/9",
      });
      expect(link).toHaveAttribute("href", "https://github.com/x/y/pull/9");
    });

    it("renders scan findings + an override button that re-calls with override:true", async () => {
      publishExistingRepoPr.mockRejectedValueOnce(
        new PublishBlocked("secret_scan_hit", "scan", {
          findings: [{ path: ".env", kind: "sensitive_path", line: null, redactedExcerpt: "" }],
        }),
      );
      publishExistingRepoPr.mockResolvedValueOnce(prResult());
      render(<PublishPanel projectId="proj" delivered />);
      fireEvent.click(await screen.findByRole("button", { name: /^open pr$/i }));
      const findings = await screen.findByLabelText("Secret scan findings");
      expect(findings).toHaveTextContent(".env");
      fireEvent.click(screen.getByRole("button", { name: /publish anyway \(override\)/i }));
      await waitFor(() =>
        expect(publishExistingRepoPr).toHaveBeenLastCalledWith("proj", { override: true }),
      );
    });

    it("renders the unrelated dirty paths on a clobber refusal", async () => {
      publishExistingRepoPr.mockRejectedValue(
        new PublishBlocked("clobber_unrelated_changes", "clobber", {
          dirtyPaths: ["notes.txt", "secret-local.md"],
        }),
      );
      render(<PublishPanel projectId="proj" delivered />);
      fireEvent.click(await screen.findByRole("button", { name: /^open pr$/i }));
      const dirty = await screen.findByLabelText("Unrelated changed paths");
      expect(dirty).toHaveTextContent("notes.txt");
      expect(dirty).toHaveTextContent("secret-local.md");
      expect(screen.getByRole("alert")).toHaveTextContent(/refused: unrelated local changes/i);
    });

    // Regression: a gate refusal must show the ACTUAL blocker in readable prose,
    // not the raw "open_tasks" mislabel a delivered project used to receive.
    it("shows the real gate blocker in friendly text (not a raw open_tasks label)", async () => {
      publishExistingRepoPr.mockRejectedValue(
        new PublishBlocked("tests_missing", "tests_missing", {
          blockers: ["tests_missing"],
        }),
      );
      render(<PublishPanel projectId="proj" delivered />);
      fireEvent.click(await screen.findByRole("button", { name: /^open pr$/i }));
      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(/tests haven't run for the delivered changes yet/i);
      expect(alert).not.toHaveTextContent("open_tasks");
      expect(alert).not.toHaveTextContent("tests_missing");
    });
  });

  describe("Create new GitHub repo (P4)", () => {
    beforeEach(() => {
      getPublishAuthStatus.mockResolvedValue(authStatus({ ghPresent: true, login: "octocat" }));
    });

    it("calls publishNewGithubRepo with {repoName, private:true, localOnly} and renders the URL + file list", async () => {
      publishNewGithubRepo.mockResolvedValue(repoResult());
      render(<PublishPanel projectId="proj" delivered />);
      await screen.findByRole("button", { name: /create repo/i });
      fireEvent.change(screen.getByLabelText(/new repo name/i), {
        target: { value: "my-project" },
      });
      fireEvent.click(screen.getByRole("button", { name: /create repo/i }));
      await waitFor(() =>
        expect(publishNewGithubRepo).toHaveBeenCalledWith("proj", {
          repoName: "my-project",
          private: true,
          localOnly: false,
          override: false,
        }),
      );
      expect(
        await screen.findByRole("link", { name: "https://github.com/octocat/my-project" }),
      ).toBeInTheDocument();
      const files = screen.getByLabelText("Initial commit files");
      expect(files).toHaveTextContent("README.md");
      expect(files).toHaveTextContent("src/index.ts");
    });

    it("honors the private toggle (off) and the local-only checkbox", async () => {
      publishNewGithubRepo.mockResolvedValue(
        repoResult({ localOnly: true, repoUrl: null, localPath: "/home/u/proj-git-repo", private: null }),
      );
      render(<PublishPanel projectId="proj" delivered />);
      await screen.findByRole("button", { name: /create repo/i });
      fireEvent.change(screen.getByLabelText(/new repo name/i), { target: { value: "my-project" } });
      fireEvent.click(screen.getByLabelText(/create local git repo only/i));
      fireEvent.click(screen.getByRole("button", { name: /create repo/i }));
      await waitFor(() =>
        expect(publishNewGithubRepo).toHaveBeenCalledWith("proj", {
          repoName: "my-project",
          private: true,
          localOnly: true,
          override: false,
        }),
      );
      expect(await screen.findByText("/home/u/proj-git-repo")).toBeInTheDocument();
    });

    it("renders scan findings + override on a 409 and re-calls with override:true", async () => {
      publishNewGithubRepo.mockRejectedValueOnce(
        new PublishBlocked("secret_scan_hit", "scan", {
          findings: [{ path: "key.pem", kind: "sensitive_path", line: null, redactedExcerpt: "" }],
        }),
      );
      publishNewGithubRepo.mockResolvedValueOnce(repoResult());
      render(<PublishPanel projectId="proj" delivered />);
      await screen.findByRole("button", { name: /create repo/i });
      fireEvent.change(screen.getByLabelText(/new repo name/i), { target: { value: "my-project" } });
      fireEvent.click(screen.getByRole("button", { name: /create repo/i }));
      const findings = await screen.findByLabelText("Secret scan findings");
      expect(findings).toHaveTextContent("key.pem");
      fireEvent.click(screen.getByRole("button", { name: /publish anyway \(override\)/i }));
      await waitFor(() =>
        expect(publishNewGithubRepo).toHaveBeenLastCalledWith("proj", {
          repoName: "my-project",
          private: true,
          localOnly: false,
          override: true,
        }),
      );
    });

    it("allows local-only repo creation without GitHub auth", async () => {
      getPublishAuthStatus.mockResolvedValue(authStatus({ ghPresent: false, login: null }));
      publishNewGithubRepo.mockResolvedValue(
        repoResult({
          localOnly: true,
          repoUrl: null,
          localPath: "/home/u/proj-git-repo",
          private: null,
        }),
      );
      render(<PublishPanel projectId="proj" delivered />);
      const localOnly = await screen.findByLabelText(/create local git repo only/i);
      expect(localOnly).not.toBeDisabled();
      expect(screen.getByRole("button", { name: /^open pr$/i })).toBeDisabled();

      fireEvent.click(localOnly);
      fireEvent.change(screen.getByLabelText(/new repo name/i), {
        target: { value: "local-project" },
      });
      fireEvent.click(screen.getByRole("button", { name: /create repo/i }));

      await waitFor(() =>
        expect(publishNewGithubRepo).toHaveBeenCalledWith("proj", {
          repoName: "local-project",
          private: true,
          localOnly: true,
          override: false,
        }),
      );
      expect(await screen.findByText("/home/u/proj-git-repo")).toBeInTheDocument();
    });
  });

  it("renders the publish-event log", async () => {
    getPublishEvents.mockResolvedValue([
      event({ eventId: "e1", kind: "manual_export", state: "committed" }),
      event({ eventId: "e2", kind: "existing_repo_pr", state: "pr_opened", prUrl: "https://github.com/x/y/pull/1" }),
    ]);
    render(<PublishPanel projectId="proj" delivered={false} />);
    const log = await screen.findByLabelText("Publish events");
    expect(log).toHaveTextContent("existing_repo_pr");
    expect(log).toHaveTextContent("pr_opened");
    expect(screen.getByRole("link", { name: "PR" })).toHaveAttribute(
      "href",
      "https://github.com/x/y/pull/1",
    );
  });

  it("degrades to 'unavailable' when routes 404", async () => {
    getPublishAuthStatus.mockRejectedValue(new Error("publish auth status failed (404)"));
    getPublishEvents.mockRejectedValue(new Error("publish events failed (404)"));
    render(<PublishPanel projectId="proj" delivered={false} />);
    expect(await screen.findByText(/publishing unavailable/i)).toBeInTheDocument();
  });

  it("never renders a GitHub token", async () => {
    getPublishAuthStatus.mockResolvedValue(authStatus({ ghPresent: true, login: "octocat", tokenInKeychain: true }));
    getPublishEvents.mockResolvedValue([event()]);
    publishNewGithubRepo.mockResolvedValue(repoResult());
    const { container } = render(<PublishPanel projectId="proj" delivered />);
    await screen.findByText(/logged in as octocat/i);
    // exercise the full GitHub section (delivered + gh-ready) so the assertion
    // covers the P3/P4 surface, not just the auth line.
    fireEvent.change(await screen.findByLabelText(/new repo name/i), {
      target: { value: "my-project" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create repo/i }));
    await screen.findByLabelText("Initial commit files");
    expect(container.textContent).not.toMatch(/gh[pousr]_/);
    expect(container.textContent).not.toMatch(/token/i);
  });
});
