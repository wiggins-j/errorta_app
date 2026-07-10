// F007 — empty-state shown on the Corpora page when no corpora exist.
// Exposes the "Add Sample Corpus" entry point.

interface Props {
  onAddSample: () => void;
  onSkip?: () => void;
}

export default function CorporaEmptyState({ onAddSample, onSkip }: Props) {
  return (
    <div className="welcome-empty-state">
      <h2>No corpora yet.</h2>
      <p>Drop files to create your first corpus, or:</p>
      <div className="welcome-empty-cta">
        <button
          type="button"
          className="welcome-cta-primary"
          onClick={onAddSample}
        >
          Add Sample Corpus
        </button>
        {onSkip ? (
          <button
            type="button"
            className="welcome-cta-secondary"
            onClick={onSkip}
          >
            Skip — I&apos;ll add my own
          </button>
        ) : null}
      </div>
      <p className="welcome-empty-hint">
        Try Errorta with the &quot;Welcome to Errorta&quot; guide-corpus —
        Errorta&apos;s own documentation, asked of itself. Small download
        (under 5 MB). Fully deletable.
      </p>
    </div>
  );
}
