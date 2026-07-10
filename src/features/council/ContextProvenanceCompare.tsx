// F031 Phase 5 polish (Task 3) — ContextProvenanceCompare.
//
// The horizontal compare strip for multi-member turns. Composes the
// sticky ContextPolicyRow on top with three per-section rows below
// (counts, refs, omitted), each row showing one cell per member so the
// viewer can scan a section across all columns at once.
//
// Invariant 5 (sealed) is repeated here: the strip only ever renders
// fields from the typed CouncilContextManifest — never raw payload
// text. A defensive test in ContextProvenanceCompare.test.tsx asserts
// the lock holds even when a future backend bug surfaces a stray
// `content` field.
//
// Invariant 4 (fail-closed): any column whose manifest carries a
// blockedReason renders the existing red banner above its section
// cells, and a single "All members blocked" caption replaces the
// "No policy differences" caption when every column is blocked.
import { useEffect, useRef } from "react";
import type { CouncilContextManifest } from "./types";
import ContextPolicyRow from "./ContextPolicyRow";
import {
  CitationRefsSection,
  CompactionSection,
  OmittedSection,
  SourceCountsSection,
  SourceRefsSection,
  StewardSection,
  TokenEstimateSection,
  ToolResultsSection,
} from "./ContextManifestSections";

type PolicyField =
  | "effectiveContextAccess"
  | "effectiveTranscriptAccess"
  | "egressClass"
  | "destinationScope";

const POLICY_FIELDS: PolicyField[] = [
  "effectiveContextAccess",
  "effectiveTranscriptAccess",
  "egressClass",
  "destinationScope",
];

interface Props {
  manifests: CouncilContextManifest[];
  focusedMemberId?: string;
}

interface SectionRowProps {
  title: string;
  manifests: CouncilContextManifest[];
  focusedMemberId?: string;
  render: (m: CouncilContextManifest) => React.ReactNode;
}

function SectionRow({
  title,
  manifests,
  focusedMemberId,
  render,
}: SectionRowProps) {
  return (
    <div className="cid-compare-section">
      <h4 className="cid-compare-section-title">{title}</h4>
      <div className="cid-compare-row">
        {manifests.map((m) => {
          const focused = m.memberId === focusedMemberId;
          const cls = focused
            ? "cid-compare-col cid-compare-col-focused"
            : "cid-compare-col";
          return (
            <div
              key={m.memberId}
              className={cls}
              data-member-id={m.memberId}
            >
              {render(m)}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function ContextProvenanceCompare({
  manifests,
  focusedMemberId,
}: Props) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!focusedMemberId || !ref.current) return;
    const sel = `[data-member-id="${CSS.escape(focusedMemberId)}"]`;
    const el = ref.current.querySelector(sel);
    if (el && typeof (el as HTMLElement).scrollIntoView === "function") {
      (el as HTMLElement).scrollIntoView({
        block: "nearest",
        inline: "nearest",
      });
    }
  }, [focusedMemberId]);

  // Determine all-blocked vs no-diff captions.
  const allBlocked =
    manifests.length > 0 &&
    manifests.every((m) => m.blockedReason !== null && m.blockedReason !== undefined);

  const hasPolicyDifference = POLICY_FIELDS.some((field) => {
    const values = manifests.map((m) => m[field]);
    return new Set(values).size > 1;
  });

  return (
    <div
      ref={ref}
      className="cid-compare-strip"
      role="region"
      aria-label="Per-member context comparison"
    >
      {allBlocked && (
        <p className="cid-compare-all-blocked">All members blocked.</p>
      )}
      {!allBlocked && !hasPolicyDifference && (
        <p className="cid-compare-no-diff">
          No policy differences across members.
        </p>
      )}
      {/* Per-column blocked banner row. */}
      {manifests.some(
        (m) => m.blockedReason !== null && m.blockedReason !== undefined,
      ) && (
        <div className="cid-compare-row cid-compare-blocked-row">
          {manifests.map((m) => {
            const focused = m.memberId === focusedMemberId;
            const cls = focused
              ? "cid-compare-col cid-compare-col-focused"
              : "cid-compare-col";
            return (
              <div
                key={`blocked-${m.memberId}`}
                className={cls}
                data-member-id={m.memberId}
              >
                {m.blockedReason !== null && m.blockedReason !== undefined && (
                  <div className="cid-blocked-banner" role="alert">
                    Blocked: <code>{m.blockedReason}</code>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
      <ContextPolicyRow
        manifests={manifests}
        focusedMemberId={focusedMemberId}
      />
      <SectionRow
        title="Token estimate"
        manifests={manifests}
        focusedMemberId={focusedMemberId}
        render={(m) => <TokenEstimateSection m={m} />}
      />
      <SectionRow
        title="Source counts"
        manifests={manifests}
        focusedMemberId={focusedMemberId}
        render={(m) => <SourceCountsSection m={m} />}
      />
      <SectionRow
        title="Source refs"
        manifests={manifests}
        focusedMemberId={focusedMemberId}
        render={(m) => <SourceRefsSection m={m} />}
      />
      <SectionRow
        title="Tool results"
        manifests={manifests}
        focusedMemberId={focusedMemberId}
        render={(m) => <ToolResultsSection m={m} />}
      />
      <SectionRow
        title="Citations"
        manifests={manifests}
        focusedMemberId={focusedMemberId}
        render={(m) => <CitationRefsSection m={m} />}
      />
      <SectionRow
        title="Compaction"
        manifests={manifests}
        focusedMemberId={focusedMemberId}
        render={(m) => <CompactionSection m={m} />}
      />
      <SectionRow
        title="Council Steward"
        manifests={manifests}
        focusedMemberId={focusedMemberId}
        render={(m) => <StewardSection m={m} />}
      />
      <SectionRow
        title="Omitted"
        manifests={manifests}
        focusedMemberId={focusedMemberId}
        render={(m) => <OmittedSection m={m} />}
      />
    </div>
  );
}
