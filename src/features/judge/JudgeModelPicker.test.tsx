import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/judge", () => ({
  fetchModel: vi.fn(async () => ({ judge_model: "llama3.1:8b", source: "default" })),
  fetchPreflight: vi.fn(async () => ({
    judge_model: "llama3.1:8b",
    judge_model_source: "default",
    aiar_available: true,
    ollama_reachable: true,
    model_available: true,
    runtime_kind: "aiar-service",
    display_name: "example-host",
    aiar_connected: true,
    backend_id: "example-host",
    answer_available: true,
    judge_available: true,
    active_model: "qwen3.5:9b",
    active_model_ready: true,
    available_models: ["qwen3.5:9b"],
    model_source: "aiar-active",
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
  })),
  setModel: vi.fn(),
}));

vi.mock("./SimilarityThresholdSlider", () => ({
  default: () => <div data-testid="threshold-slider" />,
}));

import JudgeModelPicker from "./JudgeModelPicker";

describe("JudgeModelPicker", () => {
  it("renders remote AIAR status without local Ollama pull guidance", async () => {
    render(<JudgeModelPicker />);

    await waitFor(() => {
      expect(screen.getByLabelText("Judge model status")).toBeInTheDocument();
    });

    expect(screen.getByText("AIAR: connected on example-host")).toBeInTheDocument();
    expect(screen.getByText("model: qwen3.5:9b ready")).toBeInTheDocument();
    expect(screen.queryByText(/ollama pull/i)).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /Judge model/i })).toBeDisabled();
  });
});
