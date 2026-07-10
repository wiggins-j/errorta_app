// F110 — hardware recommendation buttons: honest copy + persist-the-choice.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { RecommendationActionButtons } from "./RecommendationActionButtons";
import type { ModelTier } from "./types";

function tier(overrides: Partial<ModelTier> = {}): ModelTier {
  return {
    id: "llama3.2:3b",
    label: "Llama 3.2 3B (Q4)",
    params_b: 3,
    quant: "Q4_K_M",
    vram_gb: 3,
    install_gb: 2,
    tok_s_low: 60,
    tok_s_high: 120,
    install_label: "~2 GB",
    vram_label: "3 GB",
    tok_label: "60–120 tok/s",
    compatible: true,
    incompatible_reason: null,
    ...overrides,
  };
}

afterEach(() => cleanup());

describe("RecommendationActionButtons — F110", () => {
  it("copy says the model is set up in the next step (no install-now implication)", () => {
    const primary = tier();
    render(
      <RecommendationActionButtons
        primary={primary}
        allModels={[primary]}
        selectedId={primary.id}
        onSelect={vi.fn()}
        onUseSelected={vi.fn()}
      />,
    );
    expect(screen.getByText(/download this model in the next step/i)).toBeInTheDocument();
    // No stale F003 routing copy.
    expect(screen.queryByText(/F003/i)).toBeNull();
  });

  it("clicking 'Use Recommended' hands the model to onUseSelected", () => {
    const primary = tier();
    const onUseSelected = vi.fn();
    render(
      <RecommendationActionButtons
        primary={primary}
        allModels={[primary]}
        selectedId={primary.id}
        onSelect={vi.fn()}
        onUseSelected={onUseSelected}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /use recommended/i }));
    expect(onUseSelected).toHaveBeenCalledWith(primary);
  });
});
