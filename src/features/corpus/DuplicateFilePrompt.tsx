export default function DuplicateFilePrompt({
  filenames,
  onSkip,
  onReingest,
}: {
  filenames: string[];
  onSkip: () => void;
  onReingest: () => void;
}) {
  return (
    <div className="modal" role="dialog" aria-labelledby="dup-title">
      <h2 id="dup-title">Already in this corpus</h2>
      <p>These files match an existing entry by SHA-256:</p>
      <ul>
        {filenames.map((n) => (
          <li key={n}>{n}</li>
        ))}
      </ul>
      <div className="modal-actions">
        <button type="button" onClick={onSkip}>
          Skip
        </button>
        <button type="button" onClick={onReingest}>
          Re-ingest
        </button>
      </div>
    </div>
  );
}
