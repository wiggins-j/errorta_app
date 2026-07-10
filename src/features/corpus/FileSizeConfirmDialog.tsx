export default function FileSizeConfirmDialog({
  files,
  onConfirm,
  onCancel,
}: {
  files: { name: string; size: number }[];
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="modal" role="dialog" aria-labelledby="large-file-title">
      <h2 id="large-file-title">Large files detected</h2>
      <p>
        These files are larger than 100 MB. Ingestion may take a while and use
        significant memory. Continue?
      </p>
      <ul>
        {files.map((f) => (
          <li key={f.name}>
            {f.name} — {(f.size / (1024 * 1024)).toFixed(1)} MB
          </li>
        ))}
      </ul>
      <div className="modal-actions">
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
        <button type="button" onClick={onConfirm}>
          Ingest anyway
        </button>
      </div>
    </div>
  );
}
