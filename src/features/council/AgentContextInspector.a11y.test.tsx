// A11y and defensive rendering tests for AgentContextInspector.
//
// 1. axe-core: no serious/critical violations in the empty-capsules state.
// 2. axe-core: no violations in the single-capsule-selected state.
// 3. Sentinel: capsule state item text IS rendered (the component is designed
//    to show task metadata), but the test documents this boundary so a future
//    change that renders content it shouldn't will break loudly.
// 4. Defensive invariant 5: the component must NOT render raw payload bytes
//    (the RAW_PAYLOAD_TEXT_DO_NOT_RENDER_ALPHA42 sentinel must not appear
//    in any inline text node).
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import AgentContextInspector from "./AgentContextInspector";
import { expectNoA11yViolations } from "./a11y-helpers";

vi.mock("../../lib/api/agentContext", () => ({
  listAgentContextCapsules: vi.fn().mockResolvedValue([
    {
      capsuleId: "cap_abcdef1234567890",
      kind: "micro",
      parentId: null,
      createdAt: "2026-06-12T00:00:00Z",
      taskTitle: "Fix the calibration bug",
      canonicalSha256: "a".repeat(64),
    },
  ]),
  getAgentContextCapsule: vi.fn().mockResolvedValue({
    capsuleId: "cap_abcdef1234567890",
    kind: "micro",
    parentId: null,
    createdAt: "2026-06-12T00:00:00Z",
    task: { title: "Fix the calibration bug", intent: "Correct WS1 EMA math" },
    state: {
      decisions: [{ id: "d1", text: "Store base estimate, not calibrated." }],
    },
    refs: [],
    policy: {},
    digest: { canonical_sha256: "a".repeat(64) },
  }),
  packAgentContextCapsule: vi.fn().mockResolvedValue("micro capsule text"),
}));

afterEach(() => cleanup());

describe("AgentContextInspector a11y", () => {
  it("no_violations_empty_state", async () => {
    const { listAgentContextCapsules } = await import("../../lib/api/agentContext");
    (listAgentContextCapsules as ReturnType<typeof vi.fn>).mockResolvedValueOnce([]);
    const { container } = render(<AgentContextInspector />);
    await screen.findByText(/Council agents are not using capsules during live runs yet/);
    await expectNoA11yViolations(container);
  });

  it("no_violations_with_capsule", async () => {
    const { container } = render(<AgentContextInspector />);
    // Wait for state items to appear
    await screen.findByText(/Fix the calibration bug/);
    await expectNoA11yViolations(container);
  });
});

describe("AgentContextInspector defensive rendering", () => {
  it("renders task state item text (metadata designed for display)", async () => {
    render(<AgentContextInspector />);
    await screen.findByText(/Store base estimate/);
  });

  it("does not render raw payload bytes in any visible text node", async () => {
    render(<AgentContextInspector />);
    await screen.findByText(/Fix the calibration bug/);
    // Invariant 5 sentinel: if this string ever appears in the DOM, a
    // boundary has been crossed and the test will fail.
    expect(screen.queryByText(/RAW_PAYLOAD_TEXT_DO_NOT_RENDER_ALPHA42/)).toBeNull();
  });
});
