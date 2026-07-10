import { useEffect, useRef, useState } from "react";
import {
  fetchModel,
  fetchPreflight,
  setModel,
  type PreflightResponse,
} from "../../lib/api/judge";
import SimilarityThresholdSlider from "./SimilarityThresholdSlider";
import { useToast } from "./toast";
import aiarLogo from "../../assets/aiar-logo.png";

interface Props {
  onModelChange?: (model: string | null) => void;
}

export default function JudgeModelPicker({ onModelChange }: Props) {
  const [value, setValue] = useState<string>("");
  const [source, setSource] = useState<string>("default");
  const [preflight, setPreflight] = useState<PreflightResponse | null>(null);
  const [saving, setSaving] = useState<boolean>(false);
  const toast = useToast();
  const toastRef = useRef(toast);
  toastRef.current = toast;

  const loadAll = async () => {
    try {
      const [m, p] = await Promise.all([fetchModel(), fetchPreflight()]);
      setValue(m.judge_model ?? "");
      setSource(m.source);
      setPreflight(p);
    } catch {
      // swallow — preflight is best-effort
    }
  };

  useEffect(() => {
    void loadAll();
  }, []);

  const onApply = async () => {
    setSaving(true);
    try {
      const next = await setModel(value.trim() || null);
      setSource(next.source);
      onModelChange?.(next.judge_model ?? null);
      // Refresh preflight so model-availability badge updates.
      const p = await fetchPreflight();
      setPreflight(p);
    } catch (e: unknown) {
      toastRef.current.show({
        message: "Couldn't apply judge model.",
        details: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setSaving(false);
    }
  };

  const runtimeKind = preflight?.runtime_kind ?? "local-aiar";
  const isLocalRuntime = runtimeKind === "local-aiar" || !preflight?.runtime_kind;
  const canPull =
    isLocalRuntime &&
    (preflight?.capabilities?.ollama_pull === true || preflight?.capabilities == null);
  const connected = preflight?.aiar_connected ?? preflight?.aiar_available ?? false;
  const displayName = preflight?.display_name ?? "This Mac";
  const activeModel = preflight?.active_model ?? preflight?.judge_model ?? value;
  const modelReady = preflight?.active_model_ready ?? preflight?.model_available;
  const modelControlDisabled =
    !isLocalRuntime && preflight?.capabilities?.model_set_active !== true;

  return (
    <div className="judge-settings" aria-label="Judge settings">
      <div className="judge-settings-grid">
        <div className="judge-field">
          <label className="judge-field-label" htmlFor="judge-model">
            Judge model
          </label>
          <div className="judge-model-field">
            <input
              id="judge-model"
              type="text"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="llama3.1:8b"
              disabled={modelControlDisabled}
            />
            <span className="source-badge">{source}</span>
            <button
              type="button"
              className="judge-model-apply"
              onClick={onApply}
              disabled={saving || modelControlDisabled}
            >
              {saving ? "Saving…" : "Apply"}
            </button>
          </div>
        </div>

        <div className="judge-field">
          <SimilarityThresholdSlider />
        </div>
      </div>

      {preflight && (
        <div
          className="judge-preflight"
          role="region"
          aria-label="Judge model status"
        >
          <div className="judge-preflight-badges">
            <span
              className={`judge-status-badge ${connected ? "ok" : "warn"}`}
              aria-label={`AIAR ${connected ? "connected" : "warning"}`}
            >
              <img src={aiarLogo} alt="" className="aiar-logo-mark" aria-hidden="true" />
              AIAR: {connected ? `connected on ${displayName}` : "needs attention"}
            </span>
            <span
              className={`judge-status-badge ${modelReady === false ? "warn" : "ok"}`}
              aria-label={`model ${activeModel ?? ""} ${
                modelReady === false ? "not ready" : "ready"
              }`}
            >
              model: {activeModel || "unknown"}
              {modelReady === false ? " not ready" : " ready"}
            </span>
          </div>
          {preflight.judge_available === false && (
            <p
              className="judge-status-note warn"
              aria-label="Judge capability warning"
            >
              This AI connection can&apos;t grade answers — pick a different judge
              model or connection to get a verdict.
            </p>
          )}
          {canPull && preflight.model_available === false && (
            <p
              className="judge-status-note warn"
              aria-label={`model ${preflight.judge_model ?? ""} warning: not pulled in Ollama`}
            >
              model not pulled in Ollama - run `ollama pull {preflight.judge_model}`
            </p>
          )}
          {preflight.error && <p className="judge-status-note err">{preflight.error}</p>}
        </div>
      )}
    </div>
  );
}
