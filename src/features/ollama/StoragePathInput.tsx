import { useEffect, useState } from "react";

interface Props {
  value: string | null;
  onSave: (next: string | null) => Promise<void> | void;
  disabled?: boolean;
}

export default function StoragePathInput({ value, onSave, disabled }: Props) {
  const [draft, setDraft] = useState(value ?? "");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setDraft(value ?? "");
  }, [value]);

  async function commit() {
    setErr(null);
    setSaving(true);
    try {
      const trimmed = draft.trim();
      await onSave(trimmed === "" ? null : trimmed);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  const dirty = (draft.trim() || null) !== (value || null);

  return (
    <label style={{ display: "block", marginTop: 12 }}>
      <span style={{ display: "block", fontWeight: 600 }}>Model storage path</span>
      <span style={{ display: "block", fontSize: 12, opacity: 0.7 }}>
        Where Ollama keeps weights (OLLAMA_MODELS). Leave blank for the platform default.
      </span>
      <div style={{ display: "flex", gap: 8, marginTop: 6 }}>
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          disabled={disabled || saving}
          placeholder="/Volumes/Models/ollama"
          style={{ flex: 1, fontFamily: "monospace" }}
        />
        <button type="button" onClick={commit} disabled={disabled || saving || !dirty}>
          {saving ? "Saving…" : "Save"}
        </button>
      </div>
      {err ? <p style={{ color: "#d04848" }}>{err}</p> : null}
      <p style={{ fontSize: 12, opacity: 0.6, marginTop: 6 }}>
        Note: only applied to Errorta-managed Ollama installs, and only after
        Ollama is restarted (Errorta passes this as OLLAMA_MODELS on next
        start). If Ollama was installed outside Errorta, set OLLAMA_MODELS in
        your shell or launchd environment instead.
      </p>
    </label>
  );
}
