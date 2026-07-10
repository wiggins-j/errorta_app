// F008 Briefs — left-rail list of briefs with state badge pills.
//
// F014-LIB: adds a "Templates" button to the list header. The button opens
// CreateBriefModal preselected for the template picker flow. Because the
// modal's submit handler calls the sidecar directly, BriefsList does not
// itself reload the parent's brief list — the parent (BriefsFeature) owns
// that, so an optional `onBriefCreated` callback bubbles the new id back
// when it's provided. When the callback is not provided we still select
// the new brief locally via `onSelect` so the user lands on it.
import { useState } from "react";
import type { BriefSummary } from "../../lib/api/briefs";
import type { BriefStateValue } from "./types";
import CreateBriefModal from "./CreateBriefModal";
import ImportBriefButton from "./ImportBriefButton";
import ImportBundleButton from "./ImportBundleButton";
import "../../styles/badges.css";

interface Props {
  briefs: BriefSummary[];
  selectedId: string | null;
  onSelect: (briefId: string) => void;
  /**
   * Optional. When provided, called with the new brief_id after the
   * Templates picker successfully creates a brief. Lets the parent reload
   * its summary list. When omitted, BriefsList still calls `onSelect(id)`
   * so the user lands on the new brief.
   */
  onBriefCreated?: (briefId: string, corpusName?: string) => void;
  initialCorpusName?: string;
}

/**
 * Map a brief lifecycle state to one of the existing `.pin-*` badge tokens
 * defined in src/styles/badges.css. Reusing those classes avoids hard-coding
 * any new color literals here.
 */
export function stateBadgeClass(state: BriefStateValue): string {
  switch (state) {
    case "RUNNING":
    case "VALIDATING":
    case "DRAFT":
      return "pin-editable";
    case "COMPLETED":
      return "pin-pinned";
    case "FAILED":
    case "ARCHIVED":
    case "PAUSED":
      return "pin-absent";
    default:
      return "pin-editable";
  }
}

export default function BriefsList({
  briefs,
  selectedId,
  onSelect,
  onBriefCreated,
  initialCorpusName,
}: Props) {
  const [showTemplates, setShowTemplates] = useState<boolean>(false);

  const handleCreated = (briefId: string, corpusName?: string) => {
    setShowTemplates(false);
    if (onBriefCreated) onBriefCreated(briefId, corpusName);
    onSelect(briefId);
  };

  const header = (
    <div className="briefs-list-header">
      <button
        type="button"
        className="briefs-list-templates-btn"
        onClick={() => setShowTemplates(true)}
      >
        Templates
      </button>
      <ImportBriefButton onCreated={handleCreated} />
      <ImportBundleButton onCreated={handleCreated} />
    </div>
  );

  if (briefs.length === 0) {
    return (
      <>
        {header}
        <div className="briefs-empty">
          No briefs yet. Create your first brief to start corpus collection.
        </div>
      {showTemplates && (
        <CreateBriefModal
          onCreated={handleCreated}
          onCancel={() => setShowTemplates(false)}
          initialCorpusName={initialCorpusName}
        />
      )}
      </>
    );
  }
  return (
    <>
      {header}
      <ul className="briefs-list">
        {briefs.map((b) => {
          const isActive = b.brief_id === selectedId;
          return (
            <li key={b.brief_id}>
              <button
                type="button"
                className={`briefs-list-item${isActive ? " briefs-list-item-active" : ""}`}
                onClick={() => onSelect(b.brief_id)}
                aria-current={isActive ? "true" : undefined}
              >
                <span className="briefs-list-item-name">{b.corpus_name}</span>
                <span className="briefs-list-item-meta">
                  <span className={`pin-badge ${stateBadgeClass(b.state)}`}>{b.state}</span>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      {showTemplates && (
        <CreateBriefModal
          onCreated={handleCreated}
          onCancel={() => setShowTemplates(false)}
          initialCorpusName={initialCorpusName}
        />
      )}
    </>
  );
}
