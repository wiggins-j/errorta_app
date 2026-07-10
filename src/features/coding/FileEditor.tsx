// F105 — in-app file editor for the Coding "Files touched" panel.
//
// Edits the project's canonical source on the internal `master` (D1) — the same
// content the read-only viewer showed. CodeMirror and its language grammars are
// imported only from inside this module so they stay in the lazy Coding chunk
// (D4); grammars load per-extension via dynamic import.
import { useCallback, useEffect, useMemo, useState } from "react";
import type { Extension } from "@codemirror/state";
import CodeMirror from "@uiw/react-codemirror";

import {
  CodingFileUpdateError,
  getFile,
  updateFile,
  type CodingFile,
} from "../../lib/api/coding";

export interface FileEditorProps {
  projectId: string;
  /** The file as last loaded (its content/sha seed the editor). */
  file: CodingFile;
  /** True while a Coding run holds the worktree — save is disabled. */
  running: boolean;
  /** Called after a successful save so the parent can refresh artifacts/decisions. */
  onSaved: () => void;
}

// Per-extension language grammar, loaded on demand so unused grammars never land
// in the eager Coding chunk. Unknown extensions fall back to a plain editor.
async function loadLanguageExtension(path: string): Promise<Extension | null> {
  const ext = (path.split(".").pop() ?? "").toLowerCase();
  switch (ext) {
    case "ts":
    case "tsx":
    case "js":
    case "jsx":
    case "mjs":
    case "cjs": {
      const m = await import("@codemirror/lang-javascript");
      return m.javascript({ jsx: ext === "jsx" || ext === "tsx", typescript: ext.startsWith("ts") });
    }
    case "py": {
      const m = await import("@codemirror/lang-python");
      return m.python();
    }
    case "json": {
      const m = await import("@codemirror/lang-json");
      return m.json();
    }
    case "md":
    case "markdown": {
      const m = await import("@codemirror/lang-markdown");
      return m.markdown();
    }
    case "html":
    case "htm": {
      const m = await import("@codemirror/lang-html");
      return m.html();
    }
    case "css": {
      const m = await import("@codemirror/lang-css");
      return m.css();
    }
    default:
      return null;
  }
}

export default function FileEditor({ projectId, file, running, onSaved }: FileEditorProps) {
  // The committed-master baseline the editor was seeded from. Updated after a
  // successful save (and after a reload of a stale file).
  const [loadedContent, setLoadedContent] = useState<string>(file.content ?? "");
  const [contentSha256, setContentSha256] = useState<string | null>(file.contentSha256 ?? null);
  const [draft, setDraft] = useState<string>(file.content ?? "");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [langExt, setLangExt] = useState<Extension[]>([]);

  // Re-seed when the parent hands a different file (or a fresh load of the same).
  useEffect(() => {
    setLoadedContent(file.content ?? "");
    setContentSha256(file.contentSha256 ?? null);
    setDraft(file.content ?? "");
    setSaveError(null);
    setStale(false);
  }, [file.path, file.content, file.contentSha256]);

  // Load the language grammar for this file's extension (lazy, per-extension).
  useEffect(() => {
    let cancelled = false;
    void loadLanguageExtension(file.path).then((ext) => {
      if (!cancelled) setLangExt(ext ? [ext] : []);
    });
    return () => {
      cancelled = true;
    };
  }, [file.path]);

  const dirty = draft !== loadedContent;

  // A file is editable only when it's plain utf-8 text, fully loaded (not
  // truncated), on master, and the GET gave us a concurrency token.
  const editable =
    file.onMaster &&
    file.encoding === "utf-8" &&
    !file.truncated &&
    contentSha256 != null;

  const saveDisabled = !editable || !dirty || running || saving || stale;

  const onRevert = useCallback(() => {
    setDraft(loadedContent);
    setSaveError(null);
  }, [loadedContent]);

  const onCopy = useCallback(() => {
    if (navigator.clipboard) void navigator.clipboard.writeText(draft);
  }, [draft]);

  const onReload = useCallback(() => {
    setReloading(true);
    setSaveError(null);
    void getFile(projectId, file.path)
      .then((fresh) => {
        setLoadedContent(fresh.content ?? "");
        setContentSha256(fresh.contentSha256 ?? null);
        setDraft(fresh.content ?? "");
        setStale(false);
      })
      .catch((err: unknown) => {
        setSaveError(err instanceof Error ? err.message : "Could not reload the file.");
      })
      .finally(() => setReloading(false));
  }, [projectId, file.path]);

  const onSave = useCallback(() => {
    if (saveDisabled || contentSha256 == null) return;
    setSaving(true);
    setSaveError(null);
    void updateFile(projectId, file.path, draft, contentSha256)
      .then((res) => {
        setLoadedContent(draft);
        setContentSha256(res.contentSha256);
        setStale(false);
        onSaved();
      })
      .catch((err: unknown) => {
        if (err instanceof CodingFileUpdateError && err.reason === "stale_file") {
          setStale(true);
          setSaveError(err.message);
        } else {
          setSaveError(err instanceof Error ? err.message : "Could not save the file.");
        }
      })
      .finally(() => setSaving(false));
  }, [saveDisabled, contentSha256, projectId, file.path, draft, onSaved]);

  const editorExtensions = useMemo(() => langExt, [langExt]);

  return (
    <div className="coding-file-editor">
      <p className="coding-file-note coding-file-editor-hint">
        Edits the project&apos;s canonical source (the internal master). An
        already-delivered folder is a snapshot — re-deliver to refresh it.
      </p>
      <CodeMirror
        className="coding-file-cm"
        value={draft}
        height="22rem"
        editable={editable && !running}
        readOnly={!editable || running}
        extensions={editorExtensions}
        onChange={(value) => setDraft(value)}
        aria-label={`Edit file ${file.path}`}
        basicSetup={{ lineNumbers: true, foldGutter: false }}
      />
      <p className="coding-file-dirty" aria-live="polite">
        {dirty ? "Unsaved changes" : "No unsaved changes"}
      </p>
      {stale ? (
        <div className="coding-file-stale" role="alert">
          <span>
            This file changed since you opened it. Reload to get the latest, then
            re-apply your edit.
          </span>
          <button
            type="button"
            className="coding-btn coding-btn-small"
            onClick={onReload}
            disabled={reloading}
          >
            {reloading ? "Reloading…" : "Reload"}
          </button>
        </div>
      ) : null}
      {saveError && !stale ? (
        <p className="coding-file-note coding-file-error" role="alert">
          {saveError}
        </p>
      ) : null}
      <div className="coding-file-actions coding-file-editor-actions">
        <button
          type="button"
          className="coding-btn coding-btn-small coding-btn-accept"
          onClick={onSave}
          disabled={saveDisabled}
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          className="coding-btn coding-btn-small"
          onClick={onRevert}
          disabled={!dirty || saving}
        >
          Revert
        </button>
        <button type="button" className="coding-btn coding-btn-small" onClick={onCopy}>
          Copy
        </button>
      </div>
      {running ? (
        <p className="coding-file-note">Saving is disabled while a Coding run is active.</p>
      ) : null}
    </div>
  );
}
