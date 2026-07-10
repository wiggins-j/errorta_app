import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/hardware", () => ({
  scan: vi.fn(),
}));

import * as hardwareApi from "../../lib/api/hardware";
import type { HardwareReport, ModelTier } from "../../features/hardware/types";
import StepHardware from "./StepHardware";

const scan = hardwareApi.scan as unknown as ReturnType<typeof vi.fn>;

function tier(id: string, compatible: boolean): ModelTier {
  return {
    id,
    label: id.toUpperCase(),
    params_b: 7,
    quant: "Q4",
    vram_gb: 5,
    install_gb: 5,
    tok_s_low: 20,
    tok_s_high: 40,
    install_label: "~5 GB download",
    vram_label: "5 GB VRAM",
    tok_label: "20–40 tok/s",
    compatible,
    incompatible_reason: compatible ? null : "needs more VRAM",
  };
}

function report(primaryCompatible = true): HardwareReport {
  const primary = tier("qwen2.5:7b", primaryCompatible);
  return {
    scanned_at: "now",
    gpu: {
      vendor: "Apple",
      model: "M3",
      vram_gb: 16,
      driver: null,
      unified_memory: true,
    },
    ram_gb: 16,
    disk_free_gb: 200,
    cpu: { model: "Apple M3", cores: 8, avx: false, avx2: false },
    os: { name: "macOS", version: "26", arch: "arm64" },
    recommendation: {
      available_vram_gb: 16,
      primary,
      faster: tier("llama3.2:3b", true),
      capable: null,
      incompatible: [],
      all: [primary],
      rationale: "Picked for your GPU.",
      table_version: "1",
    },
  };
}

// happy-dom v20 does not expose localStorage by default; StepHardware persists
// the selected model there. Install a minimal in-memory shim.
function installLocalStorageShim(): void {
  const store = new Map<string, string>();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      clear: () => store.clear(),
      getItem: (key: string) => store.get(key) ?? null,
      removeItem: (key: string) => store.delete(key),
      setItem: (key: string, value: string) => store.set(key, String(value)),
    },
  });
}

describe("StepHardware", () => {
  beforeEach(() => {
    scan.mockReset();
    installLocalStorageShim();
  });

  it("shows scan results and does NOT auto-advance; Next advances", async () => {
    scan.mockResolvedValue(report());
    const onAdvance = vi.fn();
    const user = userEvent.setup();
    render(<StepHardware done={false} onAdvance={onAdvance} onSkip={vi.fn()} />);

    await user.click(screen.getByTestId("onboarding-hardware-scan"));

    // Results render; advance has NOT been called just from scanning.
    expect(await screen.findByTestId("hw-results")).toBeInTheDocument();
    expect(screen.getByText(/Picked for your GPU/i)).toBeInTheDocument();
    expect(onAdvance).not.toHaveBeenCalled();

    // The recommended tier is pre-selected and persisted for the Ollama step.
    expect(localStorage.getItem("errorta.selectedModel")).toBe("qwen2.5:7b");

    // Explicit Next advances.
    await user.click(screen.getByTestId("onboarding-hardware-next"));
    expect(onAdvance).toHaveBeenCalledTimes(1);
  });

  it("renders the all-incompatible branch without pre-selecting a model", async () => {
    scan.mockResolvedValue(report(false));
    const user = userEvent.setup();
    render(<StepHardware done={false} onAdvance={vi.fn()} onSkip={vi.fn()} />);

    await user.click(screen.getByTestId("onboarding-hardware-scan"));

    expect(await screen.findByTestId("hw-none-fit")).toBeInTheDocument();
    expect(localStorage.getItem("errorta.selectedModel")).toBeNull();
  });

  it("lets the user skip after a scan failure", async () => {
    scan.mockRejectedValue(new Error("Load failed"));
    const onAdvance = vi.fn();
    const onSkip = vi.fn();
    const user = userEvent.setup();
    render(<StepHardware done={false} onAdvance={onAdvance} onSkip={onSkip} />);

    await user.click(screen.getByTestId("onboarding-hardware-scan"));
    await screen.findByText(/scan failed: load failed/i);

    await user.click(screen.getByTestId("onboarding-hardware-skip-step"));
    expect(onAdvance).toHaveBeenCalledTimes(1);
    expect(onSkip).not.toHaveBeenCalled();
  });
});
