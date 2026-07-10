// F031-DEMO-A11Y-SWEEP Task 2 — drawer-level full-mount axe sweep.
// Mounts the drawer with a two-manifest fixture so the compare strip
// renders inside it. This is the highest-cost axe call in the suite;
// if runtime budget proves tight, scope to the drawer body
// (`.cid-drawer`) instead of the full backdrop+drawer mount.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, it, vi } from "vitest";

import ContextInspectionDrawer from "./ContextInspectionDrawer";
import { expectNoA11yViolations } from "./a11y-helpers";

function mockResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function backendManifest(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    manifest_id: "cm-x",
    format_version: 1,
    context_id: "ctx",
    run_id: "r",
    turn_id: "t",
    member_id: "m",
    payload_sha256: "deadbeef".repeat(8),
    requested_context_access: "full_context",
    effective_context_access: "full_context",
    requested_transcript_access: "all_messages",
    effective_transcript_access: "all_messages",
    destination_scope: "local",
    egress_class: "local",
    source_counts: {},
    source_refs: [],
    omitted: [],
    token_estimate: {},
    blocked_reason: null,
    transform_manifest_id: null,
    visibility_plan_id: null,
    f030_audit_id: null,
    ...overrides,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

describe("ContextInspectionDrawer a11y", () => {
  it("no_violations_when_open_with_two_manifests", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockResponse({
          run_id: "r",
          turn_id: "t",
          manifest_count: 2,
          manifests: [
            backendManifest({
              manifest_id: "cm-full",
              member_id: "m-full",
              effective_context_access: "full_context",
              source_counts: { retrieved_snippet: 2 },
            }),
            backendManifest({
              manifest_id: "cm-red",
              member_id: "m-redacted",
              effective_context_access: "redacted_summary",
              source_counts: { summary_only: 1 },
            }),
          ],
        }),
      ),
    );
    const { container } = render(
      <ContextInspectionDrawer runId="r" turnId="t" onClose={() => {}} />,
    );
    await waitFor(() =>
      screen.getByRole("region", { name: "Per-member context comparison" }),
    );
    await expectNoA11yViolations(container);
  });
});
