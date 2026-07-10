// F031-DEMO-A11Y-SWEEP Task 2 — axe-core sweep for ContextPolicyRow.
// Three cases: differing policies, identical policies, one blocked
// manifest.
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, it } from "vitest";

import ContextPolicyRow from "./ContextPolicyRow";
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

describe("ContextPolicyRow a11y", () => {
  it("no_violations_with_differing_policies", async () => {
    const a = manifest({
      memberId: "m-full",
      effectiveContextAccess: "full_context",
    });
    const b = manifest({
      memberId: "m-redacted",
      effectiveContextAccess: "redacted_summary",
    });
    const { container } = render(<ContextPolicyRow manifests={[a, b]} />);
    await expectNoA11yViolations(container);
  });

  it("no_violations_with_identical_policies", async () => {
    const a = manifest({ memberId: "m-a" });
    const b = manifest({ memberId: "m-b" });
    const { container } = render(<ContextPolicyRow manifests={[a, b]} />);
    await expectNoA11yViolations(container);
  });

  it("no_violations_with_blocked_manifest", async () => {
    const a = manifest({ memberId: "m-a" });
    const b = manifest({
      memberId: "m-b",
      blockedReason: "unknown_context_access",
    });
    const { container } = render(<ContextPolicyRow manifests={[a, b]} />);
    await expectNoA11yViolations(container);
  });
});
