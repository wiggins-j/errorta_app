// F006 — shell-tier Ollama host field. Mirrors the form F003 will own;
// kept here so Settings has a working surface in v0.1 even before the F003
// agent lands.
import { useEffect, useState } from "react";
import * as shellApi from "../../lib/api/shell";

export function OllamaHostField() {
  const [host, setHost] = useState<string>("");
  const [original, setOriginal] = useState<string>("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    shellApi
      .getOllamaHost()
      .then((r) => {
        if (cancelled) return;
        setHost(r.host);
        setOriginal(r.host);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const dirty = host !== original;

  async function onSave() {
    setSaving(true);
    setError(null);
    try {
      const r = await shellApi.setOllamaHost(host);
      setOriginal(r.host);
      setHost(r.host);
      setSavedAt(Date.now());
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="shell-ollama-host">
      <label htmlFor="ollama-host">Ollama host</label>
      <input
        id="ollama-host"
        type="text"
        value={host}
        spellCheck={false}
        onChange={(e) => setHost(e.target.value)}
        placeholder="http://127.0.0.1:11434"
      />
      <button type="button" onClick={onSave} disabled={!dirty || saving || !host.trim()}>
        {saving ? "Saving…" : "Save"}
      </button>
      {error && <span className="shell-ollama-error">{error}</span>}
      {savedAt && !error && <span className="shell-ollama-saved">saved</span>}
    </div>
  );
}

export default OllamaHostField;
