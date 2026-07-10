// F-INFRA-06 — Diagnostic bundle export card.
//
// User clicks [Export bundle] → Tauri save dialog → POST /diagnostics/export →
// success toast with path + SHA-256 + file count.

import { useCallback, useState } from "react";
import { exportDiagnostics, type DiagnosticsExportResult } from "../../lib/api/diagnostics";
import { isTauriRuntime } from "../../lib/sidecarPort";

type SaveDialogModule = {
  save: (opts: {
    defaultPath?: string;
    filters?: { name: string; extensions: string[] }[];
  }) => Promise<string | null>;
};

async function loadTauriDialog(): Promise<SaveDialogModule | null> {
  if (!isTauriRuntime()) return null;
  try {
    // Bundler-resolved import (the proven `FilePickerDialog` pattern). The
    // previous `new Function("import(...)")` shim dodged the bundler, so the
    // plugin never shipped in the packaged app and Export silently no-oped.
    const mod = (await import("@tauri-apps/plugin-dialog")) as SaveDialogModule;
    return mod;
  } catch {
    return null;
  }
}

function defaultFilename(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  const stamp =
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  return `errorta-diagnostics-${stamp}.zip`;
}

type Status =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "ok"; result: DiagnosticsExportResult }
  | { kind: "error"; message: string };

export function DiagnosticsExport() {
  const [note, setNote] = useState("");
  const [status, setStatus] = useState<Status>({ kind: "idle" });

  const onExport = useCallback(async () => {
    setStatus({ kind: "running" });
    try {
      const dialog = await loadTauriDialog();
      let dest: string | null;
      if (dialog) {
        dest = await dialog.save({
          defaultPath: defaultFilename(),
          filters: [{ name: "Errorta diagnostic bundle", extensions: ["zip"] }],
        });
      } else {
        // Browser fallback (vite dev): prompt for an absolute path. The
        // sidecar rejects relative paths with 400.
        dest = window.prompt(
          "Absolute destination path for the diagnostic bundle:",
          `/tmp/${defaultFilename()}`,
        );
      }
      if (!dest) {
        setStatus({ kind: "idle" });
        return;
      }
      const result = await exportDiagnostics(dest, note);
      setStatus({ kind: "ok", result });
    } catch (e) {
      setStatus({ kind: "error", message: String(e) });
    }
  }, [note]);

  return (
    <div className="diagnostics-export">
      <label htmlFor="diagnostics-user-note">
        Note for support (optional)
      </label>
      <textarea
        id="diagnostics-user-note"
        rows={3}
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="What were you doing when the issue happened?"
      />
      <div className="shell-actions">
        <button
          type="button"
          onClick={onExport}
          disabled={status.kind === "running"}
        >
          {status.kind === "running" ? "Exporting…" : "Export bundle"}
        </button>
      </div>
      {status.kind === "ok" && (
        <div role="status" className="diagnostics-toast diagnostics-toast-ok">
          <p>Bundle written.</p>
          <p>
            <strong>Path:</strong> {status.result.path}
          </p>
          <p>
            <strong>SHA-256:</strong>{" "}
            <code>{status.result.sha256}</code>
          </p>
          <p>
            <strong>Files:</strong> {status.result.files.length}
          </p>
        </div>
      )}
      {status.kind === "error" && (
        <div role="alert" className="diagnostics-toast diagnostics-toast-err">
          {status.message}
        </div>
      )}
    </div>
  );
}

export default DiagnosticsExport;
