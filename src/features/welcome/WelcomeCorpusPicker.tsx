// F007 — consent modal listing the available welcome corpora (v0.1: one).
import type { WelcomeOption } from "./types";

interface Props {
  options: WelcomeOption[];
  onConfirm: (option: WelcomeOption) => void;
  onCancel: () => void;
}

export default function WelcomeCorpusPicker({ options, onConfirm, onCancel }: Props) {
  const primary = options[0];
  return (
    <div className="welcome-modal" role="dialog" aria-modal="true">
      <div className="welcome-modal-card">
        <h3>Add a sample corpus</h3>
        {primary ? (
          <article className="welcome-modal-option">
            <h4>{primary.name}</h4>
            <p>{primary.description}</p>
            <ul className="welcome-modal-meta">
              <li>
                <strong>Source:</strong>{" "}
                <code>{primary.source_url}</code>
              </li>
              <li>
                <strong>License:</strong> {primary.license}
              </li>
              <li>
                <strong>Size:</strong> under {primary.approx_size_mb} MB
              </li>
              <li>
                {primary.fully_deletable
                  ? "Fully deletable from the standard Corpora UI."
                  : "Not deletable from the standard UI."}
              </li>
            </ul>
            <div className="welcome-modal-actions">
              <button
                type="button"
                className="welcome-cta-primary"
                onClick={() => onConfirm(primary)}
              >
                Download & ingest
              </button>
              <button
                type="button"
                className="welcome-cta-secondary"
                onClick={onCancel}
              >
                Cancel
              </button>
            </div>
          </article>
        ) : (
          <p>No welcome corpora available.</p>
        )}
      </div>
    </div>
  );
}
