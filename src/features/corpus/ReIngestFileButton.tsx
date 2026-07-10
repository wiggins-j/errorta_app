import { useState } from "react";

export default function ReIngestFileButton({
  onReingest,
}: {
  onReingest: () => Promise<void> | void;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <button
      type="button"
      className="row-action"
      disabled={busy}
      title="Re-ingest"
      onClick={async () => {
        setBusy(true);
        try {
          await onReingest();
        } finally {
          setBusy(false);
        }
      }}
    >
      Re-ingest
    </button>
  );
}
