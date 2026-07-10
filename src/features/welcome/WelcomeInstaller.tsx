// F007 — embeddable sample-corpus installer. Drives the
// picker -> download/ingest -> suggested-prompt flow without a page header,
// so it can be embedded in the Corpus pane (the standalone Welcome tab was
// folded into Corpus).
import { useEffect, useRef, useState } from "react";
import * as welcomeApi from "../../lib/api/welcome";
import CorporaEmptyState from "./CorporaEmptyState";
import WelcomeCorpusPicker from "./WelcomeCorpusPicker";
import DownloadProgress from "./DownloadProgress";
import SuggestedPrompt from "./SuggestedPrompt";
import type {
  WelcomeInstallResult,
  WelcomeOption,
  WelcomeStatus,
} from "./types";

type Stage = "empty" | "picker" | "installing" | "done" | "error";

interface Props {
  // "panel" = compact affordance embedded in another pane (Corpus).
  // "full" = the original full-bleed empty state.
  variant?: "panel" | "full";
  onInstalled?: (corpusName: string) => void;
}

export default function WelcomeInstaller({ variant = "panel", onInstalled }: Props) {
  const [stage, setStage] = useState<Stage>("empty");
  const [options, setOptions] = useState<WelcomeOption[]>([]);
  const [status, setStatus] = useState<WelcomeStatus | null>(null);
  const [result, setResult] = useState<WelcomeInstallResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (pollRef.current != null) window.clearInterval(pollRef.current);
    };
  }, []);

  const openPicker = async () => {
    setError(null);
    try {
      const resp = await welcomeApi.listOptions();
      setOptions(resp.options);
      setStage("picker");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setStage("error");
    }
  };

  const startInstall = async () => {
    setStage("installing");
    setError(null);
    pollRef.current = window.setInterval(async () => {
      try {
        setStatus(await welcomeApi.getStatus());
      } catch {
        /* transient — ignore */
      }
    }, 500);
    try {
      const r = await welcomeApi.install();
      setResult(r);
      setStage("done");
      onInstalled?.(r.corpus_name);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setStage("error");
    } finally {
      if (pollRef.current != null) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
      try {
        setStatus(await welcomeApi.getStatus());
      } catch {
        /* ignore */
      }
    }
  };

  // Compact entry affordance for the embedded panel: a single button that
  // opens the picker. The full variant shows the large empty state.
  const entry =
    variant === "full" ? (
      <CorporaEmptyState onAddSample={openPicker} onSkip={() => setStage("empty")} />
    ) : (
      <div className="corpus-sample-entry">
        <button type="button" className="welcome-cta-primary" onClick={openPicker}>
          Add a sample corpus
        </button>
        <span className="corpus-sample-hint">
          Errorta&apos;s own docs as a small starter corpus (&lt; 5 MB, fully
          deletable).
        </span>
      </div>
    );

  return (
    <div className="welcome-installer">
      {stage === "empty" ? entry : null}

      {stage === "picker" ? (
        <WelcomeCorpusPicker
          options={options}
          onConfirm={startInstall}
          onCancel={() => setStage("empty")}
        />
      ) : null}

      {stage === "installing" ? <DownloadProgress status={status} /> : null}

      {stage === "done" && result ? (
        <SuggestedPrompt
          prompt={result.suggested_prompt}
          corpusName={result.corpus_name}
        />
      ) : null}

      {stage === "error" ? (
        <div className="welcome-progress-error">
          <p>Install failed: {error ?? "unknown error"}</p>
          <button
            type="button"
            className="welcome-cta-secondary"
            onClick={() => setStage("empty")}
          >
            Back
          </button>
        </div>
      ) : null}
    </div>
  );
}

export { CorporaEmptyState };
