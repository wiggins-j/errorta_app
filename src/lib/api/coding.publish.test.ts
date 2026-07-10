import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", () => ({ sidecarFetch: vi.fn() }));
import { sidecarFetch } from "../api";
import {
  PublishBlocked,
  getPublishAuthStatus,
  getPublishEvents,
  getPublishTargets,
  getProject,
  publishExistingRepoPr,
  publishNewGithubRepo,
} from "./coding";

const mockFetch = sidecarFetch as unknown as ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

afterEach(() => vi.clearAllMocks());

describe("coding publish P3/P4 API", () => {
  it("getProject surfaces delivered / deliveredAt", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        project: { id: "p", north_star: "", delivered: true, delivered_at: "2026-06-22T00:00:00Z" },
      }),
    );
    const proj = await getProject("p");
    expect(proj.delivered).toBe(true);
    expect(proj.deliveredAt).toBe("2026-06-22T00:00:00Z");
  });

  it("publish read calls include the Tauri origin header", async () => {
    mockFetch
      .mockResolvedValueOnce(jsonResponse({
        gh_present: true,
        token_in_keychain: false,
        login: "octocat",
      }))
      .mockResolvedValueOnce(jsonResponse({
        events: [{ event_id: "e1", kind: "manual_export", state: "committed" }],
      }))
      .mockResolvedValueOnce(jsonResponse({
        targets: [{ target_id: "t1", kind: "manual_export" }],
      }));

    await getPublishAuthStatus("p");
    await getPublishEvents("p");
    await getPublishTargets("p");

    for (const [, init] of mockFetch.mock.calls) {
      expect(((init as RequestInit).headers as Record<string, string>)["x-errorta-origin"])
        .toBe("tauri-ui");
    }
  });

  it("publishExistingRepoPr posts override + maps the success body", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        branch: "errorta/p",
        base: "main",
        commit_sha: "abc",
        pr_url: "https://github.com/x/y/pull/3",
        events: [{ event_id: "e1", kind: "existing_repo_pr", state: "pr_opened" }],
      }),
    );
    const res = await publishExistingRepoPr("p", { override: false });
    expect(mockFetch.mock.calls[0][0]).toBe("/coding/projects/p/publish/existing-repo-pr");
    const init = mockFetch.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["x-errorta-origin"]).toBe("tauri-ui");
    expect(JSON.parse(init.body as string)).toEqual({ override: false });
    expect(res.prUrl).toBe("https://github.com/x/y/pull/3");
    expect(res.events[0].state).toBe("pr_opened");
  });

  it("maps a secret_scan_hit 409 into a typed PublishBlocked with findings", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(
        {
          detail: {
            error: "secret_scan_hit",
            detail: {
              clean: false,
              findings: [
                { path: ".env", kind: "sensitive_path", line: null, redacted_excerpt: "" },
              ],
            },
          },
        },
        409,
      ),
    );
    const err = await publishExistingRepoPr("p").catch((e) => e);
    expect(err).toBeInstanceOf(PublishBlocked);
    expect(err.reason).toBe("secret_scan_hit");
    expect(err.findings).toEqual([
      { path: ".env", kind: "sensitive_path", line: null, redactedExcerpt: "" },
    ]);
  });

  it("maps a clobber_unrelated_changes 409 into dirtyPaths", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(
        {
          detail: {
            error: "clobber_unrelated_changes",
            detail: { unrelated_paths: ["notes.txt", "x.md"] },
          },
        },
        409,
      ),
    );
    const err = await publishExistingRepoPr("p").catch((e) => e);
    expect(err).toBeInstanceOf(PublishBlocked);
    expect(err.reason).toBe("clobber_unrelated_changes");
    expect(err.dirtyPaths).toEqual(["notes.txt", "x.md"]);
  });

  it("maps a plain reason 409 (no_origin) into PublishBlocked", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ detail: { error: "no_origin" } }, 409),
    );
    const err = await publishExistingRepoPr("p").catch((e) => e);
    expect(err).toBeInstanceOf(PublishBlocked);
    expect(err.reason).toBe("no_origin");
    expect(err.findings).toBeNull();
    expect(err.dirtyPaths).toBeNull();
  });

  it("publishNewGithubRepo posts {repo_name, private, local_only, override} + maps the result", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        local_only: false,
        repo_url: "https://github.com/octocat/my-project",
        private: true,
        commit_sha: "abc",
        initial_files: ["README.md", "src/index.ts"],
        events: [],
      }),
    );
    const res = await publishNewGithubRepo("p", { repoName: "my-project" });
    expect(mockFetch.mock.calls[0][0]).toBe("/coding/projects/p/publish/new-github-repo");
    expect(JSON.parse((mockFetch.mock.calls[0][1] as RequestInit).body as string)).toEqual({
      repo_name: "my-project",
      private: true,
      local_only: false,
      override: false,
    });
    expect(res.repoUrl).toBe("https://github.com/octocat/my-project");
    expect(res.fileList).toEqual(["README.md", "src/index.ts"]);
  });

  it("publishNewGithubRepo maps the local_only result (localPath, no repoUrl)", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        local_only: true,
        local_path: "/home/u/p-git-repo",
        commit_sha: "abc",
        initial_files: ["README.md"],
        events: [],
      }),
    );
    const res = await publishNewGithubRepo("p", {
      repoName: "my-project",
      private: false,
      localOnly: true,
    });
    expect(JSON.parse((mockFetch.mock.calls[0][1] as RequestInit).body as string)).toMatchObject({
      private: false,
      local_only: true,
    });
    expect(res.localOnly).toBe(true);
    expect(res.localPath).toBe("/home/u/p-git-repo");
    expect(res.repoUrl).toBeNull();
  });

  it("maps invalid_repo_name 422 into PublishBlocked", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ detail: { error: "invalid_repo_name" } }, 422),
    );
    const err = await publishNewGithubRepo("p", { repoName: "--bad" }).catch((e) => e);
    expect(err).toBeInstanceOf(PublishBlocked);
    expect(err.reason).toBe("invalid_repo_name");
    expect(err.status).toBe(422);
  });
});
