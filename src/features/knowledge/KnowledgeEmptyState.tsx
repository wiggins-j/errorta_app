import { navigateKnowledge } from "../../lib/featureNavigation";

interface Props {
  onCreateLocal?: () => void;
  /** F134 — open the Quick Start guide. When omitted, the prominent
   * "New here?" offer is not shown (the header control still opens it). */
  onOpenQuickStart?: () => void;
  /** F134 — hide the prominent offer permanently. */
  onDismissQuickStart?: () => void;
  /** F134 — whether the prominent offer has been dismissed already. */
  quickStartDismissed?: boolean;
}

export default function KnowledgeEmptyState({
  onCreateLocal,
  onOpenQuickStart,
  onDismissQuickStart,
  quickStartDismissed,
}: Props) {
  return (
    <div className="knowledge-empty">
      {onOpenQuickStart && !quickStartDismissed ? (
        <div className="knowledge-quickstart-offer">
          <button
            type="button"
            className="knowledge-quickstart-offer-open"
            onClick={onOpenQuickStart}
          >
            <span className="knowledge-empty-icon" aria-hidden="true">
              ?
            </span>
            <span>
              <strong>New here? Read the 2-minute Quick Start.</strong>
              <small>
                How corpora work, and how to build one from files, a brief, or a
                folder.
              </small>
            </span>
          </button>
          {onDismissQuickStart ? (
            <button
              type="button"
              className="knowledge-quickstart-offer-dismiss"
              onClick={onDismissQuickStart}
              aria-label="Dismiss the Quick Start guide"
            >
              Dismiss
            </button>
          ) : null}
        </div>
      ) : null}
      <section
        className="knowledge-empty-workbench"
        aria-label="Create a corpus"
      >
        <button type="button" onClick={onCreateLocal}>
          <span className="knowledge-empty-icon" aria-hidden="true">
            +
          </span>
          <span>
            <strong>Upload files</strong>
            <small>Create a local corpus from documents on this machine.</small>
          </span>
        </button>
        <button
          type="button"
          onClick={() => navigateKnowledge({ feature: "briefs" })}
        >
          <span className="knowledge-empty-icon" aria-hidden="true">
            #
          </span>
          <span>
            <strong>Build from brief</strong>
            <small>Use a reproducible source plan to collect documents.</small>
          </span>
        </button>
        <button
          type="button"
          onClick={() => navigateKnowledge({ feature: "watch" })}
        >
          <span className="knowledge-empty-icon" aria-hidden="true">
            ^
          </span>
          <span>
            <strong>Watch folder</strong>
            <small>Keep a local corpus updated from a folder.</small>
          </span>
        </button>
      </section>
    </div>
  );
}
