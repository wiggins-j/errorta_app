// resumeRun error mapping — the workspace-integrity 409 must surface as a typed
// error the shell can recover from (fresh start), not a generic dead-end.
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", () => ({ sidecarFetch: vi.fn() }));
import { sidecarFetch } from "../api";
import { resumeRun, RunWorkspaceIntegrityError } from "./coding";

const mockFetch = sidecarFetch as unknown as ReturnType<typeof vi.fn>;

function response(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

afterEach(() => vi.clearAllMocks());

describe("resumeRun", () => {
  it("throws RunWorkspaceIntegrityError on the bare-string 409 detail", async () => {
    mockFetch.mockResolvedValueOnce(
      response(409, { detail: "workspace_integrity_failed" }),
    );
    await expect(resumeRun("proj")).rejects.toBeInstanceOf(RunWorkspaceIntegrityError);
  });

  it("surfaces a structured detail message for other failures", async () => {
    mockFetch.mockResolvedValueOnce(
      response(400, { detail: { code: "run_config_missing", message: "No saved team." } }),
    );
    await expect(resumeRun("proj")).rejects.toThrow("No saved team.");
  });

  it("returns started on success", async () => {
    mockFetch.mockResolvedValueOnce(response(200, { started: true, resumed: true }));
    await expect(resumeRun("proj")).resolves.toBe(true);
  });
});
