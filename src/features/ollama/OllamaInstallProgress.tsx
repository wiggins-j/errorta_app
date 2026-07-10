import type { OllamaInstallProgress as Progress } from "./types";

interface Props {
  progress: Progress | null;
}

const PHASE_LABEL: Record<string, string> = {
  idle: "Idle",
  downloading: "Downloading…",
  verifying: "Verifying…",
  installing: "Installing…",
  starting: "Starting…",
  ready: "Ready",
  error: "Error",
};

export default function OllamaInstallProgress({ progress }: Props) {
  if (!progress || progress.phase === "idle") return null;
  const pct = Math.max(0, Math.min(100, progress.percent));
  return (
    <div className="feature-pane-card" style={{ marginTop: 16 }}>
      <strong>{PHASE_LABEL[progress.phase] ?? progress.phase}</strong>
      <div
        role="progressbar"
        aria-valuenow={Math.round(pct)}
        aria-valuemin={0}
        aria-valuemax={100}
        style={{
          marginTop: 8,
          height: 8,
          width: "100%",
          background: "rgba(127,127,127,0.2)",
          borderRadius: 4,
          overflow: "hidden",
        }}
      >
        <div
          style={{
            width: `${pct}%`,
            height: "100%",
            background: "var(--accent, #6F7DF2)",
            transition: "width 200ms linear",
          }}
        />
      </div>
      <p style={{ marginTop: 8, opacity: 0.8 }}>
        {progress.message || (progress.error ? `Error: ${progress.error}` : "")}
      </p>
      {progress.error ? (
        <p style={{ color: "#d04848" }}>
          <code>{progress.error}</code>
        </p>
      ) : null}
    </div>
  );
}
