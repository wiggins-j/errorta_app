// F047 — import a declarative Council profile as a draft room (with preview).
//
// Paste YAML (or load a bundled example), Preview to validate against the live
// providers/tools, review warnings (unavailable providers, requested tools),
// then create a DRAFT room. Nothing runs automatically.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  listProfileExamples,
  validateProfile,
  type ProfileExample,
  type ProfileValidateResult,
} from "../../lib/api/councilProfile";
import { createRoom } from "../../lib/api/councilRoom";

interface Props {
  onClose: () => void;
  onCreated: (roomId: string) => void;
}

export default function CouncilProfileImport({ onClose, onCreated }: Props) {
  const [yamlText, setYamlText] = useState("");
  const [examples, setExamples] = useState<ProfileExample[]>([]);
  const [preview, setPreview] = useState<ProfileValidateResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    listProfileExamples().then(setExamples).catch(() => setExamples([]));
  }, []);

  // Initial focus + Esc-to-close (matches the Council modal a11y pattern).
  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCloseRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const doPreview = useCallback(async () => {
    setBusy(true);
    setError(null);
    setPreview(null);
    try {
      const result = await validateProfile({ yaml: yamlText });
      setPreview(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [yamlText]);

  const doCreate = useCallback(async () => {
    if (!preview) return;
    setBusy(true);
    setError(null);
    try {
      const resp = await createRoom(preview.room);
      onCreated(String((resp.room as { id?: string }).id ?? ""));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [preview, onCreated]);

  const v = preview?.validation;
  const members = (preview?.room?.members as Array<{ id: string; name?: string }>) ?? [];

  return (
    <div className="profile-import" role="dialog" aria-label="Import council profile">
      <header className="profile-import-head">
        <h3>Import council profile</h3>
        <button type="button" onClick={onClose} ref={closeRef}>
          Close
        </button>
      </header>

      {examples.length > 0 && (
        <div className="profile-import-examples">
          <span>Start from an example:</span>
          {examples.map((ex) => (
            <button
              key={ex.slug}
              type="button"
              onClick={() => {
                setYamlText(ex.yaml);
                setPreview(null);
              }}
              data-testid={`example-${ex.slug}`}
            >
              {ex.slug}
            </button>
          ))}
        </div>
      )}

      <label className="profile-import-field">
        <span>Profile YAML</span>
        <textarea
          value={yamlText}
          onChange={(e) => {
            setYamlText(e.target.value);
            setPreview(null);
          }}
          rows={12}
          aria-label="Profile YAML"
          data-testid="profile-yaml"
        />
      </label>

      <div className="profile-import-actions">
        <button
          type="button"
          onClick={doPreview}
          disabled={busy || !yamlText.trim()}
          data-testid="preview-profile"
        >
          Preview
        </button>
      </div>

      {error && <p className="profile-import-error" role="alert">{error}</p>}

      {preview && v && (
        <section className="profile-import-preview" aria-label="Profile preview">
          <h4>{String(preview.room.name ?? "Imported council")}</h4>
          <p className="profile-import-desc">
            {String(preview.room.description ?? "")}
          </p>
          <p>
            <strong>{members.length}</strong> member
            {members.length === 1 ? "" : "s"}:{" "}
            {members.map((m) => m.name || m.id).join(", ")}
          </p>
          {v.requested_tools.length > 0 && (
            <p>Requested tools: {v.requested_tools.join(", ")}</p>
          )}
          {v.warnings.length > 0 && (
            <ul className="profile-import-warnings">
              {v.warnings.map((w, i) => (
                <li key={i}>⚠ {w.detail}</li>
              ))}
            </ul>
          )}
          <button
            type="button"
            onClick={doCreate}
            disabled={busy}
            data-testid="create-draft"
          >
            Create draft room
          </button>
          <p className="profile-import-note">
            Creates a draft room only — nothing runs until you start it.
          </p>
        </section>
      )}
    </div>
  );
}
