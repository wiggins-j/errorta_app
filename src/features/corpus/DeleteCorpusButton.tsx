import { useState } from "react";

/**
 * F114 — delete a whole corpus, behind an explicit inline confirm. Mirrors the
 * per-file `DeleteFileButton` UX, but a corpus delete is irreversible so it
 * gates on a confirmation step before calling `onDelete`.
 */
export default function DeleteCorpusButton({
  name,
  onDelete,
}: {
  name: string;
  onDelete: () => Promise<void> | void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  if (confirming) {
    return (
      <span
        className="corpus-delete-confirm"
        role="alertdialog"
        aria-label={`Delete corpus ${name}`}
      >
        <span className="corpus-delete-confirm-text">
          Delete corpus &lsquo;{name}&rsquo; and all its files? This can&apos;t be
          undone.
        </span>
        <button
          type="button"
          className="row-action danger"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              await onDelete();
            } finally {
              setBusy(false);
              setConfirming(false);
            }
          }}
        >
          Confirm delete
        </button>
        <button
          type="button"
          className="row-action"
          disabled={busy}
          onClick={() => setConfirming(false)}
        >
          Cancel
        </button>
      </span>
    );
  }

  return (
    <button
      type="button"
      className="row-action"
      title={`Delete corpus ${name}`}
      onClick={() => setConfirming(true)}
    >
      Delete corpus
    </button>
  );
}
