interface Props {
  onInstall: () => void;
  onSkip: () => void;
  installing: boolean;
}

export default function OllamaInstallPrompt({ onInstall, onSkip, installing }: Props) {
  return (
    <div className="feature-pane-card" style={{ marginTop: 16 }}>
      <h2 style={{ marginTop: 0 }}>Errorta needs Ollama to run local models.</h2>
      <p>
        Ollama is a free, open-source tool that hosts the LLM. We can install it for you — it takes
        about 90 seconds and 450 MB of disk space.
      </p>
      <div style={{ display: "flex", gap: 8 }}>
        <button type="button" onClick={onInstall} disabled={installing}>
          {installing ? "Installing…" : "Install Ollama"}
        </button>
        <button type="button" onClick={onSkip} disabled={installing}>
          I'll install it myself
        </button>
      </div>
    </div>
  );
}
