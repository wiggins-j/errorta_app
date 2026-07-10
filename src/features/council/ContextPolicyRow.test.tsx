// F031 Phase 5 polish (Task 2) — ContextPolicyRow coverage.
//
// Locks the sticky-top-row diff-highlight contract for the compare strip.
// Differing cells must carry the cid-compare-cell-differs class + an
// aria-label naming the field, member, and value. Single-manifest input
// is a defensive call site (the drawer routes single-member turns to
// the existing card layout, but the component must not crash if called).
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import ContextPolicyRow from "./ContextPolicyRow";
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

describe("ContextPolicyRow", () => {
  it("renders one column per member", () => {
    const a = manifest({ memberId: "m-a" });
    const b = manifest({ memberId: "m-b" });
    render(<ContextPolicyRow manifests={[a, b]} />);
    // QA P2 #7 (2026-06-12): the table no longer carries
    // role="region" — native table semantics are preserved instead.
    // Find the table by its aria-label, which still announces purpose.
    const table = screen.getByRole("table", {
      name: "Per-member context policy comparison",
    });
    // Two member headers.
    const headers = table.querySelectorAll("thead th");
    expect(headers.length).toBe(2);
    // Four policy rows × two members = 8 td.
    const cells = table.querySelectorAll("tbody td");
    expect(cells.length).toBe(8);
  });

  it("highlights cells whose value differs from baseline", () => {
    const a = manifest({
      memberId: "m-full",
      effectiveContextAccess: "full_context",
    });
    const b = manifest({
      memberId: "m-redacted",
      effectiveContextAccess: "redacted_summary",
    });
    const { container } = render(
      <ContextPolicyRow manifests={[a, b]} />,
    );
    const differCells = container.querySelectorAll(".cid-compare-cell-differs");
    // Only effectiveContextAccess differs; baseline column (A) never diffs;
    // so we expect exactly one differs cell (B's effectiveContextAccess).
    expect(differCells.length).toBe(1);
    const cell = differCells[0] as HTMLElement;
    expect(cell.getAttribute("aria-label") ?? "").toMatch(
      /differs from baseline/,
    );
    expect(cell.getAttribute("aria-label") ?? "").toContain("m-redacted");
    expect(cell.getAttribute("aria-label") ?? "").toContain("redacted_summary");
    // Baseline cell does NOT carry the differs class.
    const baselineCells = container.querySelectorAll(
      'td[data-field="effectiveContextAccess"][data-member-id="m-full"]',
    );
    expect(baselineCells.length).toBe(1);
    expect(
      baselineCells[0].classList.contains("cid-compare-cell-differs"),
    ).toBe(false);
  });

  it("does not highlight when all values agree", () => {
    const a = manifest({ memberId: "m-a" });
    const b = manifest({ memberId: "m-b" });
    const { container } = render(
      <ContextPolicyRow manifests={[a, b]} />,
    );
    const differCells = container.querySelectorAll(".cid-compare-cell-differs");
    expect(differCells.length).toBe(0);
  });

  it("marks focused column", () => {
    const a = manifest({ memberId: "m-full" });
    const b = manifest({ memberId: "m-redacted" });
    const { container } = render(
      <ContextPolicyRow
        manifests={[a, b]}
        focusedMemberId="m-redacted"
      />,
    );
    const focusedHeader = container.querySelector(
      'th[data-member-id="m-redacted"]',
    );
    expect(focusedHeader).not.toBeNull();
    expect(focusedHeader!.classList.contains("cid-compare-col-focused")).toBe(
      true,
    );
    const focusedCells = container.querySelectorAll(
      'td[data-member-id="m-redacted"]',
    );
    expect(focusedCells.length).toBe(4);
    focusedCells.forEach((c) => {
      expect(c.classList.contains("cid-compare-col-focused")).toBe(true);
    });
    // Non-focused column does NOT carry the class.
    const unfocusedHeader = container.querySelector(
      'th[data-member-id="m-full"]',
    );
    expect(unfocusedHeader).not.toBeNull();
    expect(
      unfocusedHeader!.classList.contains("cid-compare-col-focused"),
    ).toBe(false);
  });

  it("renders blocked column without crashing", () => {
    const a = manifest({ memberId: "m-a" });
    const b = manifest({
      memberId: "m-b",
      blockedReason: "unknown_context_access",
    });
    const { container } = render(
      <ContextPolicyRow manifests={[a, b]} />,
    );
    // Still four cells in B's column.
    const bCells = container.querySelectorAll(
      'td[data-member-id="m-b"]',
    );
    expect(bCells.length).toBe(4);
  });

  it("single-manifest input renders one column with no diff cells", () => {
    const a = manifest({ memberId: "m-only" });
    const { container } = render(<ContextPolicyRow manifests={[a]} />);
    const headers = container.querySelectorAll("thead th");
    expect(headers.length).toBe(1);
    const differCells = container.querySelectorAll(".cid-compare-cell-differs");
    expect(differCells.length).toBe(0);
  });
});
