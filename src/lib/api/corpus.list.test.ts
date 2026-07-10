import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", () => ({
  deleteJSON: vi.fn(),
  getJSON: vi.fn(),
  postJSON: vi.fn(),
  sidecarFetch: vi.fn(),
}));

import { sidecarFetch } from "../api";
import { listCorpora } from "./corpus";

const mockFetch = sidecarFetch as unknown as ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown) {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

afterEach(() => vi.clearAllMocks());

describe("corpus catalog API", () => {
  it("adapts the frozen GET /corpora response shape", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        corpora: [
          {
            name: "discord-personas",
            file_count: 0,
            ready_count: 3737,
            status: "ready",
            source: "remote",
          },
        ],
        source: "remote",
      }),
    );

    await expect(listCorpora()).resolves.toEqual([
      {
        name: "discord-personas",
        fileCount: 0,
        readyCount: 3737,
        status: "ready",
        source: "remote",
        unit: "chunks",
        capabilities: {
          list_files: false,
          upload_files: false,
          folder_watch: false,
          refresh_preview: false,
          remote_ingest: false,
        },
      },
    ]);
    expect(mockFetch).toHaveBeenCalledWith("/corpora");
  });

  it("uses the top-level source when a corpus omits source", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        corpora: [{ name: "welcome", file_count: 3, ready_count: 3, status: "ready" }],
        source: "local",
      }),
    );

    const corpora = await listCorpora();

    expect(corpora[0].source).toBe("local");
    expect(corpora[0].unit).toBe("files");
    expect(corpora[0].capabilities?.upload_files).toBe(true);
  });
});
