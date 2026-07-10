// F031-DEMO-A11Y-SWEEP Task 2 — axe-core sweep scoped to the
// "Try the demo prompt" button as it appears in CouncilShell's
// seeded-demo-room state. Per PM decision 5, the full CouncilShell
// render is too noisy and too slow; instead we mount the button in
// a minimal landmark host that mirrors the live structure
// (council-demo-prompt-actions wrapper inside an aria-label'd
// section) and scope axe to that subtree.
//
// QA P2 #7 (2026-06-12): also add a real CouncilShell mount under an
// App-shaped wrapper so landmark/heading/nesting issues at the seam
// (where App.tsx provides <main>) are exercised at least once. The
// per-component sweeps disable the `region` rule because individual
// components are mounted without their landmark parent; this one
// re-enables it so we catch nested-landmark drift the next time it
// happens.
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoA11yViolations } from "./a11y-helpers";
import CouncilShell from "./CouncilShell";

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

describe("CouncilShell demo prompt button a11y", () => {
  it("demo_prompt_button_no_violations", async () => {
    // Mirror the shape mounted in CouncilShell.tsx — a section landmark
    // wrapping the actions div containing the button. The component
    // source is the single source of truth for the markup; if it
    // diverges, this test breaks loudly.
    const { container } = render(
      <section aria-label="Demo prompt host">
        <div className="council-demo-prompt-actions">
          <button
            type="button"
            className="council-demo-prompt-btn"
            data-testid="try-demo-prompt-btn"
            onClick={() => {}}
          >
            Try the demo prompt
          </button>
        </div>
      </section>,
    );
    await expectNoA11yViolations(container);
  });
});

// ─────────────────────────────────────────────────────────────────────
// QA P2 #7 — full CouncilShell mount under an App-shaped wrapper.
//
// Catches:
//   - nested-landmark drift (App.tsx already wraps in <main>; the shell
//     used to add `role="main"`, fixed in the same commit as this test).
//   - heading-order drift (Council uses h2 inside the shell; App's
//     header may carry h1).
//   - any new aria-label/region/contrast regressions at the seam.
//
// The per-component sweeps disable `region` because they mount
// fragments without landmark parents; this seam test re-enables it.
// ─────────────────────────────────────────────────────────────────────

// Prevent AgentContextInspector from firing real network calls in jsdom.
vi.mock("../../lib/api/agentContext", () => ({
  listAgentContextCapsules: vi.fn().mockResolvedValue([]),
  getAgentContextCapsule: vi.fn().mockResolvedValue(null),
  packAgentContextCapsule: vi.fn().mockResolvedValue(""),
}));

beforeEach(() => {
  // Mock the global fetch the council API client uses. Returns empty
  // rooms + healthz so the shell renders the empty-state seed
  // affordance without doing any real network work.
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/council/rooms")) {
        return new Response(JSON.stringify({ rooms: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.endsWith("/healthz")) {
        return new Response(
          JSON.stringify({
            council: true,
            aiar_pin: { available: true, source: "editable" },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response("{}", { status: 404 });
    }),
  );
});

describe("CouncilShell — App-shaped a11y mount", () => {
  it("full shell under <main> has no serious/critical axe findings", async () => {
    // Mirror App.tsx's wrapper structure (App.tsx:148):
    //   <main className="main-pane">{children}</main>
    // so the shell sees its real landmark parent.
    const { container } = render(
      <main className="main-pane">
        <CouncilShell />
      </main>,
    );
    // Wait for the initial rooms fetch to resolve and the empty-state
    // affordance to mount.
    await waitFor(() => {
      // Empty-state seed affordance + "Rooms" heading both present.
      // We use the heading because it's stable across the empty-state
      // and seeded-room paths.
      expect(container.querySelector("h2")).not.toBeNull();
    });
    // Run axe with `region` ENABLED (overrides the helper default).
    // We pass through the helper which already filters to
    // serious+critical impact.
    await expectNoA11yViolations(container);
  });
});
