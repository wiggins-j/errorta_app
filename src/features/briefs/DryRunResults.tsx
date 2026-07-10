// F008 VALIDATE-UI track — per-source dry-run projection cards.
//
// Renders one card per source from a ValidateResponse.dry_run_projection map.
// Each card shows pass% and fail% with a color-coded threshold band, and a
// collapsible list of sample refusal reasons for diagnosis.
import { useState } from "react";
import type { DryRunSourceProjection } from "../../lib/api/briefs";

interface Props {
  projections: Record<string, DryRunSourceProjection> | null | undefined;
}

type Band = "good" | "warn" | "bad";

function band(passPct: number): Band {
  if (passPct >= 80) return "good";
  if (passPct >= 40) return "warn";
  return "bad";
}

function colorForBand(b: Band): string {
  // Inline colors so this works without a CSS additions (briefs.css not in
  // the allowed-modify list for this track).
  if (b === "good") return "#1b8f3a";
  if (b === "warn") return "#b88217";
  return "#b3261e";
}

function ProjectionCard({
  sourceName,
  projection,
}: {
  sourceName: string;
  projection: DryRunSourceProjection;
}) {
  const [open, setOpen] = useState<boolean>(false);
  const { candidates_seen, compliance_pass, compliance_refused, connector_name } =
    projection;
  const denom = candidates_seen > 0 ? candidates_seen : 1;
  const passPct = (compliance_pass / denom) * 100;
  const failPct = (compliance_refused / denom) * 100;
  const passBand = band(passPct);
  const passColor = colorForBand(passBand);
  const failColor = colorForBand(band(100 - failPct));
  const hasReasons = projection.sample_refusal_reasons.length > 0;

  return (
    <div
      className="briefs-dryrun-card"
      style={{
        border: "1px solid var(--border, #ccc)",
        borderRadius: 6,
        padding: "0.6rem 0.8rem",
        marginBottom: "0.5rem",
      }}
      data-testid={`dryrun-card-${sourceName}`}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: "0.5rem" }}>
        <strong>{sourceName}</strong>
        <span className="briefs-list-item-meta">{connector_name}</span>
      </div>
      <div className="briefs-list-item-meta" style={{ marginTop: "0.25rem" }}>
        {candidates_seen} candidate{candidates_seen === 1 ? "" : "s"} sampled
      </div>
      <div
        style={{
          display: "flex",
          gap: "1rem",
          marginTop: "0.4rem",
          fontVariantNumeric: "tabular-nums",
        }}
      >
        <span style={{ color: passColor }} data-testid={`dryrun-pass-${sourceName}`}>
          pass {passPct.toFixed(0)}% ({compliance_pass})
        </span>
        <span style={{ color: failColor }} data-testid={`dryrun-fail-${sourceName}`}>
          fail {failPct.toFixed(0)}% ({compliance_refused})
        </span>
      </div>
      {hasReasons && (
        <div style={{ marginTop: "0.4rem" }}>
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            data-testid={`dryrun-toggle-${sourceName}`}
            style={{
              background: "none",
              border: "none",
              padding: 0,
              cursor: "pointer",
              textDecoration: "underline",
              font: "inherit",
              color: "inherit",
            }}
          >
            {open ? "Hide" : "Show"} sample refusal reasons (
            {projection.sample_refusal_reasons.length})
          </button>
          {open && (
            <ul
              style={{ marginTop: "0.3rem", marginBottom: 0, paddingLeft: "1.2rem" }}
              data-testid={`dryrun-reasons-${sourceName}`}
            >
              {projection.sample_refusal_reasons.map((reason, idx) => (
                <li key={idx} className="briefs-list-item-meta">
                  {reason}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

export default function DryRunResults({ projections }: Props) {
  if (!projections || Object.keys(projections).length === 0) {
    return null;
  }
  return (
    <div className="briefs-dryrun-results" aria-label="Dry-run projection">
      <h4 style={{ margin: "0 0 0.4rem 0" }}>Dry-run projection</h4>
      {Object.entries(projections).map(([name, projection]) => (
        <ProjectionCard key={name} sourceName={name} projection={projection} />
      ))}
    </div>
  );
}
