// F001 Judge API client.
import { getJSON, postJSON, putJSON, sidecarFetch } from "../api";

export interface Verdict {
  rating: "pass" | "partial" | "fail" | string;
  reason?: string | null;
  failure_tags?: string[];
  confidence?: number | null;
  latency_ms?: number | null;
}

export interface GroundingMatch {
  kind: "exact" | "similar" | "none" | string;
  similarity?: number;
  original_signature?: string;
}

export interface VerdictResponse {
  id: string;
  prompt: string;
  answer: string;
  verdict: Verdict;
  judge_model?: string | null;
  model?: string | null;
  prior_correction?: string | null;
  prompt_signature?: string | null;
  grounding_match?: GroundingMatch;
  call_id?: string | null;
  instance?: string | null;
  grounded?: boolean | null;
  reground_applied?: boolean | null;
  rag_enabled?: boolean | null;
  latency?: number | null;
}

export interface PriorVerdictPayload {
  verdict: Verdict | null;
  judge_model?: string | null;
  created_at?: string | null;
}

export interface PriorVerdictsResponse {
  signature: string;
  priors: PriorVerdictPayload[];
}

export interface AcceptResponse {
  id: string;
  prompt: string;
  answer: string;
  correction?: string | null;
  verdict?: Verdict | null;
  grounding_recorded: boolean;
  created_at?: string | null;
}

export interface CorrectionDraftResponse {
  draft: string;
}

export interface MetricsTrendDay {
  date: string;
  total: number;
  pass: number;
  pass_rate: number | null;
}

export interface VerdictTimelineEntry {
  rating: "pass" | "partial" | "fail" | "unknown" | string;
  judge_model?: string | null;
  created_at?: string | null;
  reason_snippet?: string | null;
}

export interface MetricsCorrectedPrompt {
  prompt: string;
  count: number;
  prompt_signature?: string | null;
  verdict_timeline?: VerdictTimelineEntry[];
}

export interface LatencyHistogramBucket {
  label: string;
  count: number;
}

export interface LatencyHistogram {
  buckets: LatencyHistogramBucket[];
  p50_ms: number | null;
  p95_ms: number | null;
  p99_ms: number | null;
}

export interface MetricsResponse {
  total: number;
  pass_rate: number | null;
  total_7d: number;
  pass_rate_7d: number | null;
  rating_counts: Record<string, number>;
  trend_7d: MetricsTrendDay[];
  most_corrected_prompts: MetricsCorrectedPrompt[];
  latency_histogram?: LatencyHistogram | null;
  log_path: string;
}

export interface PreflightResponse {
  judge_model?: string | null;
  judge_model_source: "env" | "override" | "default" | string;
  aiar_available: boolean;
  ollama_reachable: boolean;
  model_available?: boolean | null;
  error?: string | null;
  runtime_kind?: string | null;
  display_name?: string | null;
  aiar_connected?: boolean | null;
  backend_id?: string | null;
  answer_available?: boolean | null;
  judge_available?: boolean | null;
  active_model?: string | null;
  active_model_ready?: boolean | null;
  available_models?: string[];
  model_source?: string | null;
  capabilities?: Record<string, boolean | string>;
}

export interface ModelResponse {
  judge_model?: string | null;
  source: "env" | "override" | "default" | string;
}

export function runVerdict(
  prompt: string,
  opts?: { corpus?: string; judge_model?: string },
): Promise<VerdictResponse> {
  return postJSON<VerdictResponse>("/judge/verdict", {
    prompt,
    corpus: opts?.corpus,
    judge_model: opts?.judge_model,
  });
}

export function draftCorrection(
  answer: string,
  verdict: Verdict,
): Promise<CorrectionDraftResponse> {
  return postJSON<CorrectionDraftResponse>("/judge/correction-draft", {
    answer,
    verdict,
  });
}

export function acceptVerdict(
  id: string,
  correction?: string,
): Promise<AcceptResponse> {
  return postJSON<AcceptResponse>("/judge/accept", { id, correction });
}

export function fetchPriorVerdicts(
  signature: string,
  limit = 5,
): Promise<PriorVerdictsResponse> {
  const params = new URLSearchParams({ signature, limit: String(limit) });
  return getJSON<PriorVerdictsResponse>(`/judge/prior-verdicts?${params.toString()}`);
}

export function fetchMetrics(): Promise<MetricsResponse> {
  return getJSON<MetricsResponse>("/judge/metrics");
}

export function fetchPreflight(): Promise<PreflightResponse> {
  return getJSON<PreflightResponse>("/judge/preflight");
}

export function fetchModel(): Promise<ModelResponse> {
  return getJSON<ModelResponse>("/judge/model");
}

export function setModel(judge_model: string | null): Promise<ModelResponse> {
  return putJSON<ModelResponse>("/judge/model", { judge_model });
}

// ---------- F-WEDGE-DEEPEN-V1: replay ----------

export interface ReplayResult {
  prompt: string;
  original_answer: string;
  original_verdict: Verdict;
  original_grounding_match?: GroundingMatch | null;
  replay_answer: string;
  replay_verdict: Verdict;
  replay_grounding_match?: GroundingMatch | null;
  score_delta: number;
  grounding_change: "added" | "removed" | "unchanged" | string;
  occurred_at: string;
}

export interface ReplayRequest {
  corpus: string;
  dry_run?: boolean;
  limit?: number | null;
}

/** One-shot JSON replay (no streaming). */
export function replayCorpus(
  corpus: string,
  dryRun = false,
  limit?: number | null,
): Promise<ReplayResult[]> {
  const body: ReplayRequest = { corpus, dry_run: dryRun, limit: limit ?? null };
  return postJSON<ReplayResult[]>("/judge/replay", body);
}

/**
 * SSE replay. Calls ``onResult`` once per parsed ``data: {json}`` frame.
 * Returns an abort function the caller can invoke to cancel mid-stream.
 */
export async function replayCorpusStream(
  corpus: string,
  onResult: (r: ReplayResult) => void,
  opts: { dryRun?: boolean; limit?: number | null; signal?: AbortSignal } = {},
): Promise<void> {
  const body: ReplayRequest = {
    corpus,
    dry_run: opts.dryRun ?? false,
    limit: opts.limit ?? null,
  };
  const resp = await sidecarFetch("/judge/replay", {
    method: "POST",
    body: JSON.stringify(body),
    headers: { Accept: "text/event-stream" },
    signal: opts.signal,
  });
  if (!resp.ok || !resp.body) {
    throw new Error(`replay HTTP ${resp.status}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const line = frame.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      try {
        const payload = JSON.parse(line.slice("data: ".length)) as ReplayResult;
        onResult(payload);
      } catch {
        // ignore malformed frames
      }
    }
  }
}
