// F008 Briefs — action buttons gated by the FSM in errorta_briefs/lifecycle.py.
import type { BriefStateValue } from "./types";
import { LIFECYCLE_TRANSITIONS } from "./types";
import ExportBriefButton from "./ExportBriefButton";

interface Props {
  state: BriefStateValue;
  briefId: string;
  markdown: string;
  onValidate: () => void;
  onValidatePreview?: () => void;
  onRun: () => void;
  onPause: () => void;
  onRefresh: () => void;
  onArchive: () => void;
  onOpenCorpus?: () => void;
  onExported?: (info: { slug: string; dir: string }) => void;
  busy?: boolean;
}

/**
 * Render the button set legal for the current state per LIFECYCLE_TRANSITIONS.
 * ARCHIVED is terminal — only Export remains so users can still extract the
 * markdown after the brief has been retired.
 */
export default function BriefControls({
  state,
  briefId,
  markdown,
  onValidate,
  onValidatePreview,
  onRun,
  onPause,
  onRefresh,
  onArchive,
  onOpenCorpus,
  onExported,
  busy,
}: Props) {
  const allowed = LIFECYCLE_TRANSITIONS[state];

  // ARCHIVED is terminal: no FSM actions, but Export still works.
  if (allowed.size === 0) {
    return (
      <div className="briefs-controls">
        <span className="briefs-list-item-meta">Archived — no further actions.</span>
        <ExportBriefButton
          briefId={briefId}
          markdown={markdown}
          disabled={busy}
          onExported={onExported}
        />
        {onOpenCorpus ? (
          <button type="button" onClick={onOpenCorpus} disabled={busy}>
            Open corpus
          </button>
        ) : null}
      </div>
    );
  }

  const canValidate = allowed.has("VALIDATING"); // DRAFT
  const canRun = allowed.has("RUNNING"); // VALIDATING/PAUSED/COMPLETED/FAILED via DRAFT path
  const canPause = state === "RUNNING" && allowed.has("PAUSED");
  const canArchive = allowed.has("ARCHIVED"); // PAUSED/COMPLETED/FAILED
  const canRefresh = state === "COMPLETED";

  return (
    <div className="briefs-controls">
      {canValidate && (
        <button type="button" onClick={onValidate} disabled={busy}>
          Validate
        </button>
      )}
      {canValidate && onValidatePreview && (
        <button type="button" onClick={onValidatePreview} disabled={busy}>
          Validate (preview)
        </button>
      )}
      {canRun && (
        <button type="button" className="primary" onClick={onRun} disabled={busy}>
          {state === "PAUSED" ? "Resume" : "Run"}
        </button>
      )}
      <ExportBriefButton
        briefId={briefId}
        markdown={markdown}
        disabled={busy}
        onExported={onExported}
      />
      {onOpenCorpus ? (
        <button type="button" onClick={onOpenCorpus} disabled={busy}>
          Open corpus
        </button>
      ) : null}
      {canPause && (
        <button type="button" onClick={onPause} disabled={busy}>
          Pause
        </button>
      )}
      {canRefresh && (
        <button type="button" onClick={onRefresh} disabled={busy}>
          Refresh
        </button>
      )}
      {canArchive && (
        <button type="button" onClick={onArchive} disabled={busy}>
          Archive
        </button>
      )}
    </div>
  );
}
