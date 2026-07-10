import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", () => ({
  deleteJSON: vi.fn(),
  getJSON: vi.fn(),
  postJSON: vi.fn(),
  sidecarFetch: vi.fn(),
}));
import { sidecarFetch } from "../api";
import {
  createProject,
  getCorpusBinding,
  getGroundingCapabilities,
  listGroundingCorpora,
  putCorpusBinding,
} from "./coding";

const mockFetch = sidecarFetch as unknown as ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown) {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

afterEach(() => vi.clearAllMocks());

describe("grounding API", () => {
  it("lists corpus summaries", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        corpora: [
          { name: "alpha", file_count: 2, ready_count: 1, status: "ready", source: "local" },
        ],
        source: "local",
      }),
    );

    const corpora = await listGroundingCorpora();

    expect(corpora[0]).toEqual({ name: "alpha", fileCount: 2, readyCount: 1 });
    expect(mockFetch).toHaveBeenCalledWith("/corpora");
  });

  it("serializes create-project grounding payloads", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        project: {
          id: "p",
          north_star: "n",
          target: "existing",
          status: "active",
          revision: 1,
          grounding: { mode: "build_from_repo", corpus_id: "p-project" },
        },
      }),
    );

    await createProject({
      projectId: "p",
      northStar: "n",
      target: "existing",
      repoPath: "/repo",
      grounding: { mode: "build_from_repo", corpusId: "p-project", sourceRoot: "/repo" },
    });

    const body = JSON.parse((mockFetch.mock.calls[0][1] as RequestInit).body as string);
    expect(body.grounding).toEqual({
      mode: "build_from_repo",
      corpus_id: "p-project",
      source_root: "/repo",
    });
  });

  it("adapts capability and binding responses", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        capabilities: {
          available: true,
          version: "0.2.0",
          source: "installed",
          supports_corpus_ids: true,
          supports_file_ingest: true,
          supports_record_ingest: false,
          supports_metadata_filters: false,
          supports_provenance_metadata: true,
          supports_incremental_refresh: false,
          supports_supersession: false,
          supports_export_import: false,
          local_only_embedding: true,
          notes: ["fallback"],
        },
      }),
    );
    const caps = await getGroundingCapabilities("p");
    expect(caps.supportsCorpusIds).toBe(true);
    expect(caps.notes).toEqual(["fallback"]);

    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        binding: {
          project_id: "p",
          mode: "existing",
          corpus_id: "alpha",
          source_root: null,
          index_version: 1,
          health_state: "ready",
          health_reason: "1 ready files",
        },
      }),
    );
    const binding = await getCorpusBinding("p");
    expect(binding.corpusId).toBe("alpha");
    expect(binding.healthState).toBe("ready");
  });

  it("posts binding updates with tauri origin header", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ binding: { project_id: "p", mode: "none", health_state: "missing" } }),
    );

    await putCorpusBinding("p", { mode: "none" });

    expect(mockFetch.mock.calls[0][0]).toBe("/coding/projects/p/grounding/corpus-binding");
    expect((mockFetch.mock.calls[0][1] as RequestInit).headers).toEqual({
      "x-errorta-origin": "tauri-ui",
    });
  });
});
