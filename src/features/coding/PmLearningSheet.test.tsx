// F135 — PM learning info sheet.
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/coding", () => ({ getModelLearning: vi.fn() }));
import { getModelLearning } from "../../lib/api/coding";
import PmLearningSheet from "./PmLearningSheet";
import { expectNoA11yViolations } from "../council/a11y-helpers";

const mockLearning = getModelLearning as unknown as ReturnType<typeof vi.fn>;

function digest(overrides: Record<string, unknown> = {}) {
  return {
    summary: {
      totalAttempts: 40,
      distinctRoutes: 2,
      windowDays: 90,
      generatedAt: "2026-07-02T20:00:00+00:00",
      corpusAvailable: true,
    },
    thresholds: { minAttempts: 5, demotionRate: 0.6, preferredRate: 0.8 },
    routes: [
      {
        routeId: "claude_cli.haiku",
        capabilityTier: "light",
        costTier: 1,
        tiersUnset: false,
        buckets: [
          {
            taskType: "implementation",
            difficultyTier: "light",
            attempts: 20,
            accepted: 18,
            acceptedRate: 0.9,
            gatewayFailureRate: 0,
            p50LatencyMs: 8000,
            avgCostTier: 1,
            standing: "preferred",
          },
          {
            taskType: "implementation",
            difficultyTier: "mid",
            attempts: 9,
            accepted: 3,
            acceptedRate: 0.333,
            gatewayFailureRate: 0,
            p50LatencyMs: 15000,
            avgCostTier: 1,
            standing: "demoted",
          },
        ],
      },
    ],
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("PmLearningSheet", () => {
  it("renders nothing when closed", () => {
    mockLearning.mockResolvedValue(digest());
    const { container } = render(<PmLearningSheet isOpen={false} onClose={() => {}} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders per-route standings, emphasising the shared/cross-project nature", async () => {
    mockLearning.mockResolvedValue(digest());
    render(<PmLearningSheet isOpen onClose={() => {}} />);
    expect(await screen.findByText(/shared across all your projects/i)).toBeInTheDocument();
    expect(screen.getByText(/40 task attempts over the last 90 days/i)).toBeInTheDocument();
    expect(await screen.findByText("Preferred")).toBeInTheDocument();
    expect(screen.getByText("Demoted")).toBeInTheDocument();
    expect(screen.getByText(/prefers a stronger model here/i)).toBeInTheDocument();
  });

  it("shows the empty state when the corpus is cold", async () => {
    mockLearning.mockResolvedValue(
      digest({ summary: { totalAttempts: 0, distinctRoutes: 0, windowDays: 90, generatedAt: "", corpusAvailable: false }, routes: [] }),
    );
    render(<PmLearningSheet isOpen onClose={() => {}} />);
    expect(
      await screen.findByText(/no model performance recorded yet/i),
    ).toBeInTheDocument();
  });

  it("closes on Escape and on backdrop click", async () => {
    mockLearning.mockResolvedValue(digest());
    const onClose = vi.fn();
    render(<PmLearningSheet isOpen onClose={onClose} />);
    await screen.findByText("Preferred");

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);

    const dialog = screen.getByRole("dialog");
    fireEvent.click(dialog); // click the backdrop itself
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("has no serious/critical axe violations", async () => {
    mockLearning.mockResolvedValue(digest());
    const { container } = render(<PmLearningSheet isOpen onClose={() => {}} />);
    await screen.findByText("Preferred");
    await expectNoA11yViolations(container);
  });
});
