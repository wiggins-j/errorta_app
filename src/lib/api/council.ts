// F031 Phase 2 — Council API client.
// Wraps the FastAPI surface in python/errorta_app/routes/council.py.
// This module is the ONLY place that knows snake_case backend names
// (invariant 11). Components consume normalized camelCase types from
// ../../features/council/types.ts.

import { getJSON, postJSON } from "../api";
import type {
  CouncilCalloutRecord,
  CouncilChildRun,
  CouncilChildRunMessage,
  CouncilContextManifest,
  CouncilContextSourceRef,
  CouncilPendingDecision,
  CouncilRoomSummary,
  CouncilRunAuditSummary,
  CouncilRunSummary,
  CouncilEventUsage,
  CouncilRunStatus,
  CouncilTranscriptEvent,
  CouncilTurnInspection,
  DemoCorpusResult,
} from "../../features/council/types";
import { mapBackendRunState } from "../../features/council/types";

interface BackendRoomSummary {
  id: string;
  name: string;
  updated_at: string;
  revision: number;
  status_hint?: string;
}

interface BackendRunMeta {
  id: string;
  room_id?: string | null;
  status: string;
  prompt?: string | null;
  terminal_reason?: string | null;
  paused_at?: string | null;
  cancel_requested_at?: string | null;
}

interface BackendRunSummary {
  id: string;
  room_id: string;
  status: string;
  updated_at: string;
  event_count: number;
  last_sequence: number;
}

interface BackendEvent {
  id: string;
  run_id: string;
  sequence: number;
  type: string;
  status: string;
  created_at: string;
  member_id?: string | null;
  round?: number | null;
  payload?: Record<string, unknown>;
  usage?: Record<string, unknown> | null;
}

function adaptRoom(r: BackendRoomSummary): CouncilRoomSummary {
  return {
    id: r.id,
    name: r.name,
    updatedAt: r.updated_at,
    revision: r.revision,
    statusHint: r.status_hint ?? "draft",
  };
}

function adaptRun(r: BackendRunMeta): CouncilRunStatus {
  return {
    runId: r.id,
    roomId: r.room_id ?? undefined,
    state: mapBackendRunState(r.status),
    backendStatus: r.status,
    prompt: r.prompt ?? undefined,
    terminalReason: r.terminal_reason ?? undefined,
    pausedAt: r.paused_at ?? undefined,
    cancelRequestedAt: r.cancel_requested_at ?? undefined,
  };
}

function adaptRunSummary(r: BackendRunSummary): CouncilRunSummary {
  return {
    runId: r.id,
    roomId: r.room_id,
    state: mapBackendRunState(r.status),
    backendStatus: r.status,
    updatedAt: r.updated_at,
    eventCount: r.event_count,
    lastSequence: r.last_sequence,
  };
}

function adaptEventUsage(raw: unknown): CouncilEventUsage | undefined {
  // F143: the gateway usage dict the scheduler stamps on MEMBER_MESSAGE events.
  // Absent on events with no model call; number fields absent when unreported.
  if (typeof raw !== "object" || raw === null) return undefined;
  const u = raw as Record<string, unknown>;
  const num = (v: unknown): number | undefined =>
    typeof v === "number" && Number.isFinite(v) ? v : undefined;
  const inputTokens = num(u.input_tokens);
  const outputTokens = num(u.output_tokens);
  if (inputTokens === undefined && outputTokens === undefined) return undefined;
  return {
    inputTokens,
    outputTokens,
    cacheReadInputTokens: num(u.cache_read_input_tokens),
    cacheWriteInputTokens: num(u.cache_write_input_tokens),
  };
}

function adaptEvent(e: BackendEvent): CouncilTranscriptEvent {
  return {
    id: e.id,
    runId: e.run_id,
    sequence: e.sequence,
    type: e.type,
    status: e.status,
    createdAt: e.created_at,
    memberId: e.member_id ?? undefined,
    round: e.round ?? undefined,
    payload: e.payload ?? {},
    usage: adaptEventUsage(e.usage),
    raw: e,
  };
}

export async function listRooms(): Promise<CouncilRoomSummary[]> {
  const body = await getJSON<{ rooms: BackendRoomSummary[] }>("/council/rooms");
  return body.rooms.map(adaptRoom);
}

// F031-DEMO-CORPUS — minimal room-metadata probe. Returns a flattened bag
// merging the room's `metadata` dict (for `demo_marker` detection) and
// any top-level extras the schema's `_extras` round-trip preserves
// (specifically `corpus_ids`, which CouncilShell forwards to createRun
// — QA P1 #2 lock).
export async function getRoomMetadata(
  roomId: string,
): Promise<Record<string, unknown> | null> {
  try {
    const body = await getJSON<{ room: Record<string, unknown> }>(
      `/council/rooms/${roomId}`,
    );
    if (!body.room) return null;
    const out: Record<string, unknown> = {};
    const m = body.room.metadata;
    if (m && typeof m === "object") {
      Object.assign(out, m);
    }
    // Surface top-level extras the shell consults — currently `corpus_ids`.
    // Keep this list narrow to avoid leaking the whole room shape into a
    // bag billed as "metadata".
    if (Array.isArray(body.room.corpus_ids)) {
      out.corpus_ids = body.room.corpus_ids;
    }
    return Object.keys(out).length > 0 ? out : null;
  } catch {
    return null;
  }
}

export interface CreateRunOptions {
  dryFakeMembers?: boolean;
  corpusIds?: string[];
}

export async function createRun(
  roomId: string,
  prompt: string,
  options?: CreateRunOptions,
): Promise<{ run: CouncilRunStatus; events: CouncilTranscriptEvent[] }> {
  const body = await postJSON<{ run: BackendRunMeta; events: BackendEvent[] }>(
    "/council/runs",
    {
      room_id: roomId,
      prompt,
      corpus_ids: options?.corpusIds ?? [],
      dry_fake_members: options?.dryFakeMembers ?? false,
    },
  );
  return {
    run: adaptRun(body.run),
    events: body.events.map(adaptEvent),
  };
}

export async function listRuns(options?: {
  roomId?: string;
  status?: string;
  limit?: number;
  offset?: number;
}): Promise<CouncilRunSummary[]> {
  const params = new URLSearchParams();
  if (options?.roomId) params.set("room_id", options.roomId);
  if (options?.status) params.set("status", options.status);
  if (options?.limit !== undefined) params.set("limit", String(options.limit));
  if (options?.offset !== undefined) params.set("offset", String(options.offset));
  const query = params.toString();
  const body = await getJSON<{ runs: BackendRunSummary[] }>(
    `/council/runs${query ? `?${query}` : ""}`,
  );
  return body.runs.map(adaptRunSummary);
}

// F074 — the most recent run a paired phone touched, for desktop auto-surface.
export interface MobileActivity {
  runId: string | null;
  kind?: string;
  seq: number;
}

export async function getMobileActivity(): Promise<MobileActivity> {
  const body = await getJSON<{ run_id: string | null; kind?: string; seq: number }>(
    `/council/mobile-activity`,
  );
  return { runId: body.run_id, kind: body.kind, seq: body.seq };
}

export async function getRun(
  runId: string,
): Promise<{ run: CouncilRunStatus; events: CouncilTranscriptEvent[] }> {
  const body = await getJSON<{ run: BackendRunMeta; events: BackendEvent[] }>(
    `/council/runs/${runId}`,
  );
  return {
    run: adaptRun(body.run),
    events: body.events.map(adaptEvent),
  };
}

export async function getRunEvents(
  runId: string,
  afterSequence = 0,
): Promise<{ events: CouncilTranscriptEvent[]; terminal: boolean; lastSequence: number }> {
  const body = await getJSON<{
    events: BackendEvent[];
    terminal: boolean;
    last_sequence: number;
  }>(`/council/runs/${runId}/events?after_sequence=${afterSequence}`);
  return {
    events: body.events.map(adaptEvent),
    terminal: body.terminal,
    lastSequence: body.last_sequence,
  };
}

export async function cancelRun(runId: string): Promise<CouncilRunStatus> {
  const body = await postJSON<{ run: BackendRunMeta }>(
    `/council/runs/${runId}/cancel`,
    { reason: "user_action" },
  );
  return adaptRun(body.run);
}

export async function pauseRun(runId: string): Promise<CouncilRunStatus> {
  const body = await postJSON<{ run: BackendRunMeta }>(
    `/council/runs/${runId}/pause`,
    {},
  );
  return adaptRun(body.run);
}

export async function resumeRun(runId: string): Promise<CouncilRunStatus> {
  const body = await postJSON<{ run: BackendRunMeta }>(
    `/council/runs/${runId}/resume`,
    {},
  );
  return adaptRun(body.run);
}

// F049: send a live user message into a running/paused run. The next member to
// speak picks it up as authoritative direction. UI-originated (like /decision).
export async function injectMessage(
  runId: string,
  text: string,
): Promise<CouncilRunStatus> {
  const body = await postJSON<{ run: BackendRunMeta }>(
    `/council/runs/${runId}/interjection`,
    { text, requested_by: "user" },
    UI_ORIGIN,
  );
  return adaptRun(body.run);
}

// ---------------------------------------------------------------------------
// F037 expert callouts.
// ---------------------------------------------------------------------------

// Ask-class actions require UI origin (matches /decision).
const UI_ORIGIN = { "x-errorta-origin": "tauri-ui" };

interface BackendCalloutRecord {
  callout_id: string;
  target_id: string;
  reason_code: string;
  question: string;
  requested_by: Record<string, unknown>;
  state: string;
  advisory: boolean;
  approval: string | null;
  reject_reason: string | null;
  answer_event_id: string | null;
}

function adaptCallout(r: BackendCalloutRecord): CouncilCalloutRecord {
  return {
    calloutId: r.callout_id,
    targetId: r.target_id,
    reasonCode: r.reason_code,
    question: r.question,
    requestedBy: r.requested_by ?? {},
    state: r.state,
    advisory: !!r.advisory,
    approval: r.approval ?? null,
    rejectReason: r.reject_reason ?? null,
    answerEventId: r.answer_event_id ?? null,
  };
}

export async function requestCallout(
  runId: string,
  input: { targetId: string; question?: string; reasonCode?: string },
): Promise<{ calloutId: string; status: string }> {
  const body = await postJSON<{ callout_id: string; status: string }>(
    `/council/runs/${runId}/callouts`,
    {
      target_id: input.targetId,
      question: input.question ?? "",
      reason_code: input.reasonCode ?? "user_requested",
    },
    UI_ORIGIN,
  );
  return { calloutId: body.callout_id, status: body.status };
}

export async function listCallouts(
  runId: string,
): Promise<CouncilCalloutRecord[]> {
  const body = await getJSON<{ callouts: BackendCalloutRecord[] }>(
    `/council/runs/${runId}/callouts`,
  );
  return (body.callouts ?? []).map(adaptCallout);
}

export async function approveCallout(
  runId: string,
  calloutId: string,
): Promise<CouncilCalloutRecord | null> {
  const body = await postJSON<{ callout: BackendCalloutRecord | null }>(
    `/council/runs/${runId}/callouts/${calloutId}/approve`,
    {},
    UI_ORIGIN,
  );
  return body.callout ? adaptCallout(body.callout) : null;
}

export async function rejectCallout(
  runId: string,
  calloutId: string,
): Promise<CouncilCalloutRecord | null> {
  const body = await postJSON<{ callout: BackendCalloutRecord | null }>(
    `/council/runs/${runId}/callouts/${calloutId}/reject`,
    {},
    UI_ORIGIN,
  );
  return body.callout ? adaptCallout(body.callout) : null;
}

// ---------------------------------------------------------------------------
// F041 pending policy decisions.
// ---------------------------------------------------------------------------

interface BackendPendingDecision {
  decision_id: string;
  run_id: string;
  phase: string;
  state: string;
  reason_code: string;
  requester?: Record<string, unknown>;
  safe_request?: Record<string, unknown>;
  risk_class?: string | null;
  created_at: string;
  resolved_at?: string | null;
  resolved_by?: string | null;
  state_writes_on_approve?: Array<Record<string, unknown>>;
  applied_state_writes?: Array<Record<string, unknown>>;
  metadata?: Record<string, unknown>;
}

function adaptPendingDecision(r: BackendPendingDecision): CouncilPendingDecision {
  return {
    decisionId: r.decision_id,
    runId: r.run_id,
    phase: r.phase,
    state: r.state,
    reasonCode: r.reason_code,
    requester: r.requester ?? {},
    safeRequest: r.safe_request ?? {},
    riskClass: r.risk_class ?? null,
    createdAt: r.created_at,
    resolvedAt: r.resolved_at ?? null,
    resolvedBy: r.resolved_by ?? null,
    stateWritesOnApprove: r.state_writes_on_approve ?? [],
    appliedStateWrites: r.applied_state_writes ?? [],
    metadata: r.metadata ?? {},
  };
}

export async function listPendingDecisions(
  runId: string,
  state?: string,
): Promise<CouncilPendingDecision[]> {
  const query = state ? `?state=${encodeURIComponent(state)}` : "";
  const body = await getJSON<{ decisions: BackendPendingDecision[] }>(
    `/council/runs/${runId}/pending-decisions${query}`,
  );
  return (body.decisions ?? []).map(adaptPendingDecision);
}

export async function approvePendingDecision(
  runId: string,
  decisionId: string,
): Promise<CouncilPendingDecision> {
  const body = await postJSON<{ decision: BackendPendingDecision }>(
    `/council/runs/${runId}/pending-decisions/${decisionId}/approve`,
    {},
    UI_ORIGIN,
  );
  return adaptPendingDecision(body.decision);
}

export async function rejectPendingDecision(
  runId: string,
  decisionId: string,
): Promise<CouncilPendingDecision> {
  const body = await postJSON<{ decision: BackendPendingDecision }>(
    `/council/runs/${runId}/pending-decisions/${decisionId}/reject`,
    {},
    UI_ORIGIN,
  );
  return adaptPendingDecision(body.decision);
}

// ---------------------------------------------------------------------------
// F042 child runs and async inbox.
// ---------------------------------------------------------------------------

interface BackendChildRun {
  parent_run_id: string;
  child_run_id: string;
  member_id: string;
  task_kind: string;
  status: string;
  title: string;
  prompt_sha256: string;
  worker_kind: string;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  artifact_refs?: Array<Record<string, unknown>>;
  summary_ref?: Record<string, unknown> | null;
  failure?: Record<string, unknown> | null;
  metadata?: Record<string, unknown>;
}

interface BackendChildRunMessage {
  message_id: string;
  parent_run_id: string;
  child_run_id: string;
  message_kind: string;
  payload_preview: string;
  payload_sha256: string;
  payload_bytes: number;
  created_at: string;
  artifact_refs?: Array<Record<string, unknown>>;
  metadata?: Record<string, unknown>;
}

function adaptChildRun(r: BackendChildRun): CouncilChildRun {
  return {
    parentRunId: r.parent_run_id,
    childRunId: r.child_run_id,
    memberId: r.member_id,
    taskKind: r.task_kind,
    status: r.status,
    title: r.title,
    promptSha256: r.prompt_sha256,
    workerKind: r.worker_kind,
    createdAt: r.created_at,
    updatedAt: r.updated_at,
    startedAt: r.started_at ?? null,
    finishedAt: r.finished_at ?? null,
    artifactRefs: r.artifact_refs ?? [],
    summaryRef: r.summary_ref ?? null,
    failure: r.failure ?? null,
    metadata: r.metadata ?? {},
  };
}

function adaptChildRunMessage(
  r: BackendChildRunMessage,
): CouncilChildRunMessage {
  return {
    messageId: r.message_id,
    parentRunId: r.parent_run_id,
    childRunId: r.child_run_id,
    messageKind: r.message_kind,
    payloadPreview: r.payload_preview,
    payloadSha256: r.payload_sha256,
    payloadBytes: r.payload_bytes,
    createdAt: r.created_at,
    artifactRefs: r.artifact_refs ?? [],
    metadata: r.metadata ?? {},
  };
}

export async function listChildRuns(runId: string): Promise<CouncilChildRun[]> {
  const body = await getJSON<{ children: BackendChildRun[] }>(
    `/council/runs/${runId}/children`,
  );
  return (body.children ?? []).map(adaptChildRun);
}

export async function listChildRunMessages(
  runId: string,
  childRunId: string,
): Promise<CouncilChildRunMessage[]> {
  const body = await getJSON<{ messages: BackendChildRunMessage[] }>(
    `/council/runs/${runId}/children/${childRunId}/messages`,
  );
  return (body.messages ?? []).map(adaptChildRunMessage);
}

// ---------------------------------------------------------------------------
// F031-08 inspection — Phase 5 Task 1.
// ---------------------------------------------------------------------------

interface BackendSourceRef {
  class_: string;
  corpus_id?: string | null;
  chunk_id?: string | null;
  citation_id?: string | null;
  content_sha256?: string | null;
  tokens?: number | null;
  transcript_event_id?: string | null;
  sequence?: number | null;
  packed?: string | null;
  tool_call_id?: string | null;
  tool_id?: string | null;
  args_sha256?: string | null;
  produced_at?: string | null;
  tool_egress_class?: string | null;
}

interface BackendCitationRef {
  citation_id: string;
  content_sha256?: string | null;
  packed?: string | null;
}

interface BackendCompactionSegment {
  segment_index: number;
  round_range?: number[];
  artifact_sha256?: string | null;
  mode?: string | null;
  event_ids?: string[];
}

interface BackendStewardManifest {
  enabled?: boolean;
  fallback?: boolean;
  reason?: string | null;
  packet_id?: string | null;
  content_sha256?: string | null;
  mode?: string | null;
  coverage?: {
    from_sequence?: number | null;
    to_sequence?: number | null;
    source_event_ids?: string[];
  };
  recent_full_message_count?: number | null;
  omitted_transcript_event_count?: number | null;
  effective_transcript_access?: string | null;
  [key: string]: unknown;
}

interface BackendContextManifest {
  manifest_id: string;
  format_version: number;
  context_id: string;
  run_id: string;
  turn_id: string;
  member_id: string;
  payload_sha256: string;
  requested_context_access: string;
  effective_context_access: string;
  requested_transcript_access: string;
  effective_transcript_access: string;
  destination_scope: string;
  egress_class: string;
  source_counts: Record<string, number>;
  source_refs: BackendSourceRef[];
  omitted: Array<Record<string, unknown>>;
  token_estimate: Record<string, unknown>;
  citation_refs?: BackendCitationRef[];
  compaction?: {
    segments?: BackendCompactionSegment[];
    [key: string]: unknown;
  };
  steward?: BackendStewardManifest;
  packing_contract?: string;
  packing_order_variant?: string;
  cache_hints?: Array<Record<string, unknown>>;
  blocked_reason?: string | null;
  transform_manifest_id?: string | null;
  visibility_plan_id?: string | null;
  f030_audit_id?: string | null;
}

function adaptSourceRef(r: BackendSourceRef): CouncilContextSourceRef {
  return {
    class_: r.class_,
    corpusId: r.corpus_id ?? null,
    chunkId: r.chunk_id ?? null,
    citationId: r.citation_id ?? null,
    contentSha256: r.content_sha256 ?? null,
    tokens: r.tokens ?? null,
    transcriptEventId: r.transcript_event_id ?? null,
    sequence: r.sequence ?? null,
    packed: r.packed ?? null,
    toolCallId: r.tool_call_id ?? null,
    toolId: r.tool_id ?? null,
    argsSha256: r.args_sha256 ?? null,
    producedAt: r.produced_at ?? null,
    toolEgressClass: r.tool_egress_class ?? null,
  };
}

function adaptManifest(m: BackendContextManifest): CouncilContextManifest {
  return {
    manifestId: m.manifest_id,
    formatVersion: m.format_version,
    contextId: m.context_id,
    runId: m.run_id,
    turnId: m.turn_id,
    memberId: m.member_id,
    payloadSha256: m.payload_sha256,
    requestedContextAccess: m.requested_context_access,
    effectiveContextAccess: m.effective_context_access,
    requestedTranscriptAccess: m.requested_transcript_access,
    effectiveTranscriptAccess: m.effective_transcript_access,
    destinationScope: m.destination_scope,
    egressClass: m.egress_class,
    sourceCounts: m.source_counts ?? {},
    sourceRefs: (m.source_refs ?? []).map(adaptSourceRef),
    omitted: m.omitted ?? [],
    tokenEstimate: m.token_estimate ?? {},
    citationRefs: (m.citation_refs ?? []).map((r) => ({
      citationId: r.citation_id,
      contentSha256: r.content_sha256 ?? null,
      packed: r.packed ?? null,
    })),
    compaction: {
      ...(m.compaction ?? {}),
      segments: ((m.compaction ?? {}).segments ?? []).map((s) => ({
        segmentIndex: s.segment_index,
        roundRange: s.round_range ?? [],
        artifactSha256: s.artifact_sha256 ?? null,
        mode: s.mode ?? null,
        eventIds: s.event_ids ?? [],
      })),
    },
    steward: adaptSteward(m.steward),
    packingContract: m.packing_contract ?? "v1",
    packingOrderVariant: m.packing_order_variant ?? "default",
    cacheHints: m.cache_hints ?? [],
    blockedReason: m.blocked_reason ?? null,
    transformManifestId: m.transform_manifest_id ?? null,
    visibilityPlanId: m.visibility_plan_id ?? null,
    f030AuditId: m.f030_audit_id ?? null,
  };
}

function adaptSteward(s?: BackendStewardManifest): CouncilContextManifest["steward"] {
  if (!s) return undefined;
  const coverage = s.coverage ?? {};
  return {
    ...s,
    packetId: s.packet_id ?? null,
    contentSha256: s.content_sha256 ?? null,
    coverage: {
      fromSequence: coverage.from_sequence ?? null,
      toSequence: coverage.to_sequence ?? null,
      sourceEventIds: coverage.source_event_ids ?? [],
    },
    recentFullMessageCount: s.recent_full_message_count ?? null,
    omittedTranscriptEventCount: s.omitted_transcript_event_count ?? null,
    effectiveTranscriptAccess: s.effective_transcript_access ?? null,
  };
}


export async function getTurnInspection(
  runId: string,
  turnId: string,
): Promise<CouncilTurnInspection | null> {
  try {
    const body = await getJSON<{
      run_id: string;
      turn_id: string;
      manifest_count: number;
      manifests: BackendContextManifest[];
    }>(`/council/runs/${runId}/turns/${turnId}/inspection`);
    return {
      runId: body.run_id,
      turnId: body.turn_id,
      manifestCount: body.manifest_count,
      manifests: (body.manifests ?? []).map(adaptManifest),
    };
  } catch (err) {
    // 404 → no manifest yet (drawer renders an empty state). Other errors
    // bubble so the shell can surface them. The shared `request` helper
    // throws `Error("HTTP 404 on ...")` — match on the message prefix.
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.startsWith("HTTP 404")) return null;
    throw err;
  }
}

export async function getStewardPacket(
  runId: string,
  packetId: string,
): Promise<Record<string, unknown> | null> {
  try {
    return await getJSON<Record<string, unknown>>(
      `/council/runs/${encodeURIComponent(runId)}/steward-packets/${encodeURIComponent(packetId)}`,
    );
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.startsWith("HTTP 404")) return null;
    throw err;
  }
}

// QA P1 #1 — round-level inspection. The Phase 5 compare strip only
// triggers when manifests.length >= 2; per-turn inspection returns one
// manifest per click, so the compare view was structurally unreachable
// from the real Inspect path. This fetches every manifest sharing the
// round across all members.
export async function getRoundInspection(
  runId: string,
  round: number,
): Promise<CouncilTurnInspection | null> {
  try {
    const body = await getJSON<{
      run_id: string;
      round: number;
      manifest_count: number;
      manifests: BackendContextManifest[];
    }>(`/council/runs/${runId}/rounds/${round}/inspection`);
    // Shape-compatible with CouncilTurnInspection — we synthesize a
    // turnId from the round so the drawer's existing prop contract holds.
    return {
      runId: body.run_id,
      turnId: `round-${body.round}`,
      manifestCount: body.manifest_count,
      manifests: (body.manifests ?? []).map(adaptManifest),
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.startsWith("HTTP 404")) return null;
    throw err;
  }
}

// ---------------------------------------------------------------------------
// F031-DEMO-CORPUS — ensure the F007 welcome corpus is on disk before the
// Council demo seed posts a room with `corpus_ids=["welcome"]`. Reuses the
// existing `POST /welcome/install` route (Task 2 decision); adapts its
// response shape into the camelCase `DemoCorpusResult` view type.
// ---------------------------------------------------------------------------

interface BackendWelcomeInstall {
  corpus_name: string;
  suggested_prompt?: string;
  files_ingested?: number;
  bytes_downloaded?: number;
  sha256?: string;
  f004_invoked?: boolean;
  f004_error?: string | null;
}

export async function ensureDemoCorpus(): Promise<DemoCorpusResult> {
  try {
    const body = await postJSON<BackendWelcomeInstall>("/welcome/install");
    if (body.f004_error) {
      return {
        corpusId: null,
        status: "failed",
        error: body.f004_error,
      };
    }
    return {
      corpusId: body.corpus_name,
      status: "ready",
      error: null,
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return {
      corpusId: null,
      status: "failed",
      error: msg,
    };
  }
}

export async function getRunAuditSummary(
  runId: string,
): Promise<CouncilRunAuditSummary> {
  const body = await getJSON<{
    run_id: string;
    status: string;
    residency_owner: string;
    totals: {
      turns: number;
      completed: number;
      blocked: number;
      skipped: number;
      cancelled: number;
      failed: number;
      local_calls: number;
      fake_calls: number;
      remote_calls: number;
    };
    terminal_reason?: string | null;
    paused_at?: string | null;
    cancel_requested_at?: string | null;
  }>(`/council/runs/${runId}/audit-summary`);
  return {
    runId: body.run_id,
    status: body.status,
    residencyOwner: body.residency_owner,
    totals: {
      turns: body.totals.turns,
      completed: body.totals.completed,
      blocked: body.totals.blocked,
      skipped: body.totals.skipped,
      cancelled: body.totals.cancelled,
      failed: body.totals.failed,
      localCalls: body.totals.local_calls,
      fakeCalls: body.totals.fake_calls,
      remoteCalls: body.totals.remote_calls,
    },
    terminalReason: body.terminal_reason ?? undefined,
    pausedAt: body.paused_at ?? undefined,
    cancelRequestedAt: body.cancel_requested_at ?? undefined,
  };
}

// ---------------------------------------------------------------------------
// F039 auto-apply merge-back to the user's tree (human-accept-gated).
// ---------------------------------------------------------------------------

export interface ApplyWorkspaceChange {
  path: string;
  status: string;
}

export interface ApplyWorkspacePreview {
  runId: string;
  source: string;
  hasChanges: boolean;
  changedFiles: ApplyWorkspaceChange[];
  conflicts: string[];
  diff: string;
}

export interface ApplyWorkspaceResult {
  applied: boolean;
  written: string[];
  deleted: string[];
  conflicts: string[];
}

interface BackendApplyPreview {
  run_id: string;
  source: string;
  has_changes: boolean;
  changed_files: ApplyWorkspaceChange[];
  conflicts: string[];
  diff: string;
}

// Returns null when the run has no auto-apply workspace (404) — the common
// case for runs that didn't use code_write auto_apply.
export async function getApplyWorkspace(
  runId: string,
): Promise<ApplyWorkspacePreview | null> {
  try {
    const body = await getJSON<BackendApplyPreview>(
      `/council/runs/${encodeURIComponent(runId)}/apply-workspace`,
    );
    return {
      runId: body.run_id,
      source: body.source,
      hasChanges: body.has_changes,
      changedFiles: body.changed_files ?? [],
      conflicts: body.conflicts ?? [],
      diff: body.diff ?? "",
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.startsWith("HTTP 404")) return null;
    throw err;
  }
}

export async function acceptApplyWorkspace(
  runId: string,
  opts: { allowConflicts?: boolean } = {},
): Promise<ApplyWorkspaceResult> {
  return postJSON<ApplyWorkspaceResult>(
    `/council/runs/${encodeURIComponent(runId)}/apply-workspace/accept`,
    { confirm: true, allow_conflicts: opts.allowConflicts ?? false },
    UI_ORIGIN,
  );
}
