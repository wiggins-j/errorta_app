// v0.1 stub: password-protected PDF prompt. Actual decryption flow is deferred
// per F004 spec ("ship a 'provide password' UI per file"). This component is a
// presentational placeholder so the failure path has a real surface.

export default function PasswordProtectedPdfPrompt({
  filename,
  onCancel,
}: {
  filename: string;
  onCancel: () => void;
}) {
  return (
    <div className="modal" role="dialog" aria-labelledby="pwpdf-title">
      <h2 id="pwpdf-title">PDF is password-protected</h2>
      <p>
        <strong>{filename}</strong> requires a password to read. Password entry
        will be supported in a follow-up patch.
      </p>
      <div className="modal-actions">
        <button type="button" onClick={onCancel}>
          Close
        </button>
      </div>
    </div>
  );
}
