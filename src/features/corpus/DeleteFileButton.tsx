import { useState } from "react";

export default function DeleteFileButton({
  onDelete,
}: {
  onDelete: () => Promise<void> | void;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <button
      type="button"
      className="row-action"
      disabled={busy}
      title="Delete file"
      onClick={async () => {
        setBusy(true);
        try {
          await onDelete();
        } finally {
          setBusy(false);
        }
      }}
    >
      Delete
    </button>
  );
}
