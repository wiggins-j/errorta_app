import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  getAiarStatus: vi.fn(),
  updateAiarConnection: vi.fn(),
}));

vi.mock("../../lib/api/aiarConnection", () => ({
  getAiarStatus: apiMocks.getAiarStatus,
  updateAiarConnection: apiMocks.updateAiarConnection,
}));

import AiarConnectionCard from "./AiarConnectionCard";

const SENDITAI_STATUS = {
  runtime_kind: "aiar-service",
  kind: "aiar-service",
  display_name: "example-host",
  connected: true,
  base_url: "http://127.0.0.1:8766",
  token_configured: true,
  verify_tls: true,
  timeout_s: 60,
  backend_id: "example-host",
  capabilities: {
    answer: true,
    judge: true,
    model_catalog: true,
    model_active_status: true,
    model_set_active: false,
    ollama_pull: false,
    corpus_list: true,
    corpus_upload: false,
    folder_watch: false,
    pure_retrieve: true,
    grounding_record: true,
    grounding_lookup: true,
    remote_ingest: true,
  },
  active_model: "qwen3.5:9b",
  active_model_ready: true,
  available_models: ["qwen3.5:9b"],
  corpus_count: 12,
};

describe("AiarConnectionCard", () => {
  beforeEach(() => {
    apiMocks.getAiarStatus.mockReset();
    apiMocks.updateAiarConnection.mockReset();
    apiMocks.getAiarStatus.mockResolvedValue(SENDITAI_STATUS);
    apiMocks.updateAiarConnection.mockResolvedValue({
      configured: true,
      canonical: {},
      status: SENDITAI_STATUS,
    });
  });

  it("saves canonical AIAR-service settings and preserves omitted token", async () => {
    render(<AiarConnectionCard />);

    await screen.findByText("AIAR: connected on example-host - qwen3.5:9b ready");
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "example-host lab" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save AIAR connection" }));

    await waitFor(() => {
      expect(apiMocks.updateAiarConnection).toHaveBeenCalled();
    });
    expect(apiMocks.updateAiarConnection).toHaveBeenCalledWith({
      kind: "aiar-service",
      display_name: "example-host lab",
      base_url: "http://127.0.0.1:8766",
      token: undefined,
      timeout_s: 60,
      verify_tls: true,
      allow_disconnected: false,
    });
  });
});
