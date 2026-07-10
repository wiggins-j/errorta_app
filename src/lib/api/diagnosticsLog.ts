import { getSidecarBase } from "../sidecarPort";
import { getJSON } from "../api";

export interface LogTailResponse {
  lines: string[];
}

export async function tailLog(lines = 200): Promise<string[]> {
  const requested = Number.isFinite(lines) ? lines : 200;
  const count = Math.max(1, Math.min(5000, Math.floor(requested)));
  const response = await getJSON<LogTailResponse>(`/diagnostics/log-tail?lines=${count}`);
  return response.lines;
}

export async function streamLog(): Promise<EventSource> {
  const base = await getSidecarBase();
  return new EventSource(`${base}/diagnostics/log-stream`);
}
