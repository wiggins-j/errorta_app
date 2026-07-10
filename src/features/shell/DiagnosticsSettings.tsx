import { useEffect, useRef, useState } from "react";
import {
  getSettings,
  setLogLevel,
  type LogLevel,
} from "../../lib/api/settings";
import { streamLog, tailLog } from "../../lib/api/diagnosticsLog";

const MAX_BUFFER_LINES = 1000;
const COPY_LINE_COUNT = 200;

type Status =
  | { kind: "loading" }
  | { kind: "ready" }
  | { kind: "saving" }
  | { kind: "error"; message: string };

function normalizeError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function DiagnosticsSettings() {
  const [level, setLevel] = useState<LogLevel>("info");
  const [status, setStatus] = useState<Status>({ kind: "loading" });
  const [liveOpen, setLiveOpen] = useState(false);
  const [paused, setPaused] = useState(false);
  const [lines, setLines] = useState<string[]>([]);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [copyStatus, setCopyStatus] = useState<string | null>(null);
  const preRef = useRef<HTMLPreElement | null>(null);

  async function loadSettings(cancelled: () => boolean = () => false) {
    setStatus({ kind: "loading" });
    try {
      const settings = await getSettings();
      if (cancelled()) return;
      setLevel(settings.log_level);
      setStatus({ kind: "ready" });
    } catch (error) {
      if (!cancelled()) setStatus({ kind: "error", message: normalizeError(error) });
    }
  }

  useEffect(() => {
    let cancelled = false;
    void loadSettings(() => cancelled);
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!liveOpen) return undefined;
    let cancelled = false;
    let source: EventSource | null = null;

    setStreamError(null);
    tailLog(COPY_LINE_COUNT)
      .then((tail) => {
        if (!cancelled) setLines(tail.slice(-MAX_BUFFER_LINES));
      })
      .catch((error) => {
        if (!cancelled) setStreamError(normalizeError(error));
      });

    streamLog()
      .then((eventSource) => {
        if (cancelled) {
          eventSource.close();
          return;
        }
        source = eventSource;
        eventSource.onmessage = (event) => {
          setLines((current) => [...current, event.data].slice(-MAX_BUFFER_LINES));
        };
        eventSource.onerror = () => {
          setStreamError("Live log stream disconnected.");
        };
      })
      .catch((error) => {
        if (!cancelled) setStreamError(normalizeError(error));
      });

    return () => {
      cancelled = true;
      source?.close();
    };
  }, [liveOpen]);

  useEffect(() => {
    if (paused) return;
    const node = preRef.current;
    if (!node) return;
    node.scrollTop = node.scrollHeight;
  }, [lines, paused]);

  async function onToggle(nextChecked: boolean) {
    const nextLevel: LogLevel = nextChecked ? "debug" : "info";
    const previous = level;
    setLevel(nextLevel);
    setStatus({ kind: "saving" });
    try {
      const saved = await setLogLevel(nextLevel);
      setLevel(saved.log_level);
      setStatus({ kind: "ready" });
    } catch (error) {
      setLevel(previous);
      setStatus({ kind: "error", message: normalizeError(error) });
    }
  }

  async function onCopy() {
    const text = lines.slice(-COPY_LINE_COUNT).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopyStatus(`Copied ${Math.min(lines.length, COPY_LINE_COUNT)} lines.`);
    } catch (error) {
      setCopyStatus(`Copy failed: ${normalizeError(error)}`);
    }
  }

  const disabled = status.kind === "loading" || status.kind === "saving";
  const visibleLog = lines.join("\n");

  return (
    <div className="diagnostics-settings">
      <label className="diagnostics-debug-toggle">
        <input
          type="checkbox"
          checked={level === "debug"}
          disabled={disabled}
          onChange={(event) => void onToggle(event.currentTarget.checked)}
        />
        <span>Debug logging</span>
      </label>

      {status.kind === "error" && (
        <div role="alert" className="diagnostics-toast diagnostics-toast-err">
          {status.message}
          <button type="button" onClick={() => void loadSettings()}>
            Retry
          </button>
        </div>
      )}

      <details
        className="diagnostics-live-log"
        open={liveOpen}
        onToggle={(event) => setLiveOpen(event.currentTarget.open)}
      >
        <summary>Live log</summary>
        <div className="shell-actions diagnostics-log-actions">
          <button type="button" onClick={() => setPaused((v) => !v)}>
            {paused ? "Resume scroll" : "Pause scroll"}
          </button>
          <button type="button" onClick={() => void onCopy()} disabled={lines.length === 0}>
            Copy last 200
          </button>
          {copyStatus && (
            <span role="status" className="shell-muted">
              {copyStatus}
            </span>
          )}
        </div>
        {streamError && (
          <div role="alert" className="diagnostics-toast diagnostics-toast-err">
            {streamError}
          </div>
        )}
        <div
          role="log"
          aria-live="polite"
          aria-label="Live sidecar log"
          className="diagnostics-log-region"
        >
          <pre
            ref={preRef}
            data-testid="diagnostics-log-pre"
            className="diagnostics-log-pre"
          >
            {visibleLog || "Waiting for log lines..."}
          </pre>
        </div>
      </details>
    </div>
  );
}

export default DiagnosticsSettings;
