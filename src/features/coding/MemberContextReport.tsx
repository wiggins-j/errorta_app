import { useCallback, useEffect, useState } from "react";

import { getTurnComposition, type TurnComposition } from "../../lib/api/coding";
import { formatTokens } from "./formatTokens";

export interface MemberContextReportProps {
  projectId: string;
  taskId: string;
  turnId: string;
  /** Optional label (member/role) shown in the header. */
  label?: string;
  /** Provide a pre-fetched composition to render synchronously (tests / drill-downs
   * that already hold the data); when omitted the component fetches it itself. */
  composition?: TurnComposition;
}

// Human labels for the Layer-1 composition taxonomy (spec §composition).
const CLASS_LABEL: Record<string, string> = {
  role_instructions: "Role instructions",
  work_request: "Task / work request",
  project_context: "Project context (retrieved)",
  repo_snapshot: "Repo snapshot",
  prior_outputs: "Prior outputs",
  pr_diff: "PR diff",
  tool_guidance: "Tool guidance",
  transcript: "Transcript",
  prompt: "Prompt",
};

function classLabel(cls: string): string {
  return CLASS_LABEL[cls] ?? cls;
}

// The retrieved project-context / corpus category is the hero of the report — the
// whole point of the RAG story — so it gets the accent treatment when present.
const HERO_CLASS = "project_context";

/**
 * F143-01 Slice F — the per-member Context Report (Layer 1).
 *
 * Shows exactly what Errorta assembled and sent into ONE member turn, broken into
 * labeled categories with a proportional bar each, plus the sent total. For a
 * CLI-backed member it appends the honest Layer-2 caveat: the vendor CLI wraps our
 * prompt in its own system prompt / tools / skills that we can't itemize, quantified
 * (when the provider reported input) by `cliOverheadTokens` and rendered as a
 * visually distinct "vendor-added, not itemized" band.
 *
 * Self-contained: fetches its own composition from the `.../composition` endpoint
 * unless one is supplied via props.
 */
export default function MemberContextReport({
  projectId,
  taskId,
  turnId,
  label,
  composition: provided,
}: MemberContextReportProps) {
  const [data, setData] = useState<TurnComposition | null>(provided ?? null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await getTurnComposition(projectId, taskId, turnId));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [projectId, taskId, turnId]);

  useEffect(() => {
    if (provided) {
      setData(provided);
      return;
    }
    void load();
  }, [provided, load]);

  if (error) {
    return (
      <div className="coding-ctxreport" role="group" aria-label="Context report">
        <p className="coding-error" role="alert">
          {error}
        </p>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="coding-ctxreport" role="group" aria-label="Context report">
        <p className="coding-empty">Loading context report…</p>
      </div>
    );
  }

  const { composition, cliOverheadTokens, note } = data;
  const sentTotal = composition.sentTotal;
  const categories = composition.categories;
  // Bar widths are proportional to the sent total (Layer-1). The CLI overhead is a
  // separate Layer-2 band and is NOT part of this denominator (we never itemize it).
  const denom = sentTotal > 0 ? sentTotal : 1;

  return (
    <div className="coding-ctxreport" role="group" aria-label="Context report">
      <p className="coding-ctxreport-head">
        <span className="coding-ctxreport-title">
          Context sent{label ? ` — ${label}` : ""}
        </span>
        <span className="coding-ctxreport-total">{formatTokens(sentTotal)} tokens</span>
      </p>

      {categories.length === 0 ? (
        <p className="coding-empty">No per-category composition recorded for this turn.</p>
      ) : (
        <ul className="coding-ctxreport-bars">
          {categories.map((cat) => {
            const pct = Math.max(1, Math.round((cat.tokens / denom) * 100));
            const isHero = cat.class_ === HERO_CLASS;
            return (
              <li
                key={cat.class_}
                className={`coding-ctxreport-row${isHero ? " coding-ctxreport-hero" : ""}`}
              >
                <span className="coding-ctxreport-label">{classLabel(cat.class_)}</span>
                <span className="coding-ctxreport-track" aria-hidden="true">
                  <span className="coding-ctxreport-fill" style={{ width: `${pct}%` }} />
                </span>
                <span className="coding-ctxreport-count">{formatTokens(cat.tokens)}</span>
              </li>
            );
          })}
        </ul>
      )}

      {note ? (
        <div className="coding-ctxreport-layer2" role="note">
          <p className="coding-ctxreport-layer2-note">{note}</p>
          {cliOverheadTokens != null ? (
            <p
              className="coding-ctxreport-layer2-band"
              title="Vendor-managed context added by the CLI on top of what Errorta sent — Errorta cannot itemize it."
            >
              + CLI-added context (vendor-managed, not itemized) ≈{" "}
              <strong>{formatTokens(cliOverheadTokens)}</strong> tokens
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
