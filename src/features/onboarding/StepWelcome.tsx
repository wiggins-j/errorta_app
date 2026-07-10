// INTEGRATION — onboarding: sample-corpus install (final step).
//
// Installs Errorta's own docs as a small starter corpus so a new user has
// something to try the app on. It no longer auto-advances the instant the POST
// returns — it shows what was installed (corpus name, file count, where it
// lives) and lets the user finish explicitly.
import { useState } from "react";
import * as welcomeApi from "../../lib/api/welcome";
import type { WelcomeInstallResult } from "../welcome/types";

interface Props {
  onAdvance: () => void;
  onSkip: () => void;
  done: boolean;
}

export default function StepWelcome({ onAdvance, onSkip, done }: Props) {
  const [installing, setInstalling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<WelcomeInstallResult | null>(null);

  const runInstall = async () => {
    setInstalling(true);
    setError(null);
    try {
      const r = await welcomeApi.install();
      setResult(r);
      if (r.f004_error) setError(r.f004_error);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setInstalling(false);
    }
  };

  const installed = result != null && !result.f004_error;

  return (
    <div className="onboarding-step">
      <h2>Try it on a sample corpus</h2>
      <p>
        Optional. Errorta answers questions about documents you give it — so to
        try it out, we can install <strong>Errorta's own documentation</strong>{" "}
        as a small starter corpus (a few files, downloaded on demand). It becomes
        a normal corpus you can query on the Judge tab and delete any time from
        the <strong>Corpus</strong> tab. You can also drop your own files there
        later.
      </p>

      {installing ? (
        <p className="onboarding-detail" role="status" data-testid="welcome-installing">
          Downloading and ingesting the sample corpus…
        </p>
      ) : null}

      {installed && result ? (
        <p
          className="provider-key-status configured"
          role="status"
          data-testid="welcome-installed"
        >
          Installed <strong>{result.files_ingested}</strong>{" "}
          {result.files_ingested === 1 ? "file" : "files"} into the{" "}
          <code>{result.corpus_name}</code> corpus — find it on the{" "}
          <strong>Corpus</strong> tab.
        </p>
      ) : null}

      {error ? <p className="onboarding-error">Install failed: {error}</p> : null}

      <div className="onboarding-actions">
        <button
          type="button"
          className="onboarding-cta-primary"
          onClick={installed ? onAdvance : runInstall}
          disabled={installing}
          data-testid={installed ? "welcome-finish" : "welcome-install"}
        >
          {installing
            ? "Installing…"
            : installed
              ? "Finish"
              : done
                ? "Re-install"
                : "Install sample corpus"}
        </button>
        <button
          type="button"
          className="onboarding-cta-secondary"
          onClick={onAdvance}
          data-testid="welcome-skip-step"
        >
          {installed ? "Finish" : "Skip"}
        </button>
        <button
          type="button"
          className="onboarding-cta-link"
          onClick={onSkip}
        >
          Skip setup
        </button>
      </div>
    </div>
  );
}
