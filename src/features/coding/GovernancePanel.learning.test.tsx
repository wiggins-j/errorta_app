// F135 — the "What the PM has learned" launcher inside PM Governance.
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/coding", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/coding")>();
  return {
    ...actual,
    getModelLearning: vi.fn().mockResolvedValue({
      summary: {
        totalAttempts: 12,
        distinctRoutes: 1,
        windowDays: 90,
        generatedAt: "",
        corpusAvailable: true,
      },
      thresholds: { minAttempts: 5, demotionRate: 0.6, preferredRate: 0.8 },
      routes: [
        {
          routeId: "claude_cli.sonnet",
          capabilityTier: "mid",
          costTier: 1,
          tiersUnset: false,
          buckets: [
            {
              taskType: "implementation",
              difficultyTier: "mid",
              attempts: 12,
              accepted: 11,
              acceptedRate: 0.916,
              gatewayFailureRate: 0,
              p50LatencyMs: 42000,
              avgCostTier: 1,
              standing: "preferred",
            },
          ],
        },
      ],
    }),
  };
});

import GovernancePanel from "./GovernancePanel";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("GovernancePanel — PM learning launcher", () => {
  it("opens the learning sheet when the button is clicked", async () => {
    render(<GovernancePanel projectId="p" governance={null} />);
    // No dialog until the button is clicked.
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /what the pm has learned/i }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toBeInTheDocument();
    expect(await screen.findByText("Preferred")).toBeInTheDocument();
  });
});
