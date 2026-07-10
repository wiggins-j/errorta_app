// F031 Phase 2 — Council UI view models (post-API-adapter shape).
// Invariant 11: api/council.ts is the only module that knows snake_case
// backend names; components consume the normalized camelCase types here.

export type CouncilUiRunState =
  | "idle"
  | "validating"
  | "ready"
  | "submitting"
  | "running"
  | "paused"
  | "awaiting_decision"
  | "cancelling"
  | "finalizing"
  | "done"
  | "failed"
  | "cancelled"
  | "unknown";

export interface CouncilRoomSummary {
  id: string;
  name: string;
  updatedAt: string;
  revision: number;
  statusHint: string;
}

/** F143: gateway token usage stamped on MEMBER_MESSAGE events. */
export interface CouncilEventUsage {
  inputTokens?: number;
  outputTokens?: number;
  cacheReadInputTokens?: number;
  cacheWriteInputTokens?: number;
}

export interface CouncilTranscriptEvent {
  id: string;
  runId: string;
  sequence: number;
  type: string;
  status: string;
  createdAt: string;
  memberId?: string;
  round?: number;
  payload: Record<string, unknown>;
  /** F143: present only on events that carried a gateway usage dict. */
  usage?: CouncilEventUsage;
  raw: unknown;
}

export interface CouncilRunStatus {
  runId: string;
  roomId?: string;
  state: CouncilUiRunState;
  backendStatus: string;
  /** The prompt the run was started with (from RunMeta) — shows the user's
   *  question for runs not started in this session (reload / phone auto-surface). */
  prompt?: string;
  terminalReason?: string;
  pausedAt?: string;
  cancelRequestedAt?: string;
}

export interface CouncilRunSummary {
  runId: string;
  roomId: string;
  state: CouncilUiRunState;
  backendStatus: string;
  updatedAt: string;
  eventCount: number;
  lastSequence: number;
}

export interface CouncilRunAuditSummary {
  runId: string;
  status: string;
  residencyOwner: string;
  totals: {
    turns: number;
    completed: number;
    blocked: number;
    skipped: number;
    cancelled: number;
    failed: number;
    localCalls: number;
    fakeCalls: number;
    remoteCalls: number;
  };
  terminalReason?: string;
  pausedAt?: string;
  cancelRequestedAt?: string;
}

// Phase 3/5 — F031-08 ContextManifest projection (post-adapter camelCase).
// Mirrors the /runs/{run_id}/turns/{turn_id}/inspection response.
// Invariant 5 (sealed): only sha256s, counts, classes — never raw text.

export interface CouncilContextSourceRef {
  class_: string;
  corpusId?: string | null;
  chunkId?: string | null;
  citationId?: string | null;
  contentSha256?: string | null;
  tokens?: number | null;
  transcriptEventId?: string | null;
  sequence?: number | null;
  packed?: string | null;
  toolCallId?: string | null;
  toolId?: string | null;
  argsSha256?: string | null;
  producedAt?: string | null;
  toolEgressClass?: string | null;
}

export interface CouncilCitationRef {
  citationId: string;
  contentSha256?: string | null;
  packed?: string | null;
}

export interface CouncilCompactionSegment {
  segmentIndex: number;
  roundRange: number[];
  artifactSha256?: string | null;
  mode?: string | null;
  eventIds: string[];
}

export interface CouncilStewardManifest {
  enabled?: boolean;
  fallback?: boolean;
  reason?: string | null;
  packetId?: string | null;
  contentSha256?: string | null;
  mode?: string | null;
  coverage?: {
    fromSequence?: number | null;
    toSequence?: number | null;
    sourceEventIds?: string[];
  };
  recentFullMessageCount?: number | null;
  omittedTranscriptEventCount?: number | null;
  effectiveTranscriptAccess?: string | null;
  [key: string]: unknown;
}

export interface CouncilContextManifest {
  manifestId: string;
  formatVersion: number;
  contextId: string;
  runId: string;
  turnId: string;
  memberId: string;
  payloadSha256: string;
  requestedContextAccess: string;
  effectiveContextAccess: string;
  requestedTranscriptAccess: string;
  effectiveTranscriptAccess: string;
  destinationScope: string;
  egressClass: string;
  sourceCounts: Record<string, number>;
  sourceRefs: CouncilContextSourceRef[];
  omitted: Array<Record<string, unknown>>;
  tokenEstimate: Record<string, unknown>;
  citationRefs?: CouncilCitationRef[];
  compaction?: {
    segments?: CouncilCompactionSegment[];
    [key: string]: unknown;
  };
  steward?: CouncilStewardManifest;
  packingContract?: string;
  packingOrderVariant?: string;
  cacheHints?: Array<Record<string, unknown>>;
  blockedReason?: string | null;
  transformManifestId?: string | null;
  visibilityPlanId?: string | null;
  f030AuditId?: string | null;
}

export interface CouncilTurnInspection {
  runId: string;
  turnId: string;
  manifestCount: number;
  manifests: CouncilContextManifest[];
}

// F037 expert callouts — queue record (camelCase view).
export interface CouncilCalloutRecord {
  calloutId: string;
  targetId: string;
  reasonCode: string;
  question: string;
  requestedBy: Record<string, unknown>;
  state: string; // requested|awaiting_approval|started|completed|rejected|failed
  advisory: boolean;
  approval: string | null; // null|approved|rejected
  rejectReason: string | null;
  answerEventId: string | null;
}

// F041 policy engine — pending user/operator approval surface.
export interface CouncilPendingDecision {
  decisionId: string;
  runId: string;
  phase: string;
  state: "pending" | "approved" | "rejected" | "expired" | string;
  reasonCode: string;
  requester: Record<string, unknown>;
  safeRequest: Record<string, unknown>;
  riskClass?: string | null;
  createdAt: string;
  resolvedAt?: string | null;
  resolvedBy?: string | null;
  stateWritesOnApprove: Array<Record<string, unknown>>;
  appliedStateWrites: Array<Record<string, unknown>>;
  metadata: Record<string, unknown>;
}

// F042 child runs — parent-linked async work records.
export interface CouncilChildRun {
  parentRunId: string;
  childRunId: string;
  memberId: string;
  taskKind: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled" | string;
  title: string;
  promptSha256: string;
  workerKind: string;
  createdAt: string;
  updatedAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  artifactRefs: Array<Record<string, unknown>>;
  summaryRef?: Record<string, unknown> | null;
  failure?: Record<string, unknown> | null;
  metadata: Record<string, unknown>;
}

export interface CouncilChildRunMessage {
  messageId: string;
  parentRunId: string;
  childRunId: string;
  messageKind: string;
  payloadPreview: string;
  payloadSha256: string;
  payloadBytes: number;
  createdAt: string;
  artifactRefs: Array<Record<string, unknown>>;
  metadata: Record<string, unknown>;
}

// F031-DEMO-CORPUS — demo-seed corpus-ensure result (camelCase view).
export interface DemoCorpusResult {
  corpusId: string | null;
  status: "ready" | "reused" | "failed";
  error: string | null;
}

export function mapBackendRunState(backendStatus: string): CouncilUiRunState {
  switch (backendStatus) {
    case "created":
      return "idle";
    case "running":
      return "running";
    case "paused":
      return "paused";
    case "finalizing":
      return "finalizing";
    case "awaiting_user_decision":
      return "awaiting_decision";
    case "completed":
      return "done";
    case "failed":
      return "failed";
    case "cancelled":
      return "cancelled";
    default:
      return "unknown";
  }
}
