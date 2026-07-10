// F005 — warning shown when the selected folder lives inside a known
// cloud-sync provider (Dropbox, iCloud, OneDrive, Google Drive, ...).

interface Props {
  provider: string;
  path: string;
  onContinue: () => void;
  onPickDifferent: () => void;
}

export function CloudSyncWarning({ provider, path, onContinue, onPickDifferent }: Props) {
  return (
    <div
      role="alert"
      style={{
        padding: 12,
        border: "1px solid #b8860b",
        borderRadius: 6,
        background: "#fff8e1",
        color: "#4a3500",
        margin: "8px 0",
      }}
    >
      <p style={{ margin: "0 0 8px" }}>
        <strong>Cloud-synced folder detected ({provider}).</strong>
      </p>
      <p style={{ margin: "0 0 8px", fontSize: 13 }}>
        <code>{path}</code> appears to be inside a cloud-sync directory. Errorta will
        copy files at ingest time. Sync-deleted files will be removed from your corpus
        (or marked missing, depending on your deletion policy).
      </p>
      <div style={{ display: "flex", gap: 8 }}>
        <button type="button" onClick={onContinue}>
          Continue anyway
        </button>
        <button type="button" onClick={onPickDifferent}>
          Pick a different folder
        </button>
      </div>
    </div>
  );
}
