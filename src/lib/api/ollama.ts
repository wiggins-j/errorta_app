// F003 — Ollama detection + install + lifecycle client.
// F110 — model-pull (installed-check + SSE pull progress).
import { getJSON, postJSON, putJSON, sidecarFetch } from "../api";
import type {
  OllamaHealth,
  OllamaInstallProgress,
  OllamaRestartResult,
  OllamaSettings,
  OllamaSettingsUpdate,
} from "../../features/ollama/types";

export function health(): Promise<OllamaHealth> {
  return getJSON<OllamaHealth>("/ollama/health");
}

export function install(): Promise<OllamaInstallProgress> {
  return postJSON<OllamaInstallProgress>("/ollama/install");
}

export function installProgress(): Promise<OllamaInstallProgress> {
  return getJSON<OllamaInstallProgress>("/ollama/install-progress");
}

export function getSettings(): Promise<OllamaSettings> {
  return getJSON<OllamaSettings>("/ollama/settings");
}

export function updateSettings(update: OllamaSettingsUpdate): Promise<OllamaSettings> {
  return putJSON<OllamaSettings>("/ollama/settings", update);
}

export function restart(): Promise<OllamaRestartResult> {
  return postJSON<OllamaRestartResult>("/ollama/restart");
}

// ---------------------------------------------------------------------------
// F110 — model pull
// ---------------------------------------------------------------------------

export interface OllamaModelsResponse {
  models: string[];
  queried: string | null;
  installed: boolean;
}

/** Check installed models; pass `model` to also report its presence. */
export function getModels(model?: string): Promise<OllamaModelsResponse> {
  const qs = model ? `?model=${encodeURIComponent(model)}` : "";
  return getJSON<OllamaModelsResponse>(`/ollama/models${qs}`);
}

export type OllamaPullEvent =
  | { event: "hello" }
  | { event: "progress"; status: string; percent: number | null }
  | { event: "done"; model: string; message: string }
  | { event: "error"; error: string };

/**
 * Stream `POST /ollama/pull` via fetch + ReadableStream (EventSource can't
 * POST). Calls `onEvent` for every parsed SSE frame; returns an unsubscribe
 * function that aborts the request. Mirrors `streamExport`.
 */
export function streamPull(
  model: string,
  onEvent: (e: OllamaPullEvent) => void,
): () => void {
  const ac = new AbortController();
  (async () => {
    let resp: Response;
    try {
      resp = await sidecarFetch("/ollama/pull", {
        method: "POST",
        body: JSON.stringify({ model }),
        headers: { Accept: "text/event-stream" },
        signal: ac.signal,
      });
    } catch (err) {
      const e = err as { name?: string; message?: string };
      if (e?.name === "AbortError") return;
      onEvent({ event: "error", error: e?.message ?? String(err) });
      return;
    }
    if (!resp.ok || !resp.body) {
      onEvent({ event: "error", error: `pull HTTP ${resp.status}` });
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx: number;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          const lines = frame.split("\n");
          const isHello = lines.some((l) => l.startsWith("event: hello"));
          const dataLine = lines.find((l) => l.startsWith("data: "));
          if (!dataLine) continue;
          const payloadText = dataLine.slice("data: ".length);
          if (isHello && payloadText.trim() === "{}") {
            onEvent({ event: "hello" });
            continue;
          }
          try {
            onEvent(JSON.parse(payloadText) as OllamaPullEvent);
          } catch {
            // ignore malformed frame
          }
        }
      }
    } catch (err) {
      const e = err as { name?: string; message?: string };
      if (e?.name === "AbortError") return;
      onEvent({ event: "error", error: e?.message ?? String(err) });
    }
  })();
  return () => ac.abort();
}
