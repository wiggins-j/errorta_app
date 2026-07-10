import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", () => ({ sidecarFetch: vi.fn() }));
import { sidecarFetch } from "../api";
import { CodingFileUpdateError, getArtifacts, getFile, updateFile } from "./coding";

const mockFetch = sidecarFetch as unknown as ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

afterEach(() => vi.clearAllMocks());

describe("coding file API", () => {
  it("maps artifact on_master flags", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        artifacts: [
          { path: "src/core.py", status: "created", summary: "core", on_master: true },
          { path: "src/future.py", status: "created", summary: "", on_master: false },
        ],
      }),
    );

    const artifacts = await getArtifacts("proj");

    expect(artifacts[0].onMaster).toBe(true);
    expect(artifacts[1].onMaster).toBe(false);
  });

  it("fetches and maps a master file", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        path: "src/core.py",
        content: "print('ok')\n",
        truncated: true,
        encoding: "utf-8",
        bytes: 300000,
        on_master: true,
      }),
    );

    const file = await getFile("proj", "src/core.py");

    expect(mockFetch.mock.calls[0][0]).toBe("/coding/projects/proj/files?path=src%2Fcore.py");
    expect((mockFetch.mock.calls[0][1] as RequestInit).headers).toEqual({
      "x-errorta-origin": "tauri-ui",
    });
    expect(file).toEqual({
      path: "src/core.py",
      content: "print('ok')\n",
      truncated: true,
      encoding: "utf-8",
      bytes: 300000,
      onMaster: true,
      // F105: absent content_sha256 in the response maps to null.
      contentSha256: null,
    });
  });

  it("maps content_sha256 when present", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        path: "src/core.py",
        content: "print('ok')\n",
        truncated: false,
        encoding: "utf-8",
        bytes: 12,
        on_master: true,
        content_sha256: "f".repeat(64),
      }),
    );

    const file = await getFile("proj", "src/core.py");
    expect(file.contentSha256).toBe("f".repeat(64));
  });

  it("maps binary files without content", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        path: "bin/data",
        content: null,
        truncated: false,
        encoding: "binary",
        bytes: 7,
        on_master: true,
      }),
    );

    const file = await getFile("proj", "bin/data");

    expect(file.encoding).toBe("binary");
    expect(file.content).toBeNull();
  });

  it("maps not_on_master 404 into a sentinel file", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ detail: { reason: "not_on_master" } }, 404),
    );

    const file = await getFile("proj", "src/future.py");

    expect(file).toEqual({
      path: "src/future.py",
      content: null,
      truncated: false,
      encoding: "utf-8",
      bytes: 0,
      onMaster: false,
      contentSha256: null,
    });
  });

  it("PUTs a file save with the expected sha and tauri origin", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        path: "src/core.py",
        content_sha256: "b".repeat(64),
        bytes: 13,
        head: "deadbeef",
        on_master: true,
      }),
    );

    const res = await updateFile("proj", "src/core.py", "new\n", "a".repeat(64));

    expect(mockFetch.mock.calls[0][0]).toBe("/coding/projects/proj/files?path=src%2Fcore.py");
    const init = mockFetch.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("PUT");
    expect((init.headers as Record<string, string>)["x-errorta-origin"]).toBe("tauri-ui");
    expect(JSON.parse(init.body as string)).toEqual({
      content: "new\n",
      expected_sha256: "a".repeat(64),
    });
    expect(res.contentSha256).toBe("b".repeat(64));
    expect(res.head).toBe("deadbeef");
  });

  it("throws a typed stale_file error on a 409", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(
        { detail: { reason: "stale_file", content_sha256: "c".repeat(64) } },
        409,
      ),
    );

    await expect(updateFile("proj", "src/core.py", "x\n", "a".repeat(64))).rejects.toMatchObject({
      reason: "stale_file",
      currentSha256: "c".repeat(64),
    });
  });

  it("throws a typed run_active error on a 409", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ detail: { reason: "run_active" } }, 409));

    const err = await updateFile("proj", "src/core.py", "x\n", "a".repeat(64)).catch((e) => e);
    expect(err).toBeInstanceOf(CodingFileUpdateError);
    expect(err.reason).toBe("run_active");
  });
});
