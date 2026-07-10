import { useEffect, useState } from "react";

interface Props {
  value: string;
  onSave: (next: string) => Promise<void> | void;
  disabled?: boolean;
}

export default function CustomHostInput({ value, onSave, disabled }: Props) {
  const [draft, setDraft] = useState(value);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setDraft(value);
  }, [value]);

  async function commit() {
    setErr(null);
    setSaving(true);
    try {
      await onSave(draft.trim());
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <label style={{ display: "block", marginTop: 12 }}>
      <span style={{ display: "block", fontWeight: 600 }}>Custom Ollama host</span>
      <span style={{ display: "block", fontSize: 12, opacity: 0.7 }}>
        e.g. http://198.51.100.10:11434 — defaults to http://localhost:11434.
      </span>
      <div style={{ display: "flex", gap: 8, marginTop: 6 }}>
        <input
          type="url"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          disabled={disabled || saving}
          placeholder="http://localhost:11434"
          style={{ flex: 1, fontFamily: "monospace" }}
        />
        <button
          type="button"
          onClick={commit}
          disabled={disabled || saving || draft.trim() === value.trim()}
        >
          {saving ? "Saving…" : "Save"}
        </button>
      </div>
      {err ? <p style={{ color: "#d04848" }}>{err}</p> : null}
    </label>
  );
}
