// F031 Phase 5 polish (Task 2) — ContextPolicyRow.
//
// The sticky top row of the F031-PROVENANCE-VIZ compare strip: one
// column per member, four policy fields per column. Cells whose value
// differs from the first (baseline) column's value carry the
// --accent-soft highlight + a "differs from baseline" aria-label.
//
// Pure render — no fetching, no state. The wrapping
// ContextProvenanceCompare component owns the no-diff / all-blocked
// caption and the per-column blocked banner.
import type { CouncilContextManifest } from "./types";

type PolicyField =
  | "effectiveContextAccess"
  | "effectiveTranscriptAccess"
  | "egressClass"
  | "destinationScope";

interface FieldSpec {
  key: PolicyField;
  label: string;
}

const FIELDS: FieldSpec[] = [
  { key: "effectiveContextAccess", label: "Effective context access" },
  { key: "effectiveTranscriptAccess", label: "Effective transcript access" },
  { key: "egressClass", label: "Egress class" },
  { key: "destinationScope", label: "Destination scope" },
];

interface Props {
  manifests: CouncilContextManifest[];
  focusedMemberId?: string;
}

export default function ContextPolicyRow({
  manifests,
  focusedMemberId,
}: Props) {
  return (
    <div className="cid-compare-policy-row">
      {/* QA P2 #7 (2026-06-12): no `role="region"` on the table — the
          surrounding ContextProvenanceCompare wrapper already supplies
          a region role, and an explicit role here would override the
          native table role and lose row/column semantics for assistive
          tech. The aria-label still announces the table's purpose. */}
      <table
        className="cid-compare-policy-table"
        aria-label="Per-member context policy comparison"
      >
        <thead>
          <tr>
            {manifests.map((m) => {
              const focused = m.memberId === focusedMemberId;
              const cls = focused
                ? "cid-compare-cell cid-compare-col-focused"
                : "cid-compare-cell";
              return (
                <th
                  key={m.memberId}
                  className={cls}
                  data-member-id={m.memberId}
                  scope="col"
                >
                  <code>{m.memberId}</code>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {FIELDS.map((field) => {
            const baseline = manifests[0]?.[field.key];
            const values = manifests.map((m) => m[field.key]);
            const differs = new Set(values).size > 1;
            return (
              <tr key={field.key}>
                {manifests.map((m) => {
                  const value = m[field.key];
                  const isDiffer = differs && value !== baseline;
                  const focused = m.memberId === focusedMemberId;
                  const parts = ["cid-compare-cell"];
                  if (isDiffer) parts.push("cid-compare-cell-differs");
                  if (focused) parts.push("cid-compare-col-focused");
                  const ariaLabel = isDiffer
                    ? `${field.label} for ${m.memberId} differs from baseline: ${value}`
                    : undefined;
                  return (
                    <td
                      key={`${field.key}-${m.memberId}`}
                      className={parts.join(" ")}
                      data-field={field.key}
                      data-member-id={m.memberId}
                      aria-label={ariaLabel}
                    >
                      <span className="cid-compare-field-label">
                        {field.label}:
                      </span>{" "}
                      {String(value)}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
