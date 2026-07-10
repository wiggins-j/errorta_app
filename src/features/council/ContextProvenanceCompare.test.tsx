// F031 Phase 5 polish (Task 3) — ContextProvenanceCompare coverage.
//
// Marquee invariant lock for the byte-isolation story: even with a
// future manifest accidentally carrying a `content` field, the compare
// layout never renders raw payload bytes. Extends the drawer-level
// defensive assertion (invariant 5) to the compare path.
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import ContextProvenanceCompare from "./ContextProvenanceCompare";
import type { CouncilContextManifest } from "./types";

function manifest(
  overrides: Partial<CouncilContextManifest> = {},
): CouncilContextManifest {
  return {
    manifestId: "cm-base",
    formatVersion: 1,
    contextId: "ctx",
    runId: "r-1",
    turnId: "t-1",
    memberId: "m-full",
    payloadSha256: "deadbeef".repeat(8),
    requestedContextAccess: "full_context",
    effectiveContextAccess: "full_context",
    requestedTranscriptAccess: "all_messages",
    effectiveTranscriptAccess: "all_messages",
    destinationScope: "local",
    egressClass: "local",
    sourceCounts: {},
    sourceRefs: [],
    omitted: [],
    tokenEstimate: {},
    blockedReason: null,
    transformManifestId: null,
    visibilityPlanId: null,
    f030AuditId: null,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
});

describe("ContextProvenanceCompare", () => {
  it("renders two members side-by-side with distinct policies", () => {
    const a = manifest({
      memberId: "m-full",
      effectiveContextAccess: "full_context",
    });
    const b = manifest({
      memberId: "m-redacted",
      effectiveContextAccess: "redacted_summary",
    });
    render(<ContextProvenanceCompare manifests={[a, b]} />);
    // Both member IDs render in the strip region.
    expect(
      screen.getByRole("region", {
        name: "Per-member context comparison",
      }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("m-full").length).toBeGreaterThan(0);
    expect(screen.getAllByText("m-redacted").length).toBeGreaterThan(0);
    // Both policy values surface in the rendered DOM at least once each.
    expect(
      screen.getAllByText(/full_context|redacted_summary/).length,
    ).toBeGreaterThanOrEqual(2);
  });

  it("distinct source_refs render under the two columns", () => {
    const a = manifest({
      memberId: "m-A",
      sourceRefs: [
        {
          class_: "retrieved_snippet",
          citationId: "ct-A",
          contentSha256: "a".repeat(64),
        },
      ],
    });
    const b = manifest({
      memberId: "m-B",
      sourceRefs: [
        {
          class_: "retrieved_snippet",
          citationId: "ct-B",
          contentSha256: "b".repeat(64),
        },
      ],
    });
    render(<ContextProvenanceCompare manifests={[a, b]} />);
    expect(screen.getByText("ct-A")).toBeInTheDocument();
    expect(screen.getByText("ct-B")).toBeInTheDocument();
  });

  it("defensive: stray content field never leaks (invariant 5)", () => {
    const a = manifest({ memberId: "m-A" });
    // Build B with an extra `content` property that the type doesn't allow,
    // simulating a future backend bug.
    const b = {
      ...manifest({ memberId: "m-B" }),
      content: "RAW_PAYLOAD_TEXT_DO_NOT_RENDER_ALPHA42",
    } as CouncilContextManifest;
    const { container } = render(
      <ContextProvenanceCompare manifests={[a, b]} />,
    );
    expect(container.innerHTML).not.toContain(
      "RAW_PAYLOAD_TEXT_DO_NOT_RENDER_ALPHA42",
    );
  });

  it("all-blocked turn renders banner per column plus caption", () => {
    const a = manifest({
      memberId: "m-A",
      blockedReason: "unknown_context_access",
    });
    const b = manifest({
      memberId: "m-B",
      blockedReason: "egress_violation",
    });
    const { container } = render(
      <ContextProvenanceCompare manifests={[a, b]} />,
    );
    expect(screen.getByText(/All members blocked/)).toBeInTheDocument();
    const banners = container.querySelectorAll(".cid-blocked-banner");
    expect(banners.length).toBe(2);
  });

  // F031-DEMO-A11Y-SWEEP Task 4 — explicit lock that the
  // F031-PROVENANCE-VIZ diff-cell aria-label contract is reachable
  // through the compare-strip render path (not just ContextPolicyRow
  // in isolation). Without this lock, a future refactor that drops
  // the aria-label inside the strip would only fail the ContextPolicyRow
  // unit test and silently pass the marquee path.
  it("diff cells carry aria-label describing the difference", () => {
    const a = manifest({
      memberId: "m-full",
      effectiveContextAccess: "full_context",
    });
    const b = manifest({
      memberId: "m-redacted",
      effectiveContextAccess: "redacted_summary",
    });
    const { container } = render(
      <ContextProvenanceCompare manifests={[a, b]} />,
    );
    const diffCell = container.querySelector(".cid-compare-cell-differs");
    expect(diffCell).not.toBeNull();
    const label = diffCell?.getAttribute("aria-label") ?? "";
    expect(label).toMatch(/differs from baseline/);
    expect(label).toContain("m-redacted");
    expect(label).toContain("redacted_summary");
  });

  it("identical-policy fixture renders no-diff caption", () => {
    const a = manifest({ memberId: "m-A" });
    const b = manifest({ memberId: "m-B" });
    const { container } = render(
      <ContextProvenanceCompare manifests={[a, b]} />,
    );
    expect(
      screen.getByText(/No policy differences across members/),
    ).toBeInTheDocument();
    expect(
      container.querySelectorAll(".cid-compare-cell-differs").length,
    ).toBe(0);
  });

  it("focused column gets focused class", () => {
    const a = manifest({ memberId: "m-full" });
    const b = manifest({ memberId: "m-redacted" });
    const { container } = render(
      <ContextProvenanceCompare
        manifests={[a, b]}
        focusedMemberId="m-redacted"
      />,
    );
    const focusedCol = container.querySelector(
      '.cid-compare-col[data-member-id="m-redacted"]',
    );
    expect(focusedCol).not.toBeNull();
    expect(focusedCol!.classList.contains("cid-compare-col-focused")).toBe(
      true,
    );
  });

  it("tab order is row-major", () => {
    // Assert DOM order: policy cells across columns come before any
    // source-counts content cell.
    const a = manifest({
      memberId: "m-A",
      sourceCounts: { user_prompt: 1 },
    });
    const b = manifest({
      memberId: "m-B",
      sourceCounts: { user_prompt: 1 },
    });
    const { container } = render(
      <ContextProvenanceCompare manifests={[a, b]} />,
    );
    const policyCells = Array.from(
      container.querySelectorAll(".cid-compare-policy-table tbody td"),
    );
    const sourceCountsSections = container.querySelectorAll(
      'section[aria-label="Source counts"]',
    );
    expect(policyCells.length).toBeGreaterThan(0);
    expect(sourceCountsSections.length).toBeGreaterThan(0);
    // The first source-counts section starts AFTER all the policy cells
    // in document order.
    const lastPolicyCell = policyCells[policyCells.length - 1];
    const firstSourceCountsSection = sourceCountsSections[0];
    const cmp = lastPolicyCell.compareDocumentPosition(
      firstSourceCountsSection,
    );
    // Node.DOCUMENT_POSITION_FOLLOWING = 4
    expect(cmp & 4).toBeTruthy();
  });
});
