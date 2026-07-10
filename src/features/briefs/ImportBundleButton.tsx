// BUNDLE-IMPORT — restore a brief bundle (.tar.gz) by uploading it to the
// sidecar's /briefs/import-bundle endpoint.
//
// Mirrors the Tauri-or-browser fork in ImportBriefButton:
//   * Tauri shell  → native open dialog (tar.gz / tgz filter) + fs.readFile
//   * Plain browser → hidden <input type="file">
//
// 409 conflict path: surfaces a banner with an "Import with new id" affordance.
// That affordance re-POSTs the same file with ``rename_to`` set, so the
// happy-path round-trips even when the originating brief_id collides.

import { useRef, useState, type ChangeEvent } from "react";
import { importBundle, type BriefImportResult } from "../../lib/api/briefs";

interface Props {
  /** Called with the new brief_id after a successful import. */
  onCreated: (briefId: string) => void;
  /** Optional layout class. */
  className?: string;
  /** Optional label override (defaults to "Import bundle"). */
  label?: string;
  /** Corpus name to import the brief under. Defaults to "default". */
  corpusName?: string;
}

interface TauriInternals {
  __TAURI_INTERNALS__?: unknown;
}

function isTauri(): boolean {
  if (typeof window === "undefined") return false;
  return Boolean((window as TauriInternals).__TAURI_INTERNALS__);
}

interface ImportError extends Error {
  status?: number;
  body?: unknown;
}

function readableMessage(err: unknown): string {
  if (!(err instanceof Error)) return String(err);
  const body = (err as ImportError).body;
  if (body && typeof body === "object") {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object") {
      const msg = (detail as { message?: unknown }).message;
      if (typeof msg === "string") return msg;
    }
  }
  // Strip the "HTTP NNN on /path: " prefix request() adds.
  const m = err.message.match(/^HTTP \d+ on [^:]+: (.*)$/s);
  return (m ? m[1] : err.message) || err.message;
}

function isConflict(err: unknown): boolean {
  if (!(err instanceof Error)) return false;
  return (err as ImportError).status === 409;
}

function extractConflictBriefId(err: unknown): string | null {
  if (!(err instanceof Error)) return null;
  const body = (err as ImportError).body;
  if (body && typeof body === "object") {
    const detail = (body as { detail?: unknown }).detail;
    if (detail && typeof detail === "object") {
      const bid = (detail as { brief_id?: unknown }).brief_id;
      if (typeof bid === "string") return bid;
    }
  }
  return null;
}

export default function ImportBundleButton({
  onCreated,
  className,
  label = "Import bundle",
  corpusName,
}: Props) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  // When a 409 lands, we cache the file blob + the conflicting id so the
  // user can hit "Import with new id" without re-picking the file.
  const [conflictFile, setConflictFile] = useState<File | Blob | null>(null);
  const [conflictId, setConflictId] = useState<string | null>(null);

  const handleResult = (result: BriefImportResult) => {
    setError(null);
    setConflictFile(null);
    setConflictId(null);
    onCreated(result.brief_id);
  };

  const postBundle = async (file: File | Blob, renameTo?: string) => {
    setLoading(true);
    try {
      const result = await importBundle(file, {
        renameTo,
        corpusName,
      });
      handleResult(result);
    } catch (err) {
      if (isConflict(err)) {
        setConflictFile(file);
        setConflictId(extractConflictBriefId(err));
        setError(readableMessage(err));
      } else {
        setConflictFile(null);
        setConflictId(null);
        setError(readableMessage(err));
      }
    } finally {
      setLoading(false);
    }
  };

  const openTauriDialog = async () => {
    const dialog = await import("@tauri-apps/plugin-dialog");
    const fs = await import("@tauri-apps/plugin-fs");
    const selected = await dialog.open({
      multiple: false,
      filters: [
        { name: "Errorta brief bundle", extensions: ["tar.gz", "tgz"] },
      ],
    });
    if (selected === null || selected === undefined) return;
    const path = Array.isArray(selected) ? selected[0] : selected;
    if (!path || typeof path !== "string") return;
    const fsAny = fs as unknown as {
      readFile?: (p: string) => Promise<Uint8Array>;
      readBinaryFile?: (p: string) => Promise<Uint8Array>;
    };
    const reader = fsAny.readFile ?? fsAny.readBinaryFile;
    if (!reader) {
      throw new Error("Tauri fs plugin: readFile/readBinaryFile unavailable");
    }
    const bytes = await reader(path);
    const name = path.split(/[\\/]/).pop() || "bundle.tar.gz";
    // Copy through a fresh ArrayBuffer so the File constructor's BlobPart
    // typing is satisfied regardless of the source ArrayBufferLike variant.
    const buf = new ArrayBuffer(bytes.byteLength);
    new Uint8Array(buf).set(bytes);
    const blob = new File([buf], name, { type: "application/gzip" });
    await postBundle(blob);
  };

  const openBrowserPicker = () => {
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
      fileInputRef.current.click();
    }
  };

  const onBrowserFileSelected = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    await postBundle(file);
  };

  const onClick = async () => {
    setError(null);
    setConflictFile(null);
    setConflictId(null);
    try {
      if (isTauri()) {
        await openTauriDialog();
      } else {
        openBrowserPicker();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setLoading(false);
    }
  };

  const onRetryWithRename = async () => {
    if (!conflictFile) return;
    // Suggest a unique id by appending a short timestamp suffix. The user can
    // refine later; the point of the affordance is to unblock the import.
    const base = conflictId ?? "imported-brief";
    const stamp = new Date()
      .toISOString()
      .replace(/[-:.TZ]/g, "")
      .slice(0, 14);
    const renameTo = `${base}-${stamp}`;
    await postBundle(conflictFile, renameTo);
  };

  return (
    <>
      <button
        type="button"
        className={className ?? "briefs-list-import-btn"}
        onClick={onClick}
        disabled={loading}
        aria-label="Import brief bundle from file"
      >
        {loading ? "Importing…" : label}
      </button>
      <input
        ref={fileInputRef}
        type="file"
        accept=".tar.gz,.tgz,application/gzip"
        style={{ display: "none" }}
        onChange={onBrowserFileSelected}
        aria-hidden="true"
        data-testid="import-bundle-file-input"
      />
      {error && (
        <div className="briefs-parse-banner" role="alert">
          <div>{error}</div>
          {conflictFile && (
            <button
              type="button"
              onClick={onRetryWithRename}
              disabled={loading}
              aria-label="Import with new id"
            >
              Import with new id
            </button>
          )}
        </div>
      )}
    </>
  );
}
