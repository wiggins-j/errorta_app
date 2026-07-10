// F031-DEMO-A11Y-SWEEP Task 2 — axe-core sweep for the compare strip.
// Reuses the two-member fixture shape from ContextProvenanceCompare.test.tsx
// so the a11y suite cannot drift from the behavioral suite.
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, it } from "vitest";

import ContextProvenanceCompare from "./ContextProvenanceCompare";
import type { CouncilContextManifest } from "./types";
import { expectNoA11yViolations } from "./a11y-helpers";

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

describe("ContextProvenanceCompare a11y", () => {
  it("no_serious_or_critical_violations_on_two_member_fixture", async () => {
    const a = manifest({
      memberId: "m-full",
      effectiveContextAccess: "full_context",
      sourceCounts: { retrieved_snippet: 2 },
      sourceRefs: [
        {
          class_: "retrieved_snippet",
          citationId: "ct-A",
          contentSha256: "a".repeat(64),
        },
      ],
    });
    const b = manifest({
      memberId: "m-redacted",
      effectiveContextAccess: "redacted_summary",
      sourceCounts: { summary_only: 1 },
      sourceRefs: [
        {
          class_: "summary",
          citationId: "ct-B",
          contentSha256: "b".repeat(64),
        },
      ],
    });
    const { container } = render(
      <ContextProvenanceCompare manifests={[a, b]} />,
    );
    await expectNoA11yViolations(container);
  });

  it("no_violations_with_blocked_manifest_in_compare_strip", async () => {
    const a = manifest({ memberId: "m-A" });
    const b = manifest({
      memberId: "m-B",
      blockedReason: "egress_violation",
    });
    const { container } = render(
      <ContextProvenanceCompare manifests={[a, b]} />,
    );
    await expectNoA11yViolations(container);
  });
});
