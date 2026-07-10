import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", () => ({
  sidecarFetch: vi.fn(),
}));

import { sidecarFetch } from "../api";
import { meta, prompt } from "./services";

const mockFetch = sidecarFetch as unknown as ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => vi.clearAllMocks());

describe("service API client", () => {
  it("sends Service API tokens on prompt requests", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        id: "prompt-1",
        answer: "grounded answer",
        verdict: null,
        citations: [],
        judge_model: null,
        latency_ms: 12.5,
      }),
    );

    await expect(
      prompt("ert_abc", {
        prompt: "What changed?",
        corpus: "legal-cases",
        top_k: 8,
      }),
    ).resolves.toMatchObject({ answer: "grounded answer" });
    expect(mockFetch).toHaveBeenCalledWith(
      "/services/prompt",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ "X-Errorta-Token": "ert_abc" }),
        body: JSON.stringify({
          prompt: "What changed?",
          corpus: "legal-cases",
          top_k: 8,
        }),
      }),
    );
  });

  it("keeps catalog verification metadata on meta responses", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        errorta_version: "0.6.0",
        aiar_version: "0.2.4",
        sdk_contract_version: "1.0",
        judge_available: true,
        default_model: "qwen3.5:9b",
        default_judge_model: "llama3.1:8b",
        corpora: [{ name: "legal-cases" }],
        corpus_source: "remote_unverified",
        catalog_verified: false,
      }),
    );

    const data = await meta("ert_abc");

    expect(data.catalog_verified).toBe(false);
    expect(data.corpus_source).toBe("remote_unverified");
    expect(mockFetch).toHaveBeenCalledWith(
      "/services/meta",
      expect.objectContaining({
        method: "GET",
        headers: expect.objectContaining({ "X-Errorta-Token": "ert_abc" }),
      }),
    );
  });
});
