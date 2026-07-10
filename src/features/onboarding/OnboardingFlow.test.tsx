import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { OnboardingState } from "../../lib/api/onboarding";

const hookState: {
  state: OnboardingState;
  loaded: boolean;
  refresh: ReturnType<typeof vi.fn>;
} = {
  state: {
    residency_ready: false,
    residency_mode: "local",
    hardware_ready: false,
    ollama_ready: false,
    corpora_present: false,
    judge_ready: false,
    recommended_next_step: "residency",
    corpora: [],
    ollama_error: null,
  },
  loaded: true,
  refresh: vi.fn(),
};

vi.mock("./useOnboardingState", () => ({
  useOnboardingState: () => ({
    state: hookState.state,
    loaded: hookState.loaded,
    refresh: hookState.refresh,
  }),
}));

vi.mock("./StepResidency", () => ({
  default: () => <div data-testid="step-residency-mock" />,
}));
vi.mock("./StepHardware", () => ({
  default: () => <div data-testid="step-hardware-mock" />,
}));
vi.mock("./StepConnectAI", () => ({
  default: () => <div data-testid="step-connect-ai-mock" />,
}));
vi.mock("./StepWelcome", () => ({
  default: () => <div data-testid="step-welcome-mock" />,
}));

import OnboardingFlow from "./OnboardingFlow";

function resetState(overrides: Partial<OnboardingState> = {}) {
  hookState.state = {
    residency_ready: false,
    residency_mode: "local",
    hardware_ready: false,
    ollama_ready: false,
    corpora_present: false,
    judge_ready: false,
    recommended_next_step: "residency",
    corpora: [],
    ollama_error: null,
    ...overrides,
  };
  hookState.loaded = true;
  hookState.refresh = vi.fn();
  localStorage.clear();
}

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

describe("OnboardingFlow", () => {
  beforeEach(() => {
    installLocalStorageShim();
    resetState();
  });

  it("shows data residency first on a fresh install", async () => {
    render(<OnboardingFlow onComplete={vi.fn()} />);

    expect(await screen.findByTestId("step-residency-mock")).toBeInTheDocument();
    expect(screen.queryByTestId("step-hardware-mock")).toBeNull();
  });

  it("renders residency before server state has loaded", () => {
    hookState.loaded = false;

    render(<OnboardingFlow onComplete={vi.fn()} />);

    expect(screen.getByTestId("step-residency-mock")).toBeInTheDocument();
    expect(screen.queryByTestId("step-hardware-mock")).toBeNull();
  });

  it("keeps residency first until the server reports residency_ready", async () => {
    resetState({ recommended_next_step: "hardware", residency_ready: false });

    render(<OnboardingFlow onComplete={vi.fn()} />);

    expect(await screen.findByTestId("step-residency-mock")).toBeInTheDocument();
  });

  it("moves to server-recommended hardware after residency is selected", async () => {
    resetState({ recommended_next_step: "hardware", residency_ready: true });

    render(<OnboardingFlow onComplete={vi.fn()} />);

    expect(await screen.findByTestId("step-hardware-mock")).toBeInTheDocument();
  });

  it("renders the setup steps in residency-first order", () => {
    const { container } = render(<OnboardingFlow onComplete={vi.fn()} />);
    const stepper = container.querySelector(".onboarding-steps");
    expect(stepper).not.toBeNull();
    const labels = within(stepper as HTMLElement)
      .getAllByRole("button")
      .map((button) => button.textContent?.replace(/\d+/g, "").trim());

    expect(labels).toEqual([
      "Data residency",
      "Hardware",
      "Connect AI",
      "Sample corpus",
    ]);
  });

  it("has no Judge or Briefs step (F132 removal)", () => {
    render(<OnboardingFlow onComplete={vi.fn()} />);
    const labels = screen
      .getAllByRole("button")
      .map((b) => b.textContent?.replace(/\d+/g, "").trim());
    expect(labels).not.toContain("Judge");
    expect(labels).not.toContain("Briefs");
  });

  it("maps a removed-step recommendation (judge) to the last step, not a deleted one", async () => {
    resetState({ recommended_next_step: "judge", residency_ready: true });

    render(<OnboardingFlow onComplete={vi.fn()} />);

    // "judge"/"briefs"/"done" all land on the Sample corpus (welcome) handoff.
    expect(await screen.findByTestId("step-welcome-mock")).toBeInTheDocument();
    expect(screen.queryByTestId("step-judge-mock")).toBeNull();
  });

  it("places Connect AI after Hardware and no standalone Ollama step exists", async () => {
    resetState({ recommended_next_step: "ollama", residency_ready: true });

    render(<OnboardingFlow onComplete={vi.fn()} />);

    // The backend recommends "ollama"; it maps to the folded-in connect-ai step.
    expect(
      await screen.findByTestId("step-connect-ai-mock"),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("step-ollama-mock")).toBeNull();

    // "Connect AI" is the third pill (after Data residency, Hardware).
    const labels = screen
      .getAllByRole("button")
      .map((b) => b.textContent?.replace(/\d+/g, "").trim());
    expect(labels.indexOf("Connect AI")).toBe(labels.indexOf("Hardware") + 1);
  });

  it("advances from Hardware into Connect AI", async () => {
    resetState({ recommended_next_step: "hardware", residency_ready: true });

    render(<OnboardingFlow onComplete={vi.fn()} />);

    expect(await screen.findByTestId("step-hardware-mock")).toBeInTheDocument();

    // Click the Connect AI pill to navigate there.
    const connectPill = screen
      .getAllByRole("button")
      .find((b) => b.textContent?.includes("Connect AI"));
    expect(connectPill).toBeTruthy();
    connectPill?.click();

    expect(
      await screen.findByTestId("step-connect-ai-mock"),
    ).toBeInTheDocument();
  });

  it("marks Connect AI done from the seen sentinel", async () => {
    resetState({ recommended_next_step: "hardware", residency_ready: true });
    localStorage.setItem("errorta.onboarding.connect-ai.seen", "1");

    const { container } = render(<OnboardingFlow onComplete={vi.fn()} />);

    const pills = Array.from(
      container.querySelectorAll(".onboarding-step-pill"),
    );
    const connectPill = pills.find((li) =>
      li.textContent?.includes("Connect AI"),
    );
    expect(connectPill?.className).toContain("is-done");
  });
});
