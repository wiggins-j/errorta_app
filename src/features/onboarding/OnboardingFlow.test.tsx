import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// The flow is a thin shell around StepConnectAI; mock the step so we test the
// flow's contract (single screen, both actions complete onboarding), not the
// step internals (covered in StepConnectAI.test.tsx).
vi.mock("./StepConnectAI", () => ({
  default: ({
    onAdvance,
    onSkip,
  }: {
    onAdvance: () => void;
    onSkip: () => void;
  }) => (
    <div data-testid="step-connect-ai-mock">
      <button type="button" onClick={onAdvance}>
        continue-mock
      </button>
      <button type="button" onClick={onSkip}>
        skip-mock
      </button>
    </div>
  ),
}));

import OnboardingFlow from "./OnboardingFlow";

describe("OnboardingFlow (slim first-run)", () => {
  it("renders a single Connect-your-AI screen with no wizard chrome", () => {
    const { container } = render(<OnboardingFlow onComplete={vi.fn()} />);

    expect(screen.getByTestId("step-connect-ai-mock")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /Welcome to Errorta/i }),
    ).toBeInTheDocument();

    // No multi-step wizard: no step pills, no progress bar.
    expect(container.querySelector(".onboarding-steps")).toBeNull();
    expect(container.querySelector(".onboarding-progress")).toBeNull();
  });

  it("has no residency / hardware / sample-corpus steps", () => {
    render(<OnboardingFlow onComplete={vi.fn()} />);
    expect(screen.queryByTestId("step-residency-mock")).toBeNull();
    expect(screen.queryByTestId("step-hardware-mock")).toBeNull();
    expect(screen.queryByTestId("step-welcome-mock")).toBeNull();
  });

  it("Continue completes onboarding", () => {
    const onComplete = vi.fn();
    render(<OnboardingFlow onComplete={onComplete} />);
    screen.getByText("continue-mock").click();
    expect(onComplete).toHaveBeenCalledTimes(1);
  });

  it("Skip completes onboarding", () => {
    const onComplete = vi.fn();
    render(<OnboardingFlow onComplete={onComplete} />);
    screen.getByText("skip-mock").click();
    expect(onComplete).toHaveBeenCalledTimes(1);
  });
});
