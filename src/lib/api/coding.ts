// F087-06 — Coding Mode API client. All HTTP via sidecarFetch so the
// dynamically-resolved sidecar port + residency handling apply.
import { sidecarFetch } from "../api";
import { listCorpora } from "./corpus";

const UI_ORIGIN = { "x-errorta-origin": "tauri-ui" } as const;

export interface CodingProject {
  id: string;
  displayName?: string;
  northStar: string;
  definitionOfDone: string;
  target: string;
  repoPath?: string | null;
  status: string;
  revision: number;
  grounding?: ProjectCorpusBinding | null;
  // F093: the PM's justification for declaring the project done + when.
  completionSummary?: string;
  completedAt?: string;
  // F105: greenfield delivery location. `deliveryRoot` is the user-selected
  // parent dir (null = default); `plannedDeliveryDir` is `<root>/<id>` (null for
  // existing-repo targets).
  deliveryRoot?: string | null;
  plannedDeliveryDir?: string | null;
  // F102 RC2: the accept/delivered marker, surfaced so the frontend can gate the
  // GitHub publish (P3/P4) buttons on (delivered AND no open tasks).
  delivered?: boolean;
  deliveredAt?: string | null;
  // F121: whether the pre-first-run readiness gate has been confirmed. False on
  // a brand-new project, so the first Start Run opens the gate.
  runSetupConfirmed?: boolean;
  // F135: the current-focus directive ("what to work on right now").
  workRequest?: string;
  // F135: import provenance (github_clone | local_folder | local_folder_git_init),
  // or null for a hand-created project.
  importSource?: ImportSource | null;
  // F141 WS-I: project lifecycle phase. "north_star" while building the initial
  // North Star; "steering" once it's met (foundation merged for `new`; North
  // Star accepted for imported). The Current Focus panel shows in "steering".
  phase?: string;
  northStarMetAt?: string;
}

// F135 D10 — where an imported project came from (display/audit only).
export interface ImportSource {
  kind: string;
  originUrl?: string | null;
  clonedRef?: string | null;
  importedAt?: string | null;
}

// F135 S4 — a non-authoritative North Star inference proposal.
export interface NorthStarProposal {
  northStar: string;
  definitionOfDone: string;
  summary: string;
  detectedStack: string[];
  suggestedFirstTasks: string[];
  sourceRefs: string[];
  model: string;
  lowSignal: boolean;
  accepted: boolean;
}

export interface GithubAuthStatus {
  ghPresent: boolean;
  login: string | null;
}

export interface ImportJob {
  jobId: string;
  status: string; // cloning | cloned | connected | done | error | scanning
  message?: string | null;
  projectId?: string | null;
}

export interface CodingProjectSummary {
  id: string;
  displayName?: string;
  northStar: string;
  status: string;
  listStatus: string;
  listStatusReason: string;
}

export interface ProjectCorpusBinding {
  projectId: string;
  mode: string;
  corpusId: string | null;
  sourceRoot: string | null;
  indexVersion: number;
  lastRefreshAt: string | null;
  healthState: string;
  healthReason: string;
  bootstrapJobId: string | null;
}

export interface GroundingCorpusSummary {
  name: string;
  fileCount: number;
  readyCount: number;
}

export interface GroundingPayload {
  mode: "none" | "existing" | "build_from_repo" | "build_from_project";
  corpusId?: string | null;
  sourceRoot?: string | null;
}

export interface CreateCodingProjectInput {
  projectId: string;
  northStar: string;
  definitionOfDone?: string;
  target: string;
  repoPath?: string | null;
  // F105: parent dir for greenfield delivery; null = default
  // (~/Errorta Projects/<id>). Ignored for existing-repo targets.
  deliveryRoot?: string | null;
  // F135: current-focus directive at create time (optional).
  workRequest?: string;
  grounding?: GroundingPayload | null;
}

export interface ProjectGroundingCapabilities {
  available: boolean;
  version: string | null;
  source: string;
  supportsCorpusIds: boolean;
  supportsFileIngest: boolean;
  supportsRecordIngest: boolean;
  supportsMetadataFilters: boolean;
  supportsProvenanceMetadata: boolean;
  supportsIncrementalRefresh: boolean;
  supportsSupersession: boolean;
  supportsExportImport: boolean;
  localOnlyEmbedding: boolean;
  notes: string[];
}

export interface GroundingHit {
  content: string;
  corpusId: string;
  chunkId: string;
  score: number | null;
}

export type GroundingRetrieveStatus = "ok" | "no_corpus" | "empty" | "unavailable";

export interface GroundingRetrieveResult {
  status: GroundingRetrieveStatus;
  hits: GroundingHit[];
}

export interface GroundingBootstrapJob {
  jobId: string;
  corpusId: string;
  status: string; // queued | running | done | failed | interrupted
  adapterSource: string;
  documentsIngested: number;
  chunksAdded: number;
  errors: string[];
  endedAt: string | null;
}

export interface PmWorkingMemoryStatus {
  projectId: string;
  status: "local" | "mirrored" | "stale" | "unavailable" | string;
  memoryRef: string | null;
  corpusId: string | null;
  aiarMirrorStatus: string;
  aiarRetrievalStatus: string;
  lastGeneratedAt: string | null;
  lastMirroredAt: string | null;
  warnings: string[];
}

export type RuntimeKind = "static" | "web" | "api" | "cli" | "desktop" | "container" | "unknown" | (string & {});
export type RuntimeMode = "static" | "managed_local" | "container" | (string & {});
export type RuntimeSandbox = "auto" | "seatbelt" | "bwrap" | "docker" | "none" | (string & {});
export type RuntimeSessionState =
  | "starting"
  | "running"
  | "healthy"
  | "unhealthy"
  | "crashed"
  | "stopped"
  | (string & {});
export type RuntimeSandboxBackend = "seatbelt" | "bwrap" | "docker" | "none" | (string & {});

export interface RuntimeHealthSpec {
  type: string;
  url: string | null;
  timeoutSeconds: number | null;
}

export interface RuntimeDemoSpec {
  type: string;
  url: string | null;
  // F101-01: a `file` demo carries a workspace-relative path (kept for the
  // optional single-file fallback). Previously dropped on the wire round-trip.
  path: string | null;
  // F101-02: an optional per-profile CLI transcript time-box override (seconds).
  timeoutSeconds: number | null;
}

export interface RuntimePortSpec {
  name: string;
  containerPort: number | null;
  preferred: number | null;
}

export interface RuntimeProfile {
  schemaVersion: "coding_runtime_profile.v1" | string;
  profileId: string;
  projectId: string;
  kind: RuntimeKind;
  runtimeMode: RuntimeMode;
  workingDir: string;
  setup: string[][];
  start: string[];
  stop: string[] | null;
  health: RuntimeHealthSpec | null;
  demo: RuntimeDemoSpec | null;
  ports: RuntimePortSpec[];
  envRequired: string[];
  tests: string[];
  sandbox: RuntimeSandbox;
  safetyWarnings: string[];
  createdBy: "pm" | "dev" | "user" | "detector" | string;
  updatedAt: string;
}

export interface RuntimeHealthStatus {
  ok: boolean;
  detail: string;
}

export interface RuntimeSession {
  sessionId: string;
  profileId: string;
  state: RuntimeSessionState;
  pgid: number | null;
  startedAt: string;
  endedAt: string | null;
  allocatedPorts: number[];
  sandboxBackend: RuntimeSandboxBackend;
  healthStatus: RuntimeHealthStatus | null;
  logRef: string;
  exitCode: number | null;
  error: string | null;
  // F101-02: CLI transcript markers (flattened `_extras` from the backend).
  // `kind === "cli_transcript"` distinguishes a one-shot CLI run from a server
  // start; `argv` is the redacted effective command; `passed` is the derived
  // exit-0 result (null while still running / on a timeout-kill).
  kind: string | null;
  argv: string[] | null;
  passed: boolean | null;
  // F101-03 S2/S3: trust tier + display flag stamped on a windowed run.
  trustTier: number | null;
  // F101-03 S7: a captured screenshot of the app's own window (the demo asset),
  // work-root-relative, when a desktop launch produced one.
  screenshotRef: string | null;
}

export interface RuntimeLogs {
  lines: string[];
  truncated: boolean;
}

export interface RuntimeTestResult {
  kind: string;
  passed: boolean | null;
  detail: string;
  raw: Record<string, unknown>;
}

// F101-03 S1 — the universal Run front door. A grounded LaunchPlan (never a
// guessed command) that the panel previews before executing.
export interface RuntimeLaunchPlan {
  modality: string;
  launchKind: string;
  profileId: string;
  kind: string;
  start: string[];
  setup: string[][];
  workingDir: string;
  ports: RuntimePortSpec[];
  groundedBy: string;
  verifiedPaths: string[];
  trustTier: number;
  host: string;
  warnings: string[];
}

export interface RuntimeRunResolution {
  resolved: boolean;
  runnable: boolean;
  reason: string | null;
  plan: RuntimeLaunchPlan | null;
  session: RuntimeSession | null;
  lookedFor: string[];
  // F101-03 S3: the plan would run at T2 (no OS sandbox) and needs a second,
  // explicit reduced-isolation consent before it executes.
  requiresReducedIsolationConsent: boolean;
}

// F129/F135: the PM's per-task model binding. Same shape on a task record and a
// transcript turn.
export interface CodingModelAssignment {
  assignment_id?: string;
  route_id?: string;
  difficulty_tier?: string;
  task_type?: string;
  rationale?: string;
  source?: string;
  escalation_count?: number;
}

export interface CodingTask {
  taskId: string;
  title: string;
  role: string;
  state: string;
  // F141 WS-D: rework tasks carry a short "why sent back" (reasonSummary) plus a
  // fuller finding list (detail) and the linked PR (prId), so a wall of revise
  // rows is scannable.
  detail?: string;
  reasonSummary?: string;
  prId?: string | null;
  assigneeMemberId: string | null;
  dependsOn: string[];
  sourceSpecArtifactId?: string | null;
  sourcePlanArtifactId?: string | null;
  sourceSliceId?: string | null;
  governanceRequired?: boolean;
  // F135: the PM's model binding for this task, surfaced as a task-card chip.
  // The backend ships this on every task (Task.to_dict); prior adapters dropped it.
  modelAssignment?: CodingModelAssignment | null;
}

export interface CodingDecision {
  decisionId: string;
  title: string;
  choice: string;
  rationale: string;
  relatedTaskIds: string[];
}

export interface CodingArtifact {
  path: string;
  status: string;
  summary: string;
  onMaster: boolean;
}

export interface CodingFile {
  path: string;
  content: string | null;
  truncated: boolean;
  encoding: "utf-8" | "binary";
  bytes: number;
  onMaster: boolean;
  // F105: SHA-256 over the full raw blob; present only for editable utf-8 text
  // (absent for binary / when content is null). Used as the optimistic-
  // concurrency token on save.
  contentSha256?: string | null;
}

export interface CodingToolEvent {
  eventId: string;
  taskId: string;
  memberId: string;
  role: string;
  tool: string;
  status: string;
  path: string;
  error: string | null;
}

export interface CodingPmProgress {
  total: number;
  done: number;
  doing: number;
  todo: number;
  blocked: number;
  percent: number;
}

export interface CodingPmReply {
  role: string;
  kind: string;
  message: string;
  progress: CodingPmProgress | null;
  source: string;
  sourceIds: string[];
  at: string;
}

export interface CodingInterjection {
  message: string;
  at: string;
  pmReply: CodingPmReply | null;
}

export interface AutonomyPolicy {
  maxIterations: number;
  maxModelCalls: number | null;
  checkpointCadence: string;
  checkpointN: number;
}

export type GovernanceMode = "off" | "light" | "strict";
export type HumanCodeApproval = "none" | "per_slice" | "per_milestone" | "final_only";
export type CheckpointCadence =
  | "off"
  | "every_n_tasks"
  | "per_milestone"
  | "on_merge_ready";

export function parseGovernanceMode(value: unknown): GovernanceMode | undefined {
  return value === "off" || value === "light" || value === "strict" ? value : undefined;
}

export function parseHumanCodeApproval(value: unknown): HumanCodeApproval | undefined {
  return value === "none" ||
    value === "per_slice" ||
    value === "per_milestone" ||
    value === "final_only"
    ? value
    : undefined;
}

export function parseCheckpointCadence(value: unknown): CheckpointCadence | undefined {
  return value === "off" ||
    value === "every_n_tasks" ||
    value === "per_milestone" ||
    value === "on_merge_ready"
    ? value
    : undefined;
}

export interface GovernanceState {
  mode: GovernanceMode;
  phase: string;
  humanCodeApproval: HumanCodeApproval;
  activeArtifactIds: Record<string, string>;
  // F117: showstopper toggle + Progress Monitor thresholds.
  blockOnProblems: boolean;
  monitor: Record<string, unknown>;
  updatedAt: string;
}

// F117 attention signals (Problems + Alerts).
export type AttentionKind = "problem" | "alert";
export interface AttentionSuggestion {
  id: string;
  label: string;
  detail?: string;
}
export interface AttentionSignal {
  id: string;
  kind: AttentionKind;
  blocking: boolean;
  source: string;
  stage: string;
  title: string;
  summary: string;
  pmEvaluation: string | null;
  suggestions: AttentionSuggestion[];
  state: string;
  resolution: Record<string, unknown> | null;
  // F120: structured evidence (member_health Problems carry member_id /
  // coding_role / gateway_route_id / reason / detail / remediation / attempts).
  context?: Record<string, unknown>;
  createdAt: string;
}
export interface AttentionList {
  signals: AttentionSignal[];
  blocksStage: boolean;
}
export type AttentionAction = "accept" | "correct" | "defer" | "dismiss";

export interface GovernanceArtifact {
  artifactId: string;
  artifactKind: string;
  version: number;
  state: string;
  title: string;
  bodyMarkdown?: string;
  bodyJson?: Record<string, unknown>;
  sourceRefs: string[];
  supersedesArtifactId: string | null;
  createdAt: string;
}

export interface GovernanceFinding {
  severity: string;
  title: string;
  body: string;
  blocking: boolean;
}

export interface GovernanceReview {
  reviewId: string;
  artifactId: string;
  reviewerMemberId: string;
  verdict: string;
  findings: GovernanceFinding[];
  createdAt: string;
}

export interface GovernanceApproval {
  approvalId: string;
  kind: string;
  artifactId: string;
  requiredActor: string;
  state: string;
  requestedByMemberId: string;
  resolvedBy: string | null;
  feedback: string;
  createdAt: string;
  resolvedAt: string | null;
}

export interface GovernancePlanSlice {
  sliceId: string;
  title: string;
  detail: string;
  dependsOn: string[];
  doneWhen: string[];
  tests: string[];
  reviewFocus: string[];
}

export interface GovernanceSummary {
  state: GovernanceState;
  artifacts: GovernanceArtifact[];
  reviews: GovernanceReview[];
  approvals: GovernanceApproval[];
  planSlices: GovernancePlanSlice[];
}

// F100-01: a plain-language projection of governance state for the at-a-glance
// status strip (stage + status + stepper). Pure read-only — mirrors Team Log.
export type GovernanceStage = "idle" | "brainstorm" | "spec" | "plan" | "build" | "done";
export type GovernanceStatusKind =
  | "drafting"
  | "under_review"
  | "changes_requested"
  | "approved"
  | "building"
  // F100-02: governance loop not converging — the run pauses and awaits the human.
  | "stuck";
export type GovernanceStepState =
  | "approved"
  | "under_review"
  | "changes_requested"
  | "drafting"
  | "building"
  | "stuck"
  | "pending";
export type GovernanceReviewPass = "reviewer" | "pm";

export interface GovernanceStep {
  stage: GovernanceStage;
  state: GovernanceStepState;
}

export interface GovernanceStatus {
  mode: GovernanceMode;
  stage: GovernanceStage;
  status: GovernanceStatusKind | null;
  headline: string;
  actorMemberId: string | null;
  actorLabel: string | null;
  reviewPass: GovernanceReviewPass | null;
  steps: GovernanceStep[];
  buildProgress: { done: number; total: number } | null;
  // F100-02: set when the loop is not converging and needs a human decision.
  needsHuman?: boolean;
  // F100-02: how many review rounds the current stage has gone through.
  reviewRound?: number;
}

function bindingFrom(raw: Record<string, unknown> | null | undefined): ProjectCorpusBinding | null {
  if (!raw) return null;
  return {
    projectId: String(raw.project_id ?? ""),
    mode: String(raw.mode ?? "none"),
    corpusId: (raw.corpus_id as string | null) ?? null,
    sourceRoot: (raw.source_root as string | null) ?? null,
    indexVersion: Number(raw.index_version ?? 0),
    lastRefreshAt: (raw.last_refresh_at as string | null) ?? null,
    healthState: String(raw.health_state ?? "missing"),
    healthReason: String(raw.health_reason ?? ""),
    bootstrapJobId: (raw.bootstrap_job_id as string | null) ?? null,
  };
}

function projectFrom(raw: Record<string, unknown>): CodingProject {
  return {
    id: String(raw.id ?? ""),
    displayName: typeof raw.display_name === "string" ? raw.display_name : undefined,
    northStar: String(raw.north_star ?? ""),
    definitionOfDone: String(raw.definition_of_done ?? ""),
    target: String(raw.target ?? "new"),
    repoPath: (raw.repo_path as string | null) ?? null,
    status: String(raw.status ?? "active"),
    revision: Number(raw.revision ?? 1),
    grounding: bindingFrom(raw.grounding as Record<string, unknown> | null | undefined),
    completionSummary: String(raw.completion_summary ?? ""),
    completedAt: String(raw.completed_at ?? ""),
    deliveryRoot: (raw.delivery_root as string | null) ?? null,
    plannedDeliveryDir: (raw.planned_delivery_dir as string | null) ?? null,
    delivered: Boolean(raw.delivered),
    deliveredAt: (raw.delivered_at as string | null) ?? null,
    runSetupConfirmed: Boolean(raw.run_setup_confirmed),
    workRequest: String(raw.work_request ?? ""),
    importSource: importSourceFrom(raw.import_source as Record<string, unknown> | null | undefined),
    phase: String(raw.phase ?? "north_star"),
    northStarMetAt: String(raw.north_star_met_at ?? ""),
  };
}

function importSourceFrom(
  raw: Record<string, unknown> | null | undefined,
): ImportSource | null {
  if (!raw || typeof raw !== "object") return null;
  return {
    kind: String(raw.kind ?? ""),
    originUrl: (raw.origin_url as string | null) ?? null,
    clonedRef: (raw.cloned_ref as string | null) ?? null,
    importedAt: (raw.imported_at as string | null) ?? null,
  };
}

function taskFrom(raw: Record<string, unknown>): CodingTask {
  return {
    taskId: String(raw.task_id ?? ""),
    title: String(raw.title ?? ""),
    role: String(raw.role ?? ""),
    state: String(raw.state ?? "todo"),
    detail: (raw.detail as string) ?? "",
    reasonSummary: (raw.reason_summary as string) ?? "",
    prId: (raw.pr_id as string | null) ?? null,
    assigneeMemberId: (raw.assignee_member_id as string | null) ?? null,
    dependsOn: Array.isArray(raw.depends_on) ? (raw.depends_on as string[]) : [],
    sourceSpecArtifactId: (raw.source_spec_artifact_id as string | null) ?? null,
    sourcePlanArtifactId: (raw.source_plan_artifact_id as string | null) ?? null,
    sourceSliceId: (raw.source_slice_id as string | null) ?? null,
    governanceRequired: Boolean(raw.governance_required),
    modelAssignment: (
      typeof raw.model_assignment === "object" && raw.model_assignment !== null
        ? raw.model_assignment
        : null
    ) as CodingModelAssignment | null,
  };
}

function policyFrom(raw: Record<string, unknown>): AutonomyPolicy {
  return {
    maxIterations: Number(raw.max_iterations ?? 200),
    maxModelCalls: (raw.max_model_calls as number | null) ?? null,
    checkpointCadence: String(raw.checkpoint_cadence ?? "per_milestone"),
    checkpointN: Number(raw.checkpoint_n ?? 5),
  };
}

function governanceStateFrom(raw: Record<string, unknown> | null | undefined): GovernanceState {
  return {
    mode: parseGovernanceMode(raw?.mode) ?? "off",
    phase: String(raw?.phase ?? "idle"),
    humanCodeApproval: parseHumanCodeApproval(raw?.human_code_approval) ?? "final_only",
    activeArtifactIds: (raw?.active_artifact_ids as Record<string, string>) ?? {},
    blockOnProblems: raw?.block_on_problems !== false, // absent => on (F117 default)
    monitor: (raw?.monitor as Record<string, unknown>) ?? {},
    updatedAt: String(raw?.updated_at ?? ""),
  };
}

function attentionSignalFrom(raw: Record<string, unknown>): AttentionSignal {
  return {
    id: String(raw.id ?? ""),
    kind: raw.kind === "alert" ? "alert" : "problem",
    blocking: Boolean(raw.blocking),
    source: String(raw.source ?? ""),
    stage: String(raw.stage ?? ""),
    title: String(raw.title ?? ""),
    summary: String(raw.summary ?? ""),
    pmEvaluation: raw.pm_evaluation == null ? null : String(raw.pm_evaluation),
    suggestions: ((raw.suggestions as Array<Record<string, unknown>>) ?? []).map((s) => ({
      id: String(s.id ?? ""),
      label: String(s.label ?? ""),
      detail: s.detail == null ? undefined : String(s.detail),
    })),
    state: String(raw.state ?? "open"),
    resolution: (raw.resolution as Record<string, unknown> | null) ?? null,
    context: (raw.context as Record<string, unknown>) ?? {},
    createdAt: String(raw.created_at ?? ""),
  };
}

function governanceArtifactFrom(raw: Record<string, unknown>): GovernanceArtifact {
  return {
    artifactId: String(raw.artifact_id ?? ""),
    artifactKind: String(raw.artifact_kind ?? ""),
    version: Number(raw.version ?? 1),
    state: String(raw.state ?? ""),
    title: String(raw.title ?? ""),
    bodyMarkdown: raw.body_markdown == null ? undefined : String(raw.body_markdown),
    bodyJson: (raw.body_json as Record<string, unknown> | undefined) ?? undefined,
    sourceRefs: Array.isArray(raw.source_refs) ? (raw.source_refs as string[]) : [],
    supersedesArtifactId:
      raw.supersedes_artifact_id == null ? null : String(raw.supersedes_artifact_id),
    createdAt: String(raw.created_at ?? ""),
  };
}

function governanceFindingFrom(raw: Record<string, unknown>): GovernanceFinding {
  return {
    severity: String(raw.severity ?? "medium"),
    title: String(raw.title ?? ""),
    body: String(raw.body ?? ""),
    blocking: Boolean(raw.blocking),
  };
}

function governanceReviewFrom(raw: Record<string, unknown>): GovernanceReview {
  return {
    reviewId: String(raw.review_id ?? ""),
    artifactId: String(raw.artifact_id ?? ""),
    reviewerMemberId: String(raw.reviewer_member_id ?? ""),
    verdict: String(raw.verdict ?? ""),
    findings: ((raw.findings as Array<Record<string, unknown>>) ?? []).map(governanceFindingFrom),
    createdAt: String(raw.created_at ?? ""),
  };
}

function governanceApprovalFrom(raw: Record<string, unknown>): GovernanceApproval {
  return {
    approvalId: String(raw.approval_id ?? ""),
    kind: String(raw.kind ?? ""),
    artifactId: String(raw.artifact_id ?? ""),
    requiredActor: String(raw.required_actor ?? "user"),
    state: String(raw.state ?? "pending"),
    requestedByMemberId: String(raw.requested_by_member_id ?? ""),
    resolvedBy: raw.resolved_by == null ? null : String(raw.resolved_by),
    feedback: String(raw.feedback ?? ""),
    createdAt: String(raw.created_at ?? ""),
    resolvedAt: raw.resolved_at == null ? null : String(raw.resolved_at),
  };
}

function governancePlanSliceFrom(raw: Record<string, unknown>): GovernancePlanSlice {
  return {
    sliceId: String(raw.slice_id ?? ""),
    title: String(raw.title ?? ""),
    detail: String(raw.detail ?? ""),
    dependsOn: Array.isArray(raw.depends_on) ? (raw.depends_on as string[]) : [],
    doneWhen: Array.isArray(raw.done_when) ? (raw.done_when as string[]) : [],
    tests: Array.isArray(raw.tests) ? (raw.tests as string[]) : [],
    reviewFocus: Array.isArray(raw.review_focus) ? (raw.review_focus as string[]) : [],
  };
}

function governanceSummaryFrom(raw: Record<string, unknown>): GovernanceSummary {
  return {
    state: governanceStateFrom(raw.state as Record<string, unknown> | null | undefined),
    artifacts: ((raw.artifacts as Array<Record<string, unknown>>) ?? []).map(
      governanceArtifactFrom,
    ),
    reviews: ((raw.reviews as Array<Record<string, unknown>>) ?? []).map(governanceReviewFrom),
    approvals: ((raw.approvals as Array<Record<string, unknown>>) ?? []).map(
      governanceApprovalFrom,
    ),
    planSlices: ((raw.plan_slices as Array<Record<string, unknown>>) ?? []).map(
      governancePlanSliceFrom,
    ),
  };
}

const GOVERNANCE_STAGES: GovernanceStage[] = ["idle", "brainstorm", "spec", "plan", "build", "done"];
const GOVERNANCE_STATUS_KINDS: GovernanceStatusKind[] = [
  "drafting",
  "under_review",
  "changes_requested",
  "approved",
  "building",
  "stuck",
];
const GOVERNANCE_STEP_STATES: GovernanceStepState[] = [
  "approved",
  "under_review",
  "changes_requested",
  "drafting",
  "building",
  "stuck",
  "pending",
];

function governanceStepFrom(raw: Record<string, unknown>): GovernanceStep {
  const stage = String(raw.stage ?? "");
  const state = String(raw.state ?? "");
  return {
    stage: (GOVERNANCE_STAGES.includes(stage as GovernanceStage)
      ? stage
      : "idle") as GovernanceStage,
    state: (GOVERNANCE_STEP_STATES.includes(state as GovernanceStepState)
      ? state
      : "pending") as GovernanceStepState,
  };
}

// F100-01: snake_case GovernanceStatus -> camelCase. Tolerant of a null/absent
// payload (off projects, older sidecars) -> a hidden "off" status.
function governanceStatusFrom(
  raw: Record<string, unknown> | null | undefined,
): GovernanceStatus {
  const mode = String(raw?.mode ?? "off");
  const stage = String(raw?.stage ?? "idle");
  const status = raw?.status == null ? null : String(raw.status);
  const reviewPass = raw?.review_pass == null ? null : String(raw.review_pass);
  const bp = raw?.build_progress as Record<string, unknown> | null | undefined;
  return {
    mode: mode === "strict" || mode === "light" ? mode : "off",
    stage: (GOVERNANCE_STAGES.includes(stage as GovernanceStage)
      ? stage
      : "idle") as GovernanceStage,
    status:
      status != null && GOVERNANCE_STATUS_KINDS.includes(status as GovernanceStatusKind)
        ? (status as GovernanceStatusKind)
        : null,
    headline: String(raw?.headline ?? ""),
    actorMemberId: raw?.actor_member_id == null ? null : String(raw.actor_member_id),
    actorLabel: raw?.actor_label == null ? null : String(raw.actor_label),
    reviewPass:
      reviewPass === "reviewer" || reviewPass === "pm"
        ? (reviewPass as GovernanceReviewPass)
        : null,
    steps: Array.isArray(raw?.steps)
      ? (raw.steps as Array<Record<string, unknown>>).map(governanceStepFrom)
      : [],
    buildProgress: bp
      ? { done: Number(bp.done ?? 0), total: Number(bp.total ?? 0) }
      : null,
    // F100-02: optional convergence flags (absent on older sidecars).
    needsHuman: raw?.needs_human == null ? undefined : Boolean(raw.needs_human),
    reviewRound: raw?.review_round == null ? undefined : Number(raw.review_round),
  };
}

function pmReplyFrom(raw: Record<string, unknown> | null | undefined): CodingPmReply | null {
  if (!raw) return null;
  const progress = raw.progress as Record<string, unknown> | null | undefined;
  return {
    role: String(raw.role ?? "pm"),
    kind: String(raw.kind ?? ""),
    message: String(raw.message ?? ""),
    progress: progress
      ? {
          total: Number(progress.total ?? 0),
          done: Number(progress.done ?? 0),
          doing: Number(progress.doing ?? 0),
          todo: Number(progress.todo ?? 0),
          blocked: Number(progress.blocked ?? 0),
          percent: Number(progress.percent ?? 0),
        }
      : null,
    source: String(raw.source ?? ""),
    sourceIds: Array.isArray(raw.source_ids) ? (raw.source_ids as string[]) : [],
    at: String(raw.at ?? ""),
  };
}

function interjectionFrom(raw: Record<string, unknown>): CodingInterjection {
  return {
    message: String(raw.message ?? ""),
    at: String(raw.at ?? ""),
    pmReply: pmReplyFrom(raw.pm_reply as Record<string, unknown> | null | undefined),
  };
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map((v) => String(v)) : [];
}

function argvArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map((v) => String(v)) : [];
}

function argvList(value: unknown): string[][] {
  return Array.isArray(value)
    ? value.map((v) => argvArray(v)).filter((argv) => argv.length > 0)
    : [];
}

function healthSpecFrom(raw: unknown): RuntimeHealthSpec | null {
  if (!raw || typeof raw !== "object") return null;
  const h = raw as Record<string, unknown>;
  return {
    type: String(h.type ?? "none"),
    url: h.url == null ? null : String(h.url),
    timeoutSeconds:
      typeof h.timeout_seconds === "number"
        ? h.timeout_seconds
        : typeof h.timeoutSeconds === "number"
          ? h.timeoutSeconds
          : null,
  };
}

function demoSpecFrom(raw: unknown): RuntimeDemoSpec | null {
  if (!raw || typeof raw !== "object") return null;
  const d = raw as Record<string, unknown>;
  return {
    type: String(d.type ?? "none"),
    url: d.url == null ? null : String(d.url),
    // F101-01: do not drop `path` (file-fallback) on the round-trip.
    path: d.path == null ? null : String(d.path),
    // F101-02: per-profile CLI time-box override.
    timeoutSeconds:
      typeof d.timeout_seconds === "number"
        ? d.timeout_seconds
        : typeof d.timeoutSeconds === "number"
          ? d.timeoutSeconds
          : null,
  };
}

function portSpecFrom(raw: Record<string, unknown>): RuntimePortSpec {
  return {
    name: String(raw.name ?? "port"),
    containerPort:
      typeof raw.container_port === "number"
        ? raw.container_port
        : typeof raw.containerPort === "number"
          ? raw.containerPort
          : null,
    preferred:
      typeof raw.preferred === "number"
        ? raw.preferred
        : null,
  };
}

export function runtimeProfileFrom(raw: Record<string, unknown>): RuntimeProfile {
  return {
    schemaVersion: String(
      raw.schema_version ?? raw.schemaVersion ?? "coding_runtime_profile.v1",
    ),
    profileId: String(raw.profile_id ?? raw.profileId ?? "default"),
    projectId: String(raw.project_id ?? raw.projectId ?? ""),
    kind: String(raw.kind ?? "unknown"),
    runtimeMode: String(raw.runtime_mode ?? raw.runtimeMode ?? "managed_local"),
    workingDir: String(raw.working_dir ?? raw.workingDir ?? "."),
    setup: argvList(raw.setup),
    start: argvArray(raw.start),
    stop: raw.stop == null ? null : argvArray(raw.stop),
    health: healthSpecFrom(raw.health),
    demo: demoSpecFrom(raw.demo),
    ports: Array.isArray(raw.ports)
      ? (raw.ports as Array<Record<string, unknown>>).map(portSpecFrom)
      : [],
    envRequired: stringArray(raw.env_required ?? raw.envRequired),
    tests: stringArray(raw.tests),
    sandbox: String(raw.sandbox ?? "auto"),
    safetyWarnings: stringArray(raw.safety_warnings ?? raw.safetyWarnings),
    createdBy: String(raw.created_by ?? raw.createdBy ?? "detector"),
    updatedAt: String(raw.updated_at ?? raw.updatedAt ?? ""),
  };
}

export function runtimeProfileToWire(profile: RuntimeProfile): Record<string, unknown> {
  return {
    schema_version: profile.schemaVersion,
    profile_id: profile.profileId,
    project_id: profile.projectId,
    kind: profile.kind,
    runtime_mode: profile.runtimeMode,
    working_dir: profile.workingDir,
    setup: profile.setup,
    start: profile.start,
    stop: profile.stop,
    health: profile.health
      ? {
          type: profile.health.type,
          url: profile.health.url,
          timeout_seconds: profile.health.timeoutSeconds,
        }
      : null,
    demo: profile.demo
      ? {
          type: profile.demo.type,
          url: profile.demo.url,
          // F101-01/02: round-trip the file path + CLI time-box so an editor
          // save never silently drops them.
          ...(profile.demo.path != null ? { path: profile.demo.path } : {}),
          ...(profile.demo.timeoutSeconds != null
            ? { timeout_seconds: profile.demo.timeoutSeconds }
            : {}),
        }
      : null,
    ports: profile.ports.map((p) => ({
      name: p.name,
      container_port: p.containerPort,
      preferred: p.preferred,
    })),
    env_required: profile.envRequired,
    tests: profile.tests,
    sandbox: profile.sandbox,
    safety_warnings: profile.safetyWarnings,
    created_by: profile.createdBy,
    updated_at: profile.updatedAt,
  };
}

function runtimeHealthStatusFrom(raw: unknown): RuntimeHealthStatus | null {
  if (!raw || typeof raw !== "object") return null;
  const h = raw as Record<string, unknown>;
  return {
    ok: Boolean(h.ok),
    detail: String(h.detail ?? ""),
  };
}

function runtimeSessionFrom(raw: Record<string, unknown>): RuntimeSession {
  return {
    sessionId: String(raw.session_id ?? raw.sessionId ?? ""),
    profileId: String(raw.profile_id ?? raw.profileId ?? ""),
    state: String(raw.state ?? "stopped"),
    pgid: typeof raw.pgid === "number" ? raw.pgid : null,
    startedAt: String(raw.started_at ?? raw.startedAt ?? ""),
    endedAt: raw.ended_at == null && raw.endedAt == null
      ? null
      : String(raw.ended_at ?? raw.endedAt),
    allocatedPorts: Array.isArray(raw.allocated_ports)
      ? (raw.allocated_ports as unknown[]).map(Number).filter(Number.isFinite)
      : Array.isArray(raw.allocatedPorts)
        ? (raw.allocatedPorts as unknown[]).map(Number).filter(Number.isFinite)
        : [],
    sandboxBackend: String(raw.sandbox_backend ?? raw.sandboxBackend ?? "none"),
    healthStatus: runtimeHealthStatusFrom(raw.health_status ?? raw.healthStatus),
    logRef: String(raw.log_ref ?? raw.logRef ?? ""),
    exitCode:
      typeof raw.exit_code === "number"
        ? raw.exit_code
        : typeof raw.exitCode === "number"
          ? raw.exitCode
          : null,
    error: raw.error == null ? null : String(raw.error),
    kind: raw.kind == null ? null : String(raw.kind),
    argv: Array.isArray(raw.argv) ? raw.argv.map(String) : null,
    passed: typeof raw.passed === "boolean" ? raw.passed : null,
    trustTier:
      typeof raw.trust_tier === "number"
        ? raw.trust_tier
        : typeof raw.trustTier === "number"
          ? raw.trustTier
          : null,
    screenshotRef:
      raw.screenshot_ref == null && raw.screenshotRef == null
        ? null
        : String(raw.screenshot_ref ?? raw.screenshotRef),
  };
}

function runtimeLaunchPlanFrom(raw: Record<string, unknown>): RuntimeLaunchPlan {
  return {
    modality: String(raw.modality ?? ""),
    launchKind: String(raw.launch_kind ?? raw.launchKind ?? raw.modality ?? ""),
    profileId: String(raw.profile_id ?? raw.profileId ?? ""),
    kind: String(raw.kind ?? ""),
    start: stringArray(raw.start),
    setup: Array.isArray(raw.setup)
      ? (raw.setup as unknown[]).map((step) => stringArray(step))
      : [],
    workingDir: String(raw.working_dir ?? raw.workingDir ?? "."),
    ports: Array.isArray(raw.ports)
      ? (raw.ports as Record<string, unknown>[]).map(portSpecFrom)
      : [],
    groundedBy: String(raw.grounded_by ?? raw.groundedBy ?? ""),
    verifiedPaths: stringArray(raw.verified_paths ?? raw.verifiedPaths),
    trustTier:
      typeof raw.trust_tier === "number"
        ? raw.trust_tier
        : typeof raw.trustTier === "number"
          ? raw.trustTier
          : 0,
    host: String(raw.host ?? ""),
    warnings: stringArray(raw.warnings),
  };
}

function runtimeRunResolutionFrom(raw: Record<string, unknown>): RuntimeRunResolution {
  const plan = raw.plan;
  const session = raw.session;
  return {
    resolved: raw.resolved === true,
    runnable: raw.runnable === true,
    reason: raw.reason == null ? null : String(raw.reason),
    plan:
      plan && typeof plan === "object"
        ? runtimeLaunchPlanFrom(plan as Record<string, unknown>)
        : null,
    session:
      session && typeof session === "object"
        ? runtimeSessionFrom(session as Record<string, unknown>)
        : null,
    lookedFor: stringArray(raw.looked_for ?? raw.lookedFor),
    requiresReducedIsolationConsent:
      raw.requires_reduced_isolation_consent === true ||
      raw.requiresReducedIsolationConsent === true,
  };
}

function runtimeTestResultFrom(raw: Record<string, unknown>): RuntimeTestResult {
  return {
    kind: String(raw.kind ?? raw.test_kind ?? "runtime"),
    passed:
      typeof raw.passed === "boolean"
        ? raw.passed
        : typeof raw.ok === "boolean"
          ? raw.ok
          : null,
    detail: String(raw.detail ?? raw.message ?? ""),
    raw,
  };
}

async function jsonOk(res: Response, what: string): Promise<unknown> {
  if (!res.ok) throw new Error(`${what} failed (${res.status})`);
  return res.json();
}

function runtimeBase(projectId: string): string {
  return `/coding/projects/${encodeURIComponent(projectId)}/runtime`;
}

export async function listRuntimeProfiles(projectId: string): Promise<RuntimeProfile[]> {
  const r = (await jsonOk(
    await sidecarFetch(`${runtimeBase(projectId)}/profiles`),
    "list runtime profiles",
  )) as { profiles: Array<Record<string, unknown>> };
  return (r.profiles ?? []).map(runtimeProfileFrom);
}

export async function upsertRuntimeProfile(
  projectId: string,
  profile: RuntimeProfile,
): Promise<RuntimeProfile> {
  const r = (await jsonOk(
    await sidecarFetch(
      `${runtimeBase(projectId)}/profiles/${encodeURIComponent(profile.profileId)}`,
      {
        method: "PUT",
        headers: UI_ORIGIN,
        body: JSON.stringify(runtimeProfileToWire(profile)),
      },
    ),
    "upsert runtime profile",
  )) as { profile: Record<string, unknown> };
  return runtimeProfileFrom(r.profile);
}

export async function detectRuntimeProfiles(projectId: string): Promise<RuntimeProfile[]> {
  const r = (await jsonOk(
    await sidecarFetch(`${runtimeBase(projectId)}/detect`, {
      method: "POST",
      headers: UI_ORIGIN,
      body: "{}",
    }),
    "detect runtime profiles",
  )) as { proposed: Array<Record<string, unknown>> };
  return (r.proposed ?? []).map(runtimeProfileFrom);
}

// F101-03 S1 — the universal Run front door. Resolves a grounded LaunchPlan and
// either previews it (confirm omitted/false) or executes it (confirm:true). An
// ungrounded/unknown project resolves to a `lookedFor` checklist, not an error.
export async function resolveRuntimeRun(
  projectId: string,
  opts: { confirm?: boolean; confirmReducedIsolation?: boolean } = {},
): Promise<RuntimeRunResolution> {
  const r = (await jsonOk(
    await sidecarFetch(`${runtimeBase(projectId)}/run`, {
      method: "POST",
      headers: UI_ORIGIN,
      body: JSON.stringify({
        confirm: opts.confirm === true,
        confirm_reduced_isolation: opts.confirmReducedIsolation === true,
      }),
    }),
    "resolve runtime run",
  )) as Record<string, unknown>;
  return runtimeRunResolutionFrom(r);
}

export async function setupRuntimeProfile(
  projectId: string,
  profileId: string,
): Promise<RuntimeSession> {
  const r = (await jsonOk(
    await sidecarFetch(
      `${runtimeBase(projectId)}/${encodeURIComponent(profileId)}/setup`,
      {
        method: "POST",
        headers: UI_ORIGIN,
        body: JSON.stringify({ confirm: true }),
      },
    ),
    "setup runtime",
  )) as { session: Record<string, unknown> };
  return runtimeSessionFrom(r.session);
}

export async function startRuntimeProfile(
  projectId: string,
  profileId: string,
): Promise<RuntimeSession> {
  const r = (await jsonOk(
    await sidecarFetch(
      `${runtimeBase(projectId)}/${encodeURIComponent(profileId)}/start`,
      {
        method: "POST",
        headers: UI_ORIGIN,
        body: "{}",
      },
    ),
    "start runtime",
  )) as { session: Record<string, unknown> };
  return runtimeSessionFrom(r.session);
}

/**
 * F101-02: a typed error for the `/run-cli` 422 (bad extra-args — unbalanced
 * quotes, too long, too many tokens, or a non-string). The UI surfaces
 * `.message` next to the args input. Other failures throw a plain `Error`.
 */
export class RuntimeCliArgsError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RuntimeCliArgsError";
  }
}

/**
 * F101-02: run a `kind: "cli"` (managed_local) profile once as a time-boxed
 * transcript run — the CLI analog of "Open demo". `extraArgs` is parsed
 * argv-style by the backend (shlex, no shell) and appended to `start`. Returns
 * the initial `RuntimeSession` (`kind: "cli_transcript"`); poll the existing
 * sessions + logs routes for the terminal transcript + exit code. A 422 (bad
 * args) raises `RuntimeCliArgsError`.
 */
export async function runCliTranscript(
  projectId: string,
  profileId: string,
  opts: { extraArgs?: string; timeoutSeconds?: number } = {},
): Promise<RuntimeSession> {
  const body: Record<string, unknown> = {};
  if (opts.extraArgs != null && opts.extraArgs !== "") body.extra_args = opts.extraArgs;
  if (opts.timeoutSeconds != null) body.timeout_seconds = opts.timeoutSeconds;
  const res = await sidecarFetch(
    `${runtimeBase(projectId)}/${encodeURIComponent(profileId)}/run-cli`,
    {
      method: "POST",
      headers: { ...UI_ORIGIN, "content-type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  if (res.status === 422) {
    let detail = "Invalid extra arguments.";
    try {
      const parsed = (await res.json()) as { detail?: unknown };
      if (typeof parsed.detail === "string") detail = parsed.detail;
    } catch {
      /* non-JSON body */
    }
    throw new RuntimeCliArgsError(detail);
  }
  const r = (await jsonOk(res, "run cli transcript")) as { session: Record<string, unknown> };
  return runtimeSessionFrom(r.session);
}

export async function stopRuntimeProfile(projectId: string, profileId: string): Promise<boolean> {
  const r = (await jsonOk(
    await sidecarFetch(
      `${runtimeBase(projectId)}/${encodeURIComponent(profileId)}/stop`,
      {
        method: "POST",
        headers: UI_ORIGIN,
        body: "{}",
      },
    ),
    "stop runtime",
  )) as { stopped: boolean };
  return Boolean(r.stopped);
}

export async function getRuntimeSession(
  projectId: string,
  sessionId: string,
): Promise<RuntimeSession> {
  const r = (await jsonOk(
    await sidecarFetch(
      `${runtimeBase(projectId)}/sessions/${encodeURIComponent(sessionId)}`,
    ),
    "get runtime session",
  )) as { session: Record<string, unknown> };
  return runtimeSessionFrom(r.session);
}

export async function getRuntimeSessionLogs(
  projectId: string,
  sessionId: string,
): Promise<RuntimeLogs> {
  const r = (await jsonOk(
    await sidecarFetch(
      `${runtimeBase(projectId)}/sessions/${encodeURIComponent(sessionId)}/logs`,
    ),
    "get runtime logs",
  )) as { lines: string[]; truncated?: boolean };
  return {
    lines: Array.isArray(r.lines) ? r.lines.map(String) : [],
    truncated: Boolean(r.truncated),
  };
}

export async function runRuntimeHealthCheck(
  projectId: string,
  profileId: string,
): Promise<RuntimeHealthStatus> {
  const r = (await jsonOk(
    await sidecarFetch(
      `${runtimeBase(projectId)}/${encodeURIComponent(profileId)}/health-check`,
      {
        method: "POST",
        headers: UI_ORIGIN,
        body: "{}",
      },
    ),
    "runtime health check",
  )) as { health_status: Record<string, unknown> };
  return runtimeHealthStatusFrom(r.health_status) ?? { ok: false, detail: "" };
}

export async function runRuntimeTest(
  projectId: string,
  profileId: string,
  kind = "demo_smoke",
): Promise<RuntimeTestResult> {
  const r = (await jsonOk(
    await sidecarFetch(
      `${runtimeBase(projectId)}/${encodeURIComponent(profileId)}/test`,
      {
        method: "POST",
        headers: UI_ORIGIN,
        body: JSON.stringify({ kind }),
      },
    ),
    "runtime test",
  )) as { result: Record<string, unknown> };
  return runtimeTestResultFrom(r.result ?? {});
}

/**
 * F101 S5 — turn a failed runtime into a Coding Team dev task. The backend
 * composes the task detail (profile commands + last session outcome + redacted
 * log tail), binding the named session or the profile's most recent one.
 */
export async function requestRuntimeRepair(
  projectId: string,
  profileId: string,
  sessionId?: string | null,
): Promise<{ taskId: string; title: string }> {
  const r = (await jsonOk(
    await sidecarFetch(
      `${runtimeBase(projectId)}/${encodeURIComponent(profileId)}/repair`,
      {
        method: "POST",
        headers: UI_ORIGIN,
        body: JSON.stringify(sessionId ? { session_id: sessionId } : {}),
      },
    ),
    "runtime repair",
  )) as { task: Record<string, unknown> };
  return {
    taskId: String(r.task?.task_id ?? ""),
    title: String(r.task?.title ?? ""),
  };
}

export async function listProjects(): Promise<
  CodingProjectSummary[]
> {
  const r = (await jsonOk(await sidecarFetch("/coding/projects"), "list projects")) as {
    projects: Array<Record<string, unknown>>;
  };
  return r.projects.map((p) => ({
    id: String(p.id),
    displayName: typeof p.display_name === "string" ? p.display_name : undefined,
    northStar: String(p.north_star ?? ""),
    status: String(p.status ?? "active"),
    listStatus: String(p.list_status ?? p.status ?? "active"),
    listStatusReason: String(p.list_status_reason ?? "lifecycle"),
  }));
}

export async function listGroundingCorpora(): Promise<GroundingCorpusSummary[]> {
  return (await listCorpora()).map((c) => ({
    name: c.name,
    fileCount: c.fileCount,
    readyCount: c.readyCount,
  }));
}

function groundingPayload(input?: GroundingPayload | null): Record<string, unknown> | null {
  if (!input) return null;
  return {
    mode: input.mode,
    corpus_id: input.corpusId ?? null,
    source_root: input.sourceRoot ?? null,
  };
}

export async function createProject(input: CreateCodingProjectInput): Promise<CodingProject> {
  const body: Record<string, unknown> = {
    project_id: input.projectId,
    north_star: input.northStar,
    definition_of_done: input.definitionOfDone ?? "",
    target: input.target,
    repo_path: input.repoPath ?? null,
    delivery_root: input.deliveryRoot ?? null,
    work_request: input.workRequest ?? "",
  };
  const grounding = groundingPayload(input.grounding);
  if (grounding) body.grounding = grounding;
  const r = (await jsonOk(
    await sidecarFetch("/coding/projects", {
      method: "POST",
      headers: UI_ORIGIN,
      body: JSON.stringify(body),
    }),
    "create project",
  )) as { project: Record<string, unknown> };
  return projectFrom(r.project);
}

// --- F135: import + inference + work_request client methods ----------------

export interface ImportLocalInput {
  projectId: string;
  folderPath: string;
  gitInit?: boolean;
  confirm?: boolean;
}

export async function importLocalProject(input: ImportLocalInput): Promise<CodingProject> {
  const r = (await jsonOk(
    await sidecarFetch("/coding/projects/import/local", {
      method: "POST",
      headers: UI_ORIGIN,
      body: JSON.stringify({
        project_id: input.projectId,
        folder_path: input.folderPath,
        git_init: input.gitInit ?? false,
        confirm: input.confirm ?? false,
      }),
    }),
    "import local project",
  )) as { project: Record<string, unknown> };
  return projectFrom(r.project);
}

export async function importGithubAuthStatus(): Promise<GithubAuthStatus> {
  const r = (await jsonOk(
    await sidecarFetch("/coding/projects/import/github/auth-status", {
      headers: UI_ORIGIN,
    }),
    "github auth status",
  )) as Record<string, unknown>;
  return { ghPresent: Boolean(r.gh_present), login: (r.login as string | null) ?? null };
}

export interface GithubCloneInput {
  projectId: string;
  repoUrl: string;
  ref?: string | null;
  destinationRoot?: string | null;
  shallow?: boolean;
}

function jobFrom(raw: Record<string, unknown>): ImportJob {
  return {
    jobId: String(raw.job_id ?? ""),
    status: String(raw.status ?? ""),
    message: (raw.message as string | null) ?? null,
    projectId: (raw.project_id as string | null) ?? null,
  };
}

export async function importGithubClone(input: GithubCloneInput): Promise<ImportJob> {
  const r = (await jsonOk(
    await sidecarFetch("/coding/projects/import/github/clone", {
      method: "POST",
      headers: UI_ORIGIN,
      body: JSON.stringify({
        project_id: input.projectId,
        repo_url: input.repoUrl,
        ref: input.ref ?? null,
        destination_root: input.destinationRoot ?? null,
        shallow: input.shallow ?? false,
      }),
    }),
    "clone github repo",
  )) as Record<string, unknown>;
  return jobFrom(r);
}

// F141 WS-C — list a GitHub repo's branches (via git ls-remote) so the import
// wizard can offer a branch dropdown. Never throws for a "can't list" outcome —
// returns { ok: false } so the caller falls back to the free-text branch field.
export interface GithubBranchesResult {
  ok: boolean;
  branches: string[];
  defaultBranch: string | null;
  error?: string;
}

export async function importGithubBranches(
  repoUrl: string,
): Promise<GithubBranchesResult> {
  try {
    const r = (await jsonOk(
      await sidecarFetch("/coding/projects/import/github/branches", {
        method: "POST",
        headers: { ...UI_ORIGIN, "content-type": "application/json" },
        body: JSON.stringify({ repo_url: repoUrl }),
      }),
      "github branches",
    )) as Record<string, unknown>;
    if (!r.ok) {
      return { ok: false, branches: [], defaultBranch: null, error: String(r.error ?? "") };
    }
    return {
      ok: true,
      branches: Array.isArray(r.branches) ? (r.branches as string[]) : [],
      defaultBranch: (r.default_branch as string) || null,
    };
  } catch (err) {
    return {
      ok: false,
      branches: [],
      defaultBranch: null,
      error: err instanceof Error ? err.message : String(err),
    };
  }
}

export async function importGithubCloneStatus(jobId: string): Promise<ImportJob> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/import/github/clone/${encodeURIComponent(jobId)}`,
      { headers: UI_ORIGIN },
    ),
    "clone status",
  )) as Record<string, unknown>;
  return jobFrom(r);
}

// F138 — refresh an imported project from remote.
export interface RefreshPreview {
  target: string;
  repoPathExists: boolean;
  snapshotRef: string | null;
  repoHead: string | null;
  repoDirty: boolean | null;
  repoDiffers: boolean | null;
  workspaceHasUnacceptedChanges: boolean;
  originPresent: boolean;
  defaultBranch: string | null;
  shallow: boolean;
  localAhead: number | null;
  remoteAhead: number | null;
}

function refreshPreviewFrom(raw: Record<string, unknown>): RefreshPreview {
  return {
    target: String(raw.target ?? ""),
    repoPathExists: Boolean(raw.repo_path_exists),
    snapshotRef: (raw.snapshot_ref as string | null) ?? null,
    repoHead: (raw.repo_head as string | null) ?? null,
    repoDirty: (raw.repo_dirty as boolean | null) ?? null,
    repoDiffers: (raw.repo_differs as boolean | null) ?? null,
    workspaceHasUnacceptedChanges: Boolean(raw.workspace_has_unaccepted_changes),
    originPresent: Boolean(raw.origin_present),
    defaultBranch: (raw.default_branch as string | null) ?? null,
    shallow: Boolean(raw.shallow),
    localAhead: (raw.local_ahead as number | null) ?? null,
    remoteAhead: (raw.remote_ahead as number | null) ?? null,
  };
}

export async function getRefreshPreview(projectId: string): Promise<RefreshPreview> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(projectId)}/refresh-preview`,
      { headers: UI_ORIGIN },
    ),
    "refresh preview",
  )) as { preview?: Record<string, unknown> };
  return refreshPreviewFrom(r.preview ?? {});
}

export async function refreshProject(
  projectId: string,
  input: { pull?: boolean; discardWorkspace?: boolean } = {},
): Promise<ImportJob> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(projectId)}/refresh`, {
      method: "POST",
      headers: UI_ORIGIN,
      body: JSON.stringify({
        pull: input.pull ?? true,
        discard_workspace: input.discardWorkspace ?? false,
      }),
    }),
    "refresh project",
  )) as Record<string, unknown>;
  return jobFrom(r);
}

export async function refreshProjectStatus(
  projectId: string,
  jobId: string,
): Promise<ImportJob> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(projectId)}/refresh/${encodeURIComponent(jobId)}`,
      { headers: UI_ORIGIN },
    ),
    "refresh status",
  )) as Record<string, unknown>;
  return jobFrom(r);
}

export async function startOrientationScan(
  projectId: string,
  routeId?: string | null,
): Promise<ImportJob> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(projectId)}/orientation-scan`,
      {
        method: "POST",
        headers: UI_ORIGIN,
        body: JSON.stringify({ route_id: routeId ?? null }),
      },
    ),
    "start orientation scan",
  )) as Record<string, unknown>;
  return jobFrom(r);
}

export async function orientationScanStatus(
  projectId: string,
  jobId: string,
): Promise<ImportJob> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(projectId)}/orientation-scan/${encodeURIComponent(jobId)}`,
      { headers: UI_ORIGIN },
    ),
    "orientation scan status",
  )) as Record<string, unknown>;
  return jobFrom(r);
}

function proposalFrom(raw: Record<string, unknown>): NorthStarProposal {
  const list = (v: unknown): string[] =>
    Array.isArray(v) ? v.map((x) => String(x)) : [];
  return {
    northStar: String(raw.north_star ?? ""),
    definitionOfDone: String(raw.definition_of_done ?? ""),
    summary: String(raw.summary ?? ""),
    detectedStack: list(raw.detected_stack),
    suggestedFirstTasks: list(raw.suggested_first_tasks),
    sourceRefs: list(raw.source_refs),
    model: String(raw.model ?? ""),
    lowSignal: Boolean(raw.low_signal),
    accepted: Boolean(raw.accepted),
  };
}

/** Latest proposal, or null when none exists (404). */
export async function getNorthStarProposal(
  projectId: string,
): Promise<NorthStarProposal | null> {
  const resp = await sidecarFetch(
    `/coding/projects/${encodeURIComponent(projectId)}/north-star-proposal`,
    { headers: UI_ORIGIN },
  );
  if (resp.status === 404) return null;
  const r = (await jsonOk(resp, "get north star proposal")) as {
    proposal: Record<string, unknown>;
  };
  return proposalFrom(r.proposal);
}

export async function acceptNorthStarProposal(projectId: string): Promise<CodingProject> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(projectId)}/north-star-proposal/accept`,
      { method: "POST", headers: UI_ORIGIN },
    ),
    "accept north star proposal",
  )) as { project: Record<string, unknown> };
  return projectFrom(r.project);
}

export async function setWorkRequest(
  projectId: string,
  workRequest: string,
): Promise<CodingProject> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(projectId)}/work-request`,
      {
        method: "PUT",
        headers: UI_ORIGIN,
        body: JSON.stringify({ work_request: workRequest }),
      },
    ),
    "set work request",
  )) as { project: Record<string, unknown> };
  return projectFrom(r.project);
}

// --- F137: Current Focus goals ---------------------------------------------

export type FocusStatus = "active" | "completed" | "archived";

export interface Focus {
  id: string;
  title: string;
  body: string;
  status: FocusStatus;
  order: number;
  origin: string;
  createdAt: string;
  completedAt: string;
  acceptedAt: string;
  archivedAt: string;
  completionSummary: string;
}

export function adaptFocus(raw: Record<string, unknown>): Focus {
  return {
    id: String(raw.id ?? ""),
    title: String(raw.title ?? ""),
    body: String(raw.body ?? ""),
    status: (String(raw.status ?? "active") as FocusStatus),
    order: Number(raw.order ?? 0),
    origin: String(raw.origin ?? "user"),
    createdAt: String(raw.created_at ?? ""),
    completedAt: String(raw.completed_at ?? ""),
    acceptedAt: String(raw.accepted_at ?? ""),
    archivedAt: String(raw.archived_at ?? ""),
    completionSummary: String(raw.completion_summary ?? ""),
  };
}

export async function listFocuses(
  projectId: string,
  status: FocusStatus | "all" = "active",
): Promise<Focus[]> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(projectId)}/focus?status=${status}`,
      { headers: UI_ORIGIN },
    ),
    "list focuses",
  )) as { focuses: Record<string, unknown>[] };
  return (r.focuses ?? []).map(adaptFocus);
}

export async function addFocus(
  projectId: string,
  title: string,
  body = "",
): Promise<Focus> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(projectId)}/focus`,
      { method: "POST", headers: UI_ORIGIN, body: JSON.stringify({ title, body }) },
    ),
    "add focus",
  )) as { focus: Record<string, unknown> };
  return adaptFocus(r.focus);
}

export async function reorderFocuses(
  projectId: string,
  orderedIds: string[],
): Promise<Focus[]> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(projectId)}/focus/reorder`,
      { method: "PUT", headers: UI_ORIGIN, body: JSON.stringify({ ordered_ids: orderedIds }) },
    ),
    "reorder focuses",
  )) as { focuses: Record<string, unknown>[] };
  return (r.focuses ?? []).map(adaptFocus);
}

export async function updateFocus(
  projectId: string,
  focusId: string,
  patch: { title?: string; body?: string; status?: FocusStatus },
): Promise<Focus> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(projectId)}/focus/${encodeURIComponent(focusId)}`,
      { method: "PUT", headers: UI_ORIGIN, body: JSON.stringify(patch) },
    ),
    "update focus",
  )) as { focus: Record<string, unknown> };
  return adaptFocus(r.focus);
}

export async function acceptFocus(projectId: string, focusId: string): Promise<Focus> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(projectId)}/focus/${encodeURIComponent(focusId)}/accept`,
      { method: "POST", headers: UI_ORIGIN },
    ),
    "accept focus",
  )) as { focus: Record<string, unknown> };
  return adaptFocus(r.focus);
}

export async function getProject(id: string): Promise<CodingProject> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}`),
    "get project",
  )) as { project: Record<string, unknown> };
  return projectFrom(r.project);
}

export async function deleteProject(id: string): Promise<void> {
  await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: UI_ORIGIN,
    }),
    "delete project",
  );
}

export async function getGroundingCapabilities(id: string): Promise<ProjectGroundingCapabilities> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/grounding/capabilities`),
    "get grounding capabilities",
  )) as { capabilities: Record<string, unknown> };
  const c = r.capabilities;
  return {
    available: Boolean(c.available),
    version: (c.version as string | null) ?? null,
    source: String(c.source ?? ""),
    supportsCorpusIds: Boolean(c.supports_corpus_ids),
    supportsFileIngest: Boolean(c.supports_file_ingest),
    supportsRecordIngest: Boolean(c.supports_record_ingest),
    supportsMetadataFilters: Boolean(c.supports_metadata_filters),
    supportsProvenanceMetadata: Boolean(c.supports_provenance_metadata),
    supportsIncrementalRefresh: Boolean(c.supports_incremental_refresh),
    supportsSupersession: Boolean(c.supports_supersession),
    supportsExportImport: Boolean(c.supports_export_import),
    localOnlyEmbedding: Boolean(c.local_only_embedding),
    notes: (c.notes as string[]) ?? [],
  };
}

export async function getCorpusBinding(id: string): Promise<ProjectCorpusBinding> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/grounding/corpus-binding`),
    "get corpus binding",
  )) as { binding: Record<string, unknown> };
  return bindingFrom(r.binding) as ProjectCorpusBinding;
}

export async function getPmWorkingMemoryStatus(id: string): Promise<PmWorkingMemoryStatus> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/pm-working-memory`),
    "get PM working memory",
  )) as { pm_working_memory: Record<string, unknown> };
  const raw = r.pm_working_memory;
  return {
    projectId: String(raw.project_id ?? ""),
    status: String(raw.status ?? "unavailable"),
    memoryRef: (raw.memory_ref as string | null) ?? null,
    corpusId: (raw.corpus_id as string | null) ?? null,
    aiarMirrorStatus: String(raw.aiar_mirror_status ?? "unknown"),
    aiarRetrievalStatus: String(raw.aiar_retrieval_status ?? "unknown"),
    lastGeneratedAt: (raw.last_generated_at as string | null) ?? null,
    lastMirroredAt: (raw.last_mirrored_at as string | null) ?? null,
    warnings: Array.isArray(raw.warnings) ? (raw.warnings as string[]) : [],
  };
}

export async function getGovernance(id: string): Promise<GovernanceSummary> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/governance`),
    "get governance",
  )) as { governance: Record<string, unknown> };
  return governanceSummaryFrom(r.governance);
}

// F100-01: the plain-language status projection folded into the same governance
// route (response now carries `{governance, status}`). Returned separately so
// the existing `getGovernance` (summary-only) consumers stay untouched.
export async function getGovernanceStatus(id: string): Promise<GovernanceStatus> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/governance`),
    "get governance status",
  )) as { status?: Record<string, unknown> | null };
  return governanceStatusFrom(r.status);
}

// F100-01: the governance route returns `{governance, status}` in one body —
// fetch both in a single request (used by the polling loop so it doesn't hit
// the same endpoint twice per tick).
export async function getGovernanceFull(
  id: string,
): Promise<{ summary: GovernanceSummary; status: GovernanceStatus }> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/governance`),
    "get governance",
  )) as { governance: Record<string, unknown>; status?: Record<string, unknown> | null };
  return {
    summary: governanceSummaryFrom(r.governance),
    status: governanceStatusFrom(r.status),
  };
}

export async function putGovernanceSettings(
  id: string,
  patch: Partial<{
    mode: GovernanceMode;
    phase: string;
    humanCodeApproval: HumanCodeApproval;
    blockOnProblems: boolean;
    monitor: Record<string, unknown>;
  }>,
): Promise<GovernanceSummary> {
  const body: Record<string, unknown> = {};
  if (patch.mode !== undefined) body.mode = patch.mode;
  if (patch.phase !== undefined) body.phase = patch.phase;
  if (patch.humanCodeApproval !== undefined) {
    body.human_code_approval = patch.humanCodeApproval;
  }
  if (patch.blockOnProblems !== undefined) {
    body.block_on_problems = patch.blockOnProblems;
  }
  if (patch.monitor !== undefined) body.monitor = patch.monitor;
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/governance/settings`, {
      method: "PUT",
      headers: UI_ORIGIN,
      body: JSON.stringify(body),
    }),
    "put governance settings",
  )) as { governance: Record<string, unknown> };
  return governanceSummaryFrom(r.governance);
}

// F117 attention signals.
export async function getAttention(
  id: string,
  opts: { state?: string; kind?: string } = {},
): Promise<AttentionList> {
  const params = new URLSearchParams();
  if (opts.state) params.set("state", opts.state);
  if (opts.kind) params.set("kind", opts.kind);
  const qs = params.toString();
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(id)}/attention${qs ? `?${qs}` : ""}`,
    ),
    "get attention",
  )) as { signals: Array<Record<string, unknown>>; blocks_stage: boolean };
  return {
    signals: (r.signals ?? []).map(attentionSignalFrom),
    blocksStage: Boolean(r.blocks_stage),
  };
}

/**
 * Error thrown by {@link resolveSignal} that preserves the HTTP status and the
 * backend's structured `detail` so the caller can (a) show the real reason
 * instead of a bare `(422)` and (b) detect an already-resolved signal (409) and
 * self-heal by refreshing rather than surfacing an error. (F123)
 */
export class ResolveSignalError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(
      detail
        ? `resolve attention signal failed (${status}): ${detail}`
        : `resolve attention signal failed (${status})`,
    );
    this.name = "ResolveSignalError";
    this.status = status;
    this.detail = detail;
  }
}

export async function resolveSignal(
  id: string,
  signalId: string,
  body: { action: AttentionAction; suggestionId?: string; correctionText?: string },
): Promise<{ signal: AttentionSignal; createdTaskId: string | null }> {
  const res = await sidecarFetch(
    `/coding/projects/${encodeURIComponent(id)}/attention/${encodeURIComponent(
      signalId,
    )}/resolve`,
    {
      method: "POST",
      headers: { ...UI_ORIGIN, "content-type": "application/json" },
      body: JSON.stringify({
        action: body.action,
        suggestion_id: body.suggestionId ?? null,
        correction_text: body.correctionText ?? null,
      }),
    },
  );
  if (!res.ok) {
    let detail = "";
    try {
      const b = (await res.json()) as { detail?: unknown };
      detail =
        typeof b?.detail === "string"
          ? b.detail
          : b?.detail != null
            ? JSON.stringify(b.detail)
            : "";
    } catch {
      // non-JSON error body — fall back to the bare status
    }
    throw new ResolveSignalError(res.status, detail);
  }
  const r = (await res.json()) as {
    signal: Record<string, unknown>;
    created_task_id: string | null;
  };
  return {
    signal: attentionSignalFrom(r.signal),
    createdTaskId: r.created_task_id ?? null,
  };
}

export async function approveGovernanceApproval(
  id: string,
  approvalId: string,
  feedback = "",
): Promise<GovernanceSummary> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(id)}/governance/approvals/${encodeURIComponent(
        approvalId,
      )}/approve`,
      {
        method: "POST",
        headers: UI_ORIGIN,
        body: JSON.stringify({ feedback, actor: "user" }),
      },
    ),
    "approve governance approval",
  )) as { governance: Record<string, unknown> };
  return governanceSummaryFrom(r.governance);
}

export async function rejectGovernanceApproval(
  id: string,
  approvalId: string,
  feedback = "",
): Promise<GovernanceSummary> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(id)}/governance/approvals/${encodeURIComponent(
        approvalId,
      )}/reject`,
      {
        method: "POST",
        headers: UI_ORIGIN,
        body: JSON.stringify({ feedback, actor: "user" }),
      },
    ),
    "reject governance approval",
  )) as { governance: Record<string, unknown> };
  return governanceSummaryFrom(r.governance);
}

export async function getGovernanceArtifact(
  id: string,
  artifactId: string,
): Promise<GovernanceArtifact> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(id)}/governance/artifacts/${encodeURIComponent(
        artifactId,
      )}`,
    ),
    "get governance artifact",
  )) as { artifact: Record<string, unknown> };
  return governanceArtifactFrom(r.artifact);
}

// F100-02: human override — force-accept a governance artifact (the viewed one)
// as "good enough", advancing the phase. Tauri-origin guarded; body {confirm:true}.
// 409 = stale/superseded (a newer version exists), 400 = governance off.
export async function acceptGovernanceArtifact(
  id: string,
  artifactId: string,
): Promise<GovernanceSummary> {
  const res = await sidecarFetch(
    `/coding/projects/${encodeURIComponent(id)}/governance/artifacts/${encodeURIComponent(
      artifactId,
    )}/accept`,
    {
      method: "POST",
      headers: UI_ORIGIN,
      body: JSON.stringify({ confirm: true }),
    },
  );
  if (!res.ok) {
    let detail: unknown = null;
    try {
      detail = ((await res.json()) as { detail?: unknown })?.detail;
    } catch {
      /* non-JSON body */
    }
    const obj = detail && typeof detail === "object" ? (detail as Record<string, unknown>) : null;
    const detailMsg =
      obj && typeof obj.message === "string"
        ? obj.message
        : typeof detail === "string"
          ? detail
          : null;
    const message =
      res.status === 409
        ? detailMsg ??
          "This brainstorm was superseded by a newer version — refresh and try again."
        : res.status === 400
          ? detailMsg ?? "Governance is off for this project."
          : detailMsg ?? `accept governance artifact failed (${res.status})`;
    const err = new Error(message) as Error & { code?: string; status?: number };
    err.status = res.status;
    if (obj && typeof obj.code === "string") err.code = obj.code;
    throw err;
  }
  const r = (await res.json()) as { governance: Record<string, unknown> };
  return governanceSummaryFrom(r.governance);
}

export async function exportGovernanceArtifactTask(
  id: string,
  artifactId: string,
  targetPath: string,
  title?: string,
): Promise<CodingTask> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(id)}/governance/artifacts/${encodeURIComponent(
        artifactId,
      )}/export-task`,
      {
        method: "POST",
        headers: UI_ORIGIN,
        body: JSON.stringify({ target_path: targetPath, title: title ?? null }),
      },
    ),
    "export governance artifact task",
  )) as { task: Record<string, unknown> };
  return taskFrom(r.task);
}

export async function putCorpusBinding(
  id: string,
  binding: GroundingPayload,
): Promise<ProjectCorpusBinding> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/grounding/corpus-binding`, {
      method: "PUT",
      headers: UI_ORIGIN,
      body: JSON.stringify(groundingPayload(binding)),
    }),
    "put corpus binding",
  )) as { binding?: Record<string, unknown> };
  return bindingFrom(r.binding) as ProjectCorpusBinding;
}

export async function buildCorpusFromProject(
  id: string,
  corpusId?: string,
): Promise<ProjectCorpusBinding> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(id)}/grounding/build-from-project`,
      {
        method: "POST",
        headers: UI_ORIGIN,
        body: JSON.stringify({ corpus_id: corpusId ?? null }),
      },
    ),
    "build corpus from project",
  )) as { binding?: Record<string, unknown> };
  return bindingFrom(r.binding) as ProjectCorpusBinding;
}

export async function refreshProjectCorpus(id: string): Promise<void> {
  await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(id)}/grounding/memory/rebuild`,
      {
        method: "POST",
        headers: UI_ORIGIN,
        body: JSON.stringify({ mode: "from_repo" }),
      },
    ),
    "refresh project corpus",
  );
}

export async function retrieveProjectCorpus(
  id: string,
  query: string,
  k = 6,
): Promise<GroundingRetrieveResult> {
  const params = new URLSearchParams({ q: query, k: String(k) });
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(id)}/grounding/retrieve?${params.toString()}`,
      { headers: UI_ORIGIN },
    ),
    "retrieve project corpus",
  )) as { hits: Array<Record<string, unknown>>; status?: string };
  const hits: GroundingHit[] = (r.hits ?? []).map((h) => ({
    content: String(h.content ?? ""),
    corpusId: String(h.corpus_id ?? ""),
    chunkId: String(h.chunk_id ?? ""),
    score: typeof h.score === "number" ? h.score : null,
  }));
  // Backend status: ok | no_corpus | unavailable | empty_query. "ok" with no
  // hits means the corpus is served but nothing matched -> distinct "empty".
  let status: GroundingRetrieveStatus;
  switch (r.status) {
    case "no_corpus":
      status = "no_corpus";
      break;
    case "unavailable":
      status = "unavailable";
      break;
    case "empty_query":
      status = "empty";
      break;
    case "ok":
      status = hits.length > 0 ? "ok" : "empty";
      break;
    default:
      status = hits.length > 0 ? "ok" : "empty";
  }
  return { status, hits };
}

export async function getBootstrapJob(
  id: string,
  jobId: string,
): Promise<GroundingBootstrapJob | null> {
  const res = await sidecarFetch(
    `/coding/projects/${encodeURIComponent(id)}/grounding/bootstrap/${encodeURIComponent(jobId)}`,
  );
  if (res.status === 404) return null;
  const r = (await jsonOk(res, "get bootstrap job")) as { job: Record<string, unknown> };
  const j = r.job;
  return {
    jobId: String(j.job_id ?? ""),
    corpusId: String(j.corpus_id ?? ""),
    status: String(j.status ?? "failed"),
    adapterSource: String(j.adapter_source ?? "local"),
    documentsIngested: Number(j.documents_ingested ?? 0),
    chunksAdded: Number(j.chunks_added ?? 0),
    errors: Array.isArray(j.errors) ? (j.errors as string[]) : [],
    endedAt: (j.ended_at as string | null) ?? null,
  };
}

export async function getBacklog(id: string): Promise<CodingTask[]> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/backlog`),
    "get backlog",
  )) as { tasks: Array<Record<string, unknown>> };
  return r.tasks.map(taskFrom);
}

export async function getDecisions(id: string): Promise<CodingDecision[]> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/decisions`),
    "get decisions",
  )) as { decisions: Array<Record<string, unknown>> };
  return r.decisions.map((d) => ({
    decisionId: String(d.decision_id ?? ""),
    title: String(d.title ?? ""),
    choice: String(d.choice ?? ""),
    rationale: String(d.rationale ?? ""),
    relatedTaskIds: Array.isArray(d.related_task_ids)
      ? (d.related_task_ids as unknown[]).map((x) => String(x))
      : [],
  }));
}

/** A human-readable Team Log entry (the narrative of what the team did). */
export interface TeamLogEntry {
  at: string;
  role: string;
  /** Member id (e.g. "m-2"), or "" for PM/system entries. */
  member: string;
  kind: string;
  message: string;
}

export async function getTeamLog(id: string): Promise<TeamLogEntry[]> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/team-log`),
    "team log",
  )) as { entries: Array<Record<string, unknown>> };
  return (r.entries ?? []).map((e) => ({
    at: String(e.at ?? ""),
    role: String(e.role ?? ""),
    member: String(e.member ?? ""),
    kind: String(e.kind ?? ""),
    message: String(e.message ?? ""),
  }));
}

export async function getArtifacts(id: string): Promise<CodingArtifact[]> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/artifacts`),
    "get artifacts",
  )) as { artifacts: Array<Record<string, unknown>> };
  return r.artifacts.map((a) => ({
    path: String(a.path ?? ""),
    status: String(a.status ?? ""),
    summary: String(a.summary ?? ""),
    onMaster: Boolean(a.on_master),
  }));
}

function fileFrom(raw: Record<string, unknown>, fallbackPath: string): CodingFile {
  const encoding = raw.encoding === "binary" ? "binary" : "utf-8";
  return {
    path: String(raw.path ?? fallbackPath),
    content: raw.content == null ? null : String(raw.content),
    truncated: Boolean(raw.truncated),
    encoding,
    bytes: Number(raw.bytes ?? 0),
    onMaster: Boolean(raw.on_master),
    contentSha256: raw.content_sha256 == null ? null : String(raw.content_sha256),
  };
}

export async function getFile(id: string, path: string): Promise<CodingFile> {
  const res = await sidecarFetch(
    `/coding/projects/${encodeURIComponent(id)}/files?path=${encodeURIComponent(path)}`,
    { headers: UI_ORIGIN },
  );
  if (res.status === 404) {
    const body = (await res.json().catch(() => ({}))) as {
      detail?: { reason?: string };
    };
    if (body.detail?.reason === "not_on_master") {
      return {
        path,
        content: null,
        truncated: false,
        encoding: "utf-8",
        bytes: 0,
        onMaster: false,
        contentSha256: null,
      };
    }
  }
  const raw = (await jsonOk(res, "get file")) as Record<string, unknown>;
  return fileFrom(raw, path);
}

/** F105: result of a successful in-app file save (PUT /files). */
export interface CodingFileUpdate {
  path: string;
  contentSha256: string;
  bytes: number;
  head: string;
  onMaster: boolean;
}

/**
 * F105: a typed error from `updateFile` so the UI can branch on the backend's
 * 409 reasons. `reason` is `stale_file` (the file changed since GET —
 * `currentSha256` carries the new committed hash for a reload) or `run_active`
 * (a Coding run holds the worktree). Other failures throw a plain `Error`.
 */
export class CodingFileUpdateError extends Error {
  reason: "stale_file" | "run_active";
  currentSha256: string | null;
  constructor(
    reason: "stale_file" | "run_active",
    message: string,
    currentSha256: string | null = null,
  ) {
    super(message);
    this.name = "CodingFileUpdateError";
    this.reason = reason;
    this.currentSha256 = currentSha256;
  }
}

export async function updateFile(
  id: string,
  path: string,
  content: string,
  expectedSha256: string,
): Promise<CodingFileUpdate> {
  const res = await sidecarFetch(
    `/coding/projects/${encodeURIComponent(id)}/files?path=${encodeURIComponent(path)}`,
    {
      method: "PUT",
      headers: { ...UI_ORIGIN, "content-type": "application/json" },
      body: JSON.stringify({ content, expected_sha256: expectedSha256 }),
    },
  );
  if (res.status === 409) {
    const body = (await res.json().catch(() => ({}))) as {
      detail?: { reason?: string; content_sha256?: string | null };
    };
    const reason = body.detail?.reason;
    if (reason === "stale_file") {
      throw new CodingFileUpdateError(
        "stale_file",
        "This file changed since you opened it. Reload to get the latest, then re-apply your edit.",
        (body.detail?.content_sha256 as string | null) ?? null,
      );
    }
    if (reason === "run_active") {
      throw new CodingFileUpdateError(
        "run_active",
        "A Coding run is active — saving is disabled until it finishes.",
      );
    }
  }
  const raw = (await jsonOk(res, "update file")) as Record<string, unknown>;
  return {
    path: String(raw.path ?? path),
    contentSha256: String(raw.content_sha256 ?? ""),
    bytes: Number(raw.bytes ?? 0),
    head: String(raw.head ?? ""),
    onMaster: Boolean(raw.on_master),
  };
}

export async function getToolEvents(id: string): Promise<CodingToolEvent[]> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/tool-events`),
    "get tool events",
  )) as { tool_events: Array<Record<string, unknown>> };
  return r.tool_events.map((e) => {
    const intent = (e.intent ?? {}) as Record<string, unknown>;
    const result = (e.result ?? {}) as Record<string, unknown>;
    return {
      eventId: String(e.event_id ?? ""),
      taskId: String(e.task_id ?? ""),
      memberId: String(e.member_id ?? ""),
      role: String(e.role ?? ""),
      tool: String(e.tool ?? ""),
      status: String(e.status ?? ""),
      path: String(result.path ?? intent.path ?? ""),
      error: e.error == null ? null : String(e.error),
    };
  });
}

export async function getGuardrail(id: string): Promise<boolean> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/guardrail`),
    "get guardrail",
  )) as { enabled: boolean };
  return Boolean(r.enabled);
}

export async function putGuardrail(id: string, enabled: boolean): Promise<boolean> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/guardrail`, {
      method: "PUT",
      headers: UI_ORIGIN,
      body: JSON.stringify({ enabled }),
    }),
    "put guardrail",
  )) as { enabled: boolean };
  return Boolean(r.enabled);
}

export async function getAutonomy(id: string): Promise<AutonomyPolicy> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/autonomy`),
    "get autonomy",
  )) as { policy: Record<string, unknown> };
  return policyFrom(r.policy);
}

export async function putAutonomy(
  id: string,
  policy: Partial<AutonomyPolicy>,
): Promise<AutonomyPolicy> {
  const body: Record<string, unknown> = {};
  if (policy.maxIterations != null) body.max_iterations = policy.maxIterations;
  if (policy.maxModelCalls !== undefined) body.max_model_calls = policy.maxModelCalls;
  if (policy.checkpointCadence) body.checkpoint_cadence = policy.checkpointCadence;
  if (policy.checkpointN != null) body.checkpoint_n = policy.checkpointN;
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/autonomy`, {
      method: "PUT",
      headers: UI_ORIGIN,
      body: JSON.stringify(body),
    }),
    "put autonomy",
  )) as { policy: Record<string, unknown> };
  return policyFrom(r.policy);
}

export async function putNorthStar(
  id: string,
  northStar: string,
  definitionOfDone?: string,
): Promise<CodingProject> {
  const body: Record<string, unknown> = { north_star: northStar };
  if (definitionOfDone !== undefined) body.definition_of_done = definitionOfDone;
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/north-star`, {
      method: "PUT",
      headers: UI_ORIGIN,
      body: JSON.stringify(body),
    }),
    "put north star",
  )) as { project: Record<string, unknown> };
  return projectFrom(r.project);
}

export async function interject(
  id: string,
  message: string,
  // F100-02: optionally tag the interjection with the governance artifact it
  // comments on (e.g. the viewed brainstorm), for the audit/Team Log.
  artifactId?: string,
): Promise<CodingInterjection> {
  const body: Record<string, unknown> = { message };
  if (artifactId) body.artifact_id = artifactId;
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/interject`, {
      method: "POST",
      headers: UI_ORIGIN,
      body: JSON.stringify(body),
    }),
    "interject",
  )) as { interjection: Record<string, unknown> };
  return interjectionFrom(r.interjection);
}

// F141 WS-J — synchronous PM chat ("pull the PM into your office").
export interface PmChatTurn {
  role: string; // "user" | "pm"
  message: string;
  at: string;
}

export interface PmAskResult {
  reply: { role: string; kind: string; message: string; at: string };
  threadId: string;
  answered: boolean;
  error?: string;
}

export async function pmAsk(
  id: string,
  message: string,
  threadId = "main",
): Promise<PmAskResult> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/pm-ask`, {
      method: "POST",
      headers: { ...UI_ORIGIN, "content-type": "application/json" },
      body: JSON.stringify({ message, thread_id: threadId }),
    }),
    "ask the PM",
  )) as Record<string, unknown>;
  const reply = (r.reply ?? {}) as Record<string, unknown>;
  return {
    reply: {
      role: String(reply.role ?? "pm"),
      kind: String(reply.kind ?? "chat"),
      message: String(reply.message ?? ""),
      at: String(reply.at ?? ""),
    },
    threadId: String(r.thread_id ?? threadId),
    answered: Boolean(r.answered),
    error: r.error ? String(r.error) : undefined,
  };
}

export async function getPmChat(
  id: string,
  threadId = "main",
): Promise<PmChatTurn[]> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(id)}/pm-chat?thread_id=${encodeURIComponent(threadId)}`,
      { headers: UI_ORIGIN },
    ),
    "pm chat",
  )) as { thread?: Array<Record<string, unknown>> };
  return (r.thread ?? []).map((t) => ({
    role: String(t.role ?? "pm"),
    message: String(t.message ?? ""),
    at: String(t.at ?? ""),
  }));
}

export interface MergeBlocker {
  code: string;
  detail: string;
}
export interface MergeGate {
  allowed: boolean;
  allowOverride: boolean;
  blockers: MergeBlocker[];
}
export interface FileDiff {
  path: string;
  oldPath: string | null;
  changeType: string;
  addedLines: number;
  removedLines: number;
}
/** F104 S5: spec-conformance signal — did an implementer turn see the bound corpus's facts? */
export interface GroundingSignal {
  corpusBound: boolean;
  implementerGrounded: boolean;
  policy: string;
}
export interface WorktreePreview {
  diff: string;
  conflicts: string[];
  fileDiffs: FileDiff[];
  gate: MergeGate;
  grounding: GroundingSignal | null;
}

export async function getWorktreePreview(id: string): Promise<WorktreePreview> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/worktree`),
    "worktree preview",
  )) as Record<string, unknown>;
  const gate = (r.gate ?? {}) as Record<string, unknown>;
  return {
    diff: String(r.diff ?? ""),
    conflicts: (r.conflicts as string[]) ?? [],
    fileDiffs: ((r.file_diffs as Array<Record<string, unknown>>) ?? []).map((f) => ({
      path: String(f.path ?? ""),
      oldPath: f.oldPath == null ? null : String(f.oldPath),
      changeType: String(f.changeType ?? "modified"),
      addedLines: Number(f.addedLines ?? 0),
      removedLines: Number(f.removedLines ?? 0),
    })),
    gate: {
      allowed: Boolean(gate.allowed),
      allowOverride: Boolean(gate.allowOverride),
      blockers: ((gate.blockers as Array<Record<string, unknown>>) ?? []).map((b) => ({
        code: String(b.code ?? ""),
        detail: String(b.detail ?? ""),
      })),
    },
    grounding: r.grounding
      ? {
          corpusBound: Boolean((r.grounding as Record<string, unknown>).corpus_bound),
          implementerGrounded: Boolean(
            (r.grounding as Record<string, unknown>).implementer_grounded,
          ),
          policy: String((r.grounding as Record<string, unknown>).policy ?? "warn"),
        }
      : null,
  };
}

export interface Delivery {
  deliveredTo: string;
  openUrl: string;
  runHint: string;
}

export async function acceptWorktree(
  id: string,
  opts: { allowConflicts?: boolean; override?: boolean } = {},
): Promise<Delivery> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/worktree/accept`, {
      method: "POST",
      headers: UI_ORIGIN,
      body: JSON.stringify({
        confirm: true,
        allow_conflicts: Boolean(opts.allowConflicts),
        override: Boolean(opts.override),
      }),
    }),
    "accept worktree",
  )) as Record<string, unknown>;
  return {
    deliveredTo: String(r.delivered_to ?? ""),
    openUrl: String(r.open_url ?? ""),
    runHint: String(r.run_hint ?? ""),
  };
}

export interface TestCommand {
  argv: string[];
  cwd: string;
  timeoutSeconds: number;
  label: string;
}
export interface TestRun {
  testRunId: string;
  taskId: string;
  passed: boolean;
  commandIds: string[];
  sandbox: string;
  at: string;
}

export async function getTestCommands(id: string): Promise<Record<string, TestCommand>> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/test-commands`),
    "get test commands",
  )) as { commands: Record<string, Record<string, unknown>> };
  const out: Record<string, TestCommand> = {};
  for (const [k, v] of Object.entries(r.commands ?? {})) {
    out[k] = {
      argv: (v.argv as string[]) ?? [],
      cwd: String(v.cwd ?? "."),
      timeoutSeconds: Number(v.timeout_seconds ?? 120),
      label: String(v.label ?? k),
    };
  }
  return out;
}

export async function putTestCommands(
  id: string,
  commands: Record<string, { argv: string[]; cwd?: string; timeoutSeconds?: number }>,
): Promise<void> {
  const payload: Record<string, Record<string, unknown>> = {};
  for (const [k, v] of Object.entries(commands)) {
    payload[k] = {
      argv: v.argv,
      cwd: v.cwd ?? ".",
      timeout_seconds: v.timeoutSeconds ?? 120,
    };
  }
  await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/test-commands`, {
      method: "PUT",
      headers: UI_ORIGIN,
      body: JSON.stringify({ commands: payload }),
    }),
    "put test commands",
  );
}

export async function getTestRuns(id: string): Promise<TestRun[]> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/test-runs`),
    "get test runs",
  )) as { runs: Array<Record<string, unknown>> };
  return (r.runs ?? []).map((x) => ({
    testRunId: String(x.test_run_id ?? ""),
    taskId: String(x.task_id ?? ""),
    passed: Boolean(x.passed),
    commandIds: (x.command_ids as string[]) ?? [],
    sandbox: String(x.sandbox ?? ""),
    at: String(x.at ?? ""),
  }));
}

export interface CodingTurn {
  turnId: string;
  role: string;
  memberId: string;
  taskId: string;
  prompt: string;
  response: string;
  outcome: string;
  reason: string;
  parseOk: boolean;
  durationMs: number;
  at: string;
  modelAssignment?: {
    assignment_id?: string;
    route_id?: string;
    difficulty_tier?: string;
    task_type?: string;
    rationale?: string;
    source?: string;
    escalation_count?: number;
  } | null;
  /**
   * F143: per-turn token usage, present only when the provider reported it
   * (`measured` true). Absent on unreported providers (e.g. cursor_cli) and on
   * pre-feature turns — the UI shows those as "unreported", never as 0.
   */
  usage?: CodingTurnUsage | null;
}

export interface CodingTurnUsage {
  measured: boolean;
  inputTokens: number | null;
  outputTokens: number | null;
  cacheReadInputTokens: number | null;
  cacheWriteInputTokens: number | null;
}

function adaptTurnUsage(raw: unknown): CodingTurnUsage | null {
  if (typeof raw !== "object" || raw === null) return null;
  const u = raw as Record<string, unknown>;
  const num = (v: unknown): number | null =>
    typeof v === "number" && Number.isFinite(v) ? v : null;
  return {
    measured: Boolean(u.measured),
    inputTokens: num(u.input_tokens),
    outputTokens: num(u.output_tokens),
    cacheReadInputTokens: num(u.cache_read_input_tokens),
    cacheWriteInputTokens: num(u.cache_write_input_tokens),
  };
}

export async function getTurns(id: string, limit = 200): Promise<CodingTurn[]> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/turns?limit=${limit}`),
    "get turns",
  )) as { turns: Array<Record<string, unknown>> };
  return (r.turns ?? []).map((t) => ({
    turnId: String(t.turn_id ?? ""),
    role: String(t.role ?? ""),
    memberId: String(t.member_id ?? ""),
    taskId: String(t.task_id ?? ""),
    prompt: String(t.prompt ?? ""),
    response: String(t.response ?? ""),
    outcome: String(t.outcome ?? ""),
    reason: String(t.reason ?? ""),
    parseOk: Boolean(t.parse_ok),
    durationMs: Number(t.duration_ms ?? 0),
    at: String(t.at ?? ""),
    modelAssignment: (
      typeof t.model_assignment === "object" && t.model_assignment !== null
        ? t.model_assignment
        : null
    ) as CodingTurn["modelAssignment"],
    usage: adaptTurnUsage(t.usage),
  }));
}

// ── F135: PM model-assignment insight ──────────────────────────────────────

export type ModelStanding =
  | "insufficient_data"
  | "preferred"
  | "cautioned"
  | "demoted";

export interface ModelLearningBucket {
  taskType: string;
  difficultyTier: string;
  attempts: number;
  accepted: number;
  acceptedRate: number;
  gatewayFailureRate: number;
  p50LatencyMs: number;
  avgCostTier: number;
  standing: ModelStanding;
}

export interface ModelLearningRoute {
  routeId: string;
  capabilityTier: string;
  costTier: number;
  tiersUnset: boolean;
  buckets: ModelLearningBucket[];
}

export interface ModelLearningDigest {
  summary: {
    totalAttempts: number;
    distinctRoutes: number;
    windowDays: number;
    generatedAt: string;
    corpusAvailable: boolean;
  };
  thresholds: { minAttempts: number; demotionRate: number; preferredRate: number };
  routes: ModelLearningRoute[];
}

/**
 * F135: the global, cross-project PM learning digest. Not scoped by project —
 * the performance corpus is shared across every project and PM.
 */
export async function getModelLearning(): Promise<ModelLearningDigest> {
  const r = (await jsonOk(
    await sidecarFetch("/coding/model-learning"),
    "get model learning",
  )) as { learning: Record<string, any> };
  const l = r.learning ?? {};
  const summary = l.summary ?? {};
  const thresholds = l.thresholds ?? {};
  return {
    summary: {
      totalAttempts: Number(summary.total_attempts ?? 0),
      distinctRoutes: Number(summary.distinct_routes ?? 0),
      windowDays: Number(summary.window_days ?? 90),
      generatedAt: String(summary.generated_at ?? ""),
      corpusAvailable: Boolean(summary.corpus_available),
    },
    thresholds: {
      minAttempts: Number(thresholds.min_attempts ?? 5),
      demotionRate: Number(thresholds.demotion_rate ?? 0.6),
      preferredRate: Number(thresholds.preferred_rate ?? 0.8),
    },
    routes: (Array.isArray(l.routes) ? l.routes : []).map((route: any) => ({
      routeId: String(route.route_id ?? ""),
      capabilityTier: String(route.capability_tier ?? "mid"),
      costTier: Number(route.cost_tier ?? 0),
      tiersUnset: Boolean(route.tiers_unset),
      buckets: (Array.isArray(route.buckets) ? route.buckets : []).map((b: any) => ({
        taskType: String(b.task_type ?? ""),
        difficultyTier: String(b.difficulty_tier ?? ""),
        attempts: Number(b.attempts ?? 0),
        accepted: Number(b.accepted ?? 0),
        acceptedRate: Number(b.accepted_rate ?? 0),
        gatewayFailureRate: Number(b.gateway_failure_rate ?? 0),
        p50LatencyMs: Number(b.p50_latency_ms ?? 0),
        avgCostTier: Number(b.avg_cost_tier ?? 0),
        standing: String(b.standing ?? "insufficient_data") as ModelStanding,
      })),
    })),
  };
}

export interface ModelUsageAssignment {
  routeId: string;
  difficultyTier: string;
  source: string;
  count: number;
  maxEscalation: number;
}

export interface ModelUsageEscalation {
  taskId: string;
  routeId: string;
  escalationCount: number;
  attemptedRouteIds: string[];
}

export interface MultiModelMember {
  memberId: string;
  role: string;
  modelMode: string;
  pool: string[];
  assignments: ModelUsageAssignment[];
  escalations: ModelUsageEscalation[];
}

export interface ProjectModelUsage {
  multiMembers: MultiModelMember[];
  singleMembers: Array<{ memberId: string; routeId: string }>;
}

/** F135: per-project model-assignment usage rollup. */
export async function getProjectModelUsage(id: string): Promise<ProjectModelUsage> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/model-usage`),
    "get model usage",
  )) as { usage: Record<string, any> };
  const u = r.usage ?? {};
  return {
    multiMembers: (Array.isArray(u.multi_members) ? u.multi_members : []).map((m: any) => ({
      memberId: String(m.member_id ?? ""),
      role: String(m.role ?? ""),
      modelMode: String(m.model_mode ?? "multi"),
      pool: Array.isArray(m.pool) ? m.pool.map(String) : [],
      assignments: (Array.isArray(m.assignments) ? m.assignments : []).map((a: any) => ({
        routeId: String(a.route_id ?? ""),
        difficultyTier: String(a.difficulty_tier ?? ""),
        source: String(a.source ?? ""),
        count: Number(a.count ?? 0),
        maxEscalation: Number(a.max_escalation ?? 0),
      })),
      escalations: (Array.isArray(m.escalations) ? m.escalations : []).map((e: any) => ({
        taskId: String(e.task_id ?? ""),
        routeId: String(e.route_id ?? ""),
        escalationCount: Number(e.escalation_count ?? 0),
        attemptedRouteIds: Array.isArray(e.attempted_route_ids)
          ? e.attempted_route_ids.map(String)
          : [],
      })),
    })),
    singleMembers: (Array.isArray(u.single_members) ? u.single_members : []).map((s: any) => ({
      memberId: String(s.member_id ?? ""),
      routeId: String(s.route_id ?? ""),
    })),
  };
}

// ── F143: per-project token usage rollup ───────────────────────────────────

/** F143-01 Slice D: measured/estimated share of a bucket's HEADLINE tokens. */
export interface TokenCoverage {
  measuredPct: number;
  estimatedPct: number;
}

export interface TokenUsageBucket {
  // Headline (effective) sums — measured-where-present, estimated otherwise.
  input: number;
  output: number;
  // Split of the headline into its measured vs estimated portions (F143-01).
  measuredInput: number;
  measuredOutput: number;
  estimatedInput: number;
  estimatedOutput: number;
  // Detail only — never folded into input/output (D4).
  cacheRead: number;
  cacheWrite: number;
  // Turn + provenance counts.
  turns: number;
  measuredTurns: number;
  partialTurns: number;
  estimatedTurns: number;
  unreportedTurns: number;
  // Share of headline tokens (not turn count) measured vs estimated.
  coverage: TokenCoverage;
}

export interface ProjectUsageSummary {
  byMember: Record<string, TokenUsageBucket>;
  byRoute: Record<string, TokenUsageBucket>;
  // F143-01 Slice D — per-role (PM / DEV / REVIEWER / TESTER) subtotal.
  byRole: Record<string, TokenUsageBucket>;
  total: TokenUsageBucket;
}

function adaptTokenBucket(raw: unknown): TokenUsageBucket {
  const b = (typeof raw === "object" && raw !== null ? raw : {}) as Record<string, unknown>;
  const n = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);
  // Defensive: older payloads omit the F143-01 fields — they default to 0 rather
  // than throwing, so a pre-feature sidecar keeps rendering.
  const cov = (typeof b.coverage === "object" && b.coverage !== null
    ? b.coverage
    : {}) as Record<string, unknown>;
  return {
    input: n(b.input),
    output: n(b.output),
    measuredInput: n(b.measured_input),
    measuredOutput: n(b.measured_output),
    estimatedInput: n(b.estimated_input),
    estimatedOutput: n(b.estimated_output),
    cacheRead: n(b.cache_read),
    cacheWrite: n(b.cache_write),
    turns: n(b.turns),
    measuredTurns: n(b.measured_turns),
    partialTurns: n(b.partial_turns),
    estimatedTurns: n(b.estimated_turns),
    unreportedTurns: n(b.unreported_turns),
    coverage: {
      measuredPct: n(cov.measured_pct),
      estimatedPct: n(cov.estimated_pct),
    },
  };
}

function adaptBucketMap(raw: unknown): Record<string, TokenUsageBucket> {
  const out: Record<string, TokenUsageBucket> = {};
  if (typeof raw === "object" && raw !== null) {
    for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
      out[k] = adaptTokenBucket(v);
    }
  }
  return out;
}

/** F143 / F143-01: per-project token-usage rollup (by member / route / role / total). */
export async function getProjectUsageSummary(id: string): Promise<ProjectUsageSummary> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/usage-summary`),
    "get usage summary",
  )) as { usage: Record<string, unknown> };
  const u = r.usage ?? {};
  return {
    byMember: adaptBucketMap(u.by_member),
    byRoute: adaptBucketMap(u.by_route),
    byRole: adaptBucketMap(u.by_role),
    total: adaptTokenBucket(u.total),
  };
}

// --- F143-01 Slice F: per-member Context Report (Layer-1 composition) ----------

/** One category of what Errorta sent into a member for a turn (Layer-1). */
export interface ContextCompositionCategory {
  /** Taxonomy class, e.g. "role_instructions" | "work_request" | "project_context"
   * | "repo_snapshot" | "prior_outputs" | "pr_diff" | "tool_guidance" | "transcript". */
  class_: string;
  tokens: number;
}

export interface ContextComposition {
  /** Sum of the category tokens = the input Errorta itself assembled and sent. */
  sentTotal: number;
  categories: ContextCompositionCategory[];
  estimatorMethod?: string | null;
}

export interface TurnComposition {
  composition: ContextComposition;
  /** For a CLI-backed member: the vendor-managed inner context we can't itemize,
   * clamp>=0(measured_input - estimated_input). Null when not derivable / non-CLI. */
  cliOverheadTokens: number | null;
  /** Layer-2 caveat for CLI members; null for direct-API members. */
  note: string | null;
}

function adaptComposition(raw: unknown): ContextComposition {
  const c = (typeof raw === "object" && raw !== null ? raw : {}) as Record<string, unknown>;
  const n = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);
  const cats = Array.isArray(c.categories) ? c.categories : [];
  return {
    sentTotal: n(c.sent_total),
    categories: cats
      .filter((e): e is Record<string, unknown> => typeof e === "object" && e !== null)
      .map((e) => ({ class_: String(e.class ?? ""), tokens: n(e.tokens) }))
      .filter((e) => e.class_ !== ""),
    estimatorMethod:
      typeof c.estimator_method === "string" ? c.estimator_method : null,
  };
}

/** F143-01 Slice F: per-turn Layer-1 Context Report (what Errorta sent, by category)
 * + the CLI vendor-overhead magnitude + a Layer-2 caveat note for CLI members. */
export async function getTurnComposition(
  projectId: string,
  taskId: string,
  turnId: string,
): Promise<TurnComposition> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(
        taskId,
      )}/turns/${encodeURIComponent(turnId)}/composition`,
    ),
    "get turn composition",
  )) as Record<string, unknown>;
  const overhead = r.cli_overhead_tokens;
  return {
    composition: adaptComposition(r.composition),
    cliOverheadTokens:
      typeof overhead === "number" && Number.isFinite(overhead) ? overhead : null,
    note: typeof r.note === "string" ? r.note : null,
  };
}

export interface PrReviewFinding {
  severity: string;
  title: string;
  body: string;
  path: string;
  blocking: boolean;
}

export interface CodingPr {
  prId: string;
  taskId: string;
  branch: string;
  status: string;
  reviewerApproved: boolean | null;
  testsPassed: boolean | null;
  conflicts: string[];
  createdAt: string;
  updatedAt: string;
  // F091: when a revise PR merges, the PRs it superseded carry this back-pointer.
  supersededByPrId?: string | null;
  // F126: the reviewer's findings — the "why" behind a changes-requested verdict.
  reviewFindings: PrReviewFinding[];
}

export async function getPrs(id: string): Promise<CodingPr[]> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/prs`),
    "get prs",
  )) as { prs: Array<Record<string, unknown>> };
  return (r.prs ?? []).map((p) => ({
    prId: String(p.pr_id ?? ""),
    taskId: String(p.task_id ?? ""),
    branch: String(p.branch ?? ""),
    status: String(p.status ?? ""),
    reviewerApproved: p.reviewer_approved == null ? null : Boolean(p.reviewer_approved),
    testsPassed: p.tests_passed == null ? null : Boolean(p.tests_passed),
    conflicts: (p.conflicts as string[]) ?? [],
    createdAt: String(p.created_at ?? ""),
    updatedAt: String(p.updated_at ?? ""),
    supersededByPrId: p.superseded_by_pr_id == null ? null : String(p.superseded_by_pr_id),
    reviewFindings: ((p.review_findings as Array<Record<string, unknown>>) ?? []).map(
      (f) => ({
        severity: String(f.severity ?? ""),
        title: String(f.title ?? ""),
        body: String(f.body ?? ""),
        path: String(f.path ?? ""),
        blocking: Boolean(f.blocking),
      }),
    ),
  }));
}

export async function fetchRunLog(id: string): Promise<string> {
  const r = await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/run-log.txt`);
  if (!r.ok) throw new Error(`run log: ${r.status}`);
  return r.text();
}

export async function getTestSettings(id: string): Promise<{ requireSandbox: boolean }> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/test-settings`),
    "get test settings",
  )) as { require_sandbox: boolean };
  return { requireSandbox: Boolean(r.require_sandbox) };
}

export async function putTestSettings(id: string, requireSandbox: boolean): Promise<void> {
  await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/test-settings`, {
      method: "PUT",
      headers: UI_ORIGIN,
      body: JSON.stringify({ require_sandbox: requireSandbox }),
    }),
    "put test settings",
  );
}

export interface RunStatus {
  running: boolean;
  result: Record<string, unknown> | null;
  state?: Record<string, unknown>;
  recoverable: boolean;
  canResume: boolean;
}

/**
 * F093: the run's terminal stop_reason, read off `result`. Note `result` is
 * passed through un-adapted by `getRunStatus`, so the key is snake_case
 * `stop_reason` — reading `result.stopReason` (camel) would be `undefined`.
 */
export function runStopReason(s: RunStatus | null | undefined): string | null {
  const r = s?.result;
  if (!r) return null;
  return (r["stop_reason"] as string | null) ?? null;
}

/**
 * F121: the backend run-state `status` (running/stopped/interrupted/failed),
 * read off the snake_case `state` passthrough. `null` before the first poll.
 */
export function runStateStatus(s: RunStatus | null | undefined): string | null {
  const st = s?.state;
  if (!st) return null;
  const v = st["status"];
  return typeof v === "string" ? v : null;
}

/**
 * F121: whether a cancel has been requested for this run. The backend sets
 * `cancel_requested=true` on `POST /run/cancel`; the loop honors it at its next
 * turn boundary. We derive the "Stopping…" state from this so it survives a
 * reload/poll (not just the optimistic click). Snake_case key off `state`.
 */
export function runCancelRequested(s: RunStatus | null | undefined): boolean {
  const st = s?.state;
  if (!st) return false;
  return Boolean(st["cancel_requested"]);
}

// F120-04: the typed error a /run start raises when the pre-run preflight
// refuses to start because one or more providers are logged-out / missing. The
// shell renders the unhealthy list as the PreflightBlockedBanner.
export interface PreflightUnhealthyEntry {
  provider: string;
  route: string;
  reason: string;
  detail: string;
  remediation: string;
  memberIds: string[];
}

export class RunPreflightBlocked extends Error {
  readonly unhealthy: PreflightUnhealthyEntry[];
  constructor(message: string, unhealthy: PreflightUnhealthyEntry[]) {
    super(message);
    this.name = "RunPreflightBlocked";
    this.unhealthy = unhealthy;
  }
}

// F121-B1: the typed error a /run start raises when the project's readiness gate
// hasn't been confirmed (belt-and-suspenders behind the frontend gate). The
// shell catches it and opens the Run setup gate instead of erroring.
export class RunSetupRequired extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RunSetupRequired";
  }
}

// The typed error a /run/resume raises when the run's workspace no longer matches
// the fingerprint captured at interrupt time (worktrees changed/removed). Resume
// refuses by design; the shell catches this and recovers with a fresh start
// (which reuses the persistent repo on master and re-queues unfinished tasks),
// instead of leaving the user stuck on the only "Resume" affordance.
export class RunWorkspaceIntegrityError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RunWorkspaceIntegrityError";
  }
}

function preflightEntryFrom(raw: Record<string, unknown>): PreflightUnhealthyEntry {
  return {
    provider: String(raw.provider ?? ""),
    route: String(raw.route ?? ""),
    reason: String(raw.reason ?? ""),
    detail: String(raw.detail ?? ""),
    remediation: String(raw.remediation ?? ""),
    memberIds: Array.isArray(raw.member_ids) ? (raw.member_ids as unknown[]).map(String) : [],
  };
}

// F121 Part B — the readiness gate ("Run setup") config + client.
//
// `RunSetupConfig` is the resolved, flat config the gate edits and confirms. It
// round-trips both the project's live state (governance + autonomy + guardrail)
// and the user-level sticky defaults seed for a fresh project. Every field is
// optional in flight so the gate can pre-fill from either source.
export interface RunSetupConfig {
  governanceMode?: GovernanceMode;
  blockOnProblems?: boolean;
  humanCodeApproval?: HumanCodeApproval;
  maxReviewRounds?: number;
  checkpointCadence?: CheckpointCadence;
  checkpointN?: number;
  guardrailEnabled?: boolean;
  groundingEnabled?: boolean;
  maxIterations?: number;
  maxModelCalls?: number | null;
  maxParallelWorkers?: number | null;
  memberFailureLimit?: number;
  preflightEnabled?: boolean;
  teamRoomId?: string;
}

export interface RunSetupState {
  runSetupConfirmed: boolean;
  governance: Record<string, unknown>;
  autonomy: Record<string, unknown>;
  guardrailEnabled: boolean;
  memberHealthPreflight: boolean;
  /** The user-level last-used config seed (empty before the first confirm). */
  defaults: RunSetupConfig;
}

function runSetupConfigFromRaw(raw: Record<string, unknown>): RunSetupConfig {
  const num = (v: unknown): number | undefined =>
    typeof v === "number" ? v : undefined;
  return {
    governanceMode: parseGovernanceMode(raw.governance_mode),
    blockOnProblems: typeof raw.block_on_problems === "boolean" ? raw.block_on_problems : undefined,
    humanCodeApproval: parseHumanCodeApproval(raw.human_code_approval),
    maxReviewRounds: num(raw.max_review_rounds),
    checkpointCadence: parseCheckpointCadence(raw.checkpoint_cadence),
    checkpointN: num(raw.checkpoint_n),
    guardrailEnabled: typeof raw.guardrail_enabled === "boolean" ? raw.guardrail_enabled : undefined,
    groundingEnabled: typeof raw.grounding_enabled === "boolean" ? raw.grounding_enabled : undefined,
    maxIterations: num(raw.max_iterations),
    maxModelCalls: raw.max_model_calls === null ? null : num(raw.max_model_calls),
    maxParallelWorkers: raw.max_parallel_workers === null ? null : num(raw.max_parallel_workers),
    memberFailureLimit: num(raw.member_failure_limit),
    preflightEnabled: typeof raw.preflight_enabled === "boolean" ? raw.preflight_enabled : undefined,
    teamRoomId: typeof raw.team_room_id === "string" ? raw.team_room_id : undefined,
  };
}

function runSetupConfigToWire(cfg: RunSetupConfig): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (cfg.governanceMode !== undefined) out.governance_mode = cfg.governanceMode;
  if (cfg.blockOnProblems !== undefined) out.block_on_problems = cfg.blockOnProblems;
  if (cfg.humanCodeApproval !== undefined) out.human_code_approval = cfg.humanCodeApproval;
  if (cfg.maxReviewRounds !== undefined) out.max_review_rounds = cfg.maxReviewRounds;
  if (cfg.checkpointCadence !== undefined) out.checkpoint_cadence = cfg.checkpointCadence;
  if (cfg.checkpointN !== undefined) out.checkpoint_n = cfg.checkpointN;
  if (cfg.guardrailEnabled !== undefined) out.guardrail_enabled = cfg.guardrailEnabled;
  if (cfg.maxIterations !== undefined) out.max_iterations = cfg.maxIterations;
  if (cfg.maxModelCalls !== undefined) out.max_model_calls = cfg.maxModelCalls;
  if (cfg.maxParallelWorkers !== undefined) out.max_parallel_workers = cfg.maxParallelWorkers;
  if (cfg.memberFailureLimit !== undefined) out.member_failure_limit = cfg.memberFailureLimit;
  if (cfg.preflightEnabled !== undefined) out.preflight_enabled = cfg.preflightEnabled;
  if (cfg.teamRoomId !== undefined) out.team_room_id = cfg.teamRoomId;
  return out;
}

export async function getRunSetup(id: string): Promise<RunSetupState> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/run-setup`),
    "run setup",
  )) as Record<string, unknown>;
  return {
    runSetupConfirmed: Boolean(r.run_setup_confirmed),
    governance: (r.governance as Record<string, unknown>) ?? {},
    autonomy: (r.autonomy as Record<string, unknown>) ?? {},
    guardrailEnabled: Boolean(r.guardrail_enabled),
    memberHealthPreflight: Boolean(r.member_health_preflight),
    defaults: runSetupConfigFromRaw((r.defaults as Record<string, unknown>) ?? {}),
  };
}

export async function runSetupPreflight(
  id: string,
  roomId: string,
): Promise<PreflightUnhealthyEntry[]> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/run-setup/preflight`, {
      method: "POST",
      headers: UI_ORIGIN,
      body: JSON.stringify({ room_id: roomId }),
    }),
    "run setup preflight",
  )) as { unhealthy?: unknown[] };
  return Array.isArray(r.unhealthy)
    ? r.unhealthy.map((u) => preflightEntryFrom(u as Record<string, unknown>))
    : [];
}

export async function confirmRunSetup(
  id: string,
  cfg: RunSetupConfig,
): Promise<boolean> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/run-setup/confirm`, {
      method: "POST",
      headers: UI_ORIGIN,
      body: JSON.stringify(runSetupConfigToWire(cfg)),
    }),
    "confirm run setup",
  )) as { run_setup_confirmed: boolean };
  return Boolean(r.run_setup_confirmed);
}

export async function startRun(
  id: string,
  members?: Array<Record<string, unknown>>,
  roomId?: string,
): Promise<boolean> {
  const body: Record<string, unknown> = {};
  if (members) body.members = members;
  if (roomId) body.room_id = roomId;
  const res = await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/run`, {
    method: "POST",
    headers: UI_ORIGIN,
    body: JSON.stringify(body),
  });
  if (res.status === 409) {
    // Distinguish the F120 preflight refusal (structured detail) from the
    // generic "a run is already in progress" 409.
    const data = (await res.json().catch(() => null)) as
      | { detail?: { code?: string; message?: string; unhealthy?: unknown[] } | string }
      | null;
    const detail = data?.detail;
    if (detail && typeof detail === "object" && detail.code === "member_health_preflight_failed") {
      const unhealthy = Array.isArray(detail.unhealthy)
        ? detail.unhealthy.map((u) => preflightEntryFrom(u as Record<string, unknown>))
        : [];
      throw new RunPreflightBlocked(
        detail.message || "Can't start: a provider isn't ready.",
        unhealthy,
      );
    }
    if (detail && typeof detail === "object" && detail.code === "run_setup_required") {
      throw new RunSetupRequired(detail.message || "Run setup hasn't been confirmed.");
    }
    throw new Error(`start run failed (409)`);
  }
  const r = (await jsonOk(res, "start run")) as { started: boolean };
  return Boolean(r.started);
}

export async function getRunStatus(id: string): Promise<RunStatus> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/run`),
    "run status",
  )) as Record<string, unknown>;
  return {
    running: Boolean(r.running),
    result: (r.result as Record<string, unknown> | null) ?? null,
    state: (r.state as Record<string, unknown>) ?? undefined,
    recoverable: Boolean(r.recoverable),
    canResume: Boolean(r.can_resume),
  };
}

export async function cancelRun(id: string): Promise<boolean> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/run/cancel`, {
      method: "POST",
      headers: UI_ORIGIN,
      body: "{}",
    }),
    "cancel run",
  )) as { cancelled: boolean };
  return Boolean(r.cancelled);
}

export async function resumeRun(
  id: string,
  members?: Array<Record<string, unknown>>,
  roomId?: string,
): Promise<boolean> {
  // F097: members/room_id are optional — the backend recovers the run's saved
  // team when the body is empty. On failure, surface the backend's structured
  // detail (e.g. run_config_missing) so the message is actionable.
  const body: Record<string, unknown> = {};
  if (members) body.members = members;
  if (roomId) body.room_id = roomId;
  const res = await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/run/resume`, {
    method: "POST",
    headers: UI_ORIGIN,
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail: unknown = null;
    try {
      detail = ((await res.json()) as { detail?: unknown })?.detail;
    } catch {
      /* non-JSON body */
    }
    const obj =
      detail && typeof detail === "object" ? (detail as Record<string, unknown>) : null;
    // Integrity failure is a bare-string detail; recognize it so the shell can
    // recover with a fresh start instead of a dead-end error.
    if (detail === "workspace_integrity_failed" || obj?.code === "workspace_integrity_failed") {
      throw new RunWorkspaceIntegrityError(
        "This run's workspace changed since it was interrupted, so it can't be resumed as-is.",
      );
    }
    const message =
      obj && typeof obj.message === "string"
        ? obj.message
        : `resume run failed (${res.status})`;
    const err = new Error(message) as Error & { code?: string };
    if (obj && typeof obj.code === "string") err.code = obj.code;
    throw err;
  }
  const r = (await res.json()) as { started: boolean };
  return Boolean(r.started);
}

export async function continueRun(
  id: string,
  members?: Array<Record<string, unknown>>,
  roomId?: string,
): Promise<boolean> {
  // F100 governance continuation: re-drive a run that STOPPED at a review/gate.
  // Unlike resumeRun (crash-recovery only — 409s a stopped governance run), this
  // hits /run/continue, which starts a fresh worker over the same ledger so the
  // PM re-drafts the stuck artifact with the queued interjection in context. The
  // saved team is recovered server-side when the body is empty. On failure the
  // backend's structured detail (e.g. run_config_missing) is surfaced.
  const body: Record<string, unknown> = {};
  if (members) body.members = members;
  if (roomId) body.room_id = roomId;
  const res = await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/run/continue`, {
    method: "POST",
    headers: UI_ORIGIN,
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail: unknown = null;
    try {
      detail = ((await res.json()) as { detail?: unknown })?.detail;
    } catch {
      /* non-JSON body */
    }
    const obj =
      detail && typeof detail === "object" ? (detail as Record<string, unknown>) : null;
    const message =
      obj && typeof obj.message === "string"
        ? obj.message
        : `continue run failed (${res.status})`;
    const err = new Error(message) as Error & { code?: string };
    if (obj && typeof obj.code === "string") err.code = obj.code;
    throw err;
  }
  const r = (await res.json()) as { started: boolean };
  return Boolean(r.started);
}

export async function addTask(id: string, title: string, role: string): Promise<void> {
  await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(id)}/tasks`, {
      method: "POST",
      headers: UI_ORIGIN,
      body: JSON.stringify({ title, role }),
    }),
    "add task",
  );
}

// ── F102 — publishing (P1 manual export + P2 auth-status detection) ──────────
//
// The backend owns all GitHub egress, the keychain, and event redaction. This
// client only models the read/manual surface; no token ever crosses the wire
// (auth-status never returns one — and we never render it if it somehow did).

export type ManualExportKind = "zip" | "patch" | "git_apply" | "open_folder";

export interface ManualExportResult {
  kind: ManualExportKind;
  // The union is modeled loosely: each kind populates a subset of these.
  path?: string | null;
  diff?: string | null;
  command?: string | null;
  runHint?: string | null;
}

export interface PublishAuthStatus {
  ghPresent: boolean;
  tokenInKeychain: boolean;
  login: string | null;
}

export interface PublishTarget {
  targetId: string;
  kind: string;
  repoPath: string | null;
  githubOwner: string | null;
  githubRepo: string | null;
  defaultBranch: string | null;
  privacy: string | null;
  createdAt: string;
  lastPublishedAt: string | null;
}

export interface PublishEvent {
  eventId: string;
  targetId: string;
  kind: string;
  state: string;
  branch: string | null;
  commitSha: string | null;
  prUrl: string | null;
  error: string | null;
  createdAt: string;
}

function publishBase(projectId: string): string {
  return `/coding/projects/${encodeURIComponent(projectId)}/publish`;
}

function publishTargetFrom(raw: Record<string, unknown>): PublishTarget {
  return {
    targetId: String(raw.target_id ?? ""),
    kind: String(raw.kind ?? ""),
    repoPath: (raw.repo_path as string | null) ?? null,
    githubOwner: (raw.github_owner as string | null) ?? null,
    githubRepo: (raw.github_repo as string | null) ?? null,
    defaultBranch: (raw.default_branch as string | null) ?? null,
    privacy: (raw.privacy as string | null) ?? null,
    createdAt: String(raw.created_at ?? ""),
    lastPublishedAt: (raw.last_published_at as string | null) ?? null,
  };
}

function publishEventFrom(raw: Record<string, unknown>): PublishEvent {
  return {
    eventId: String(raw.event_id ?? ""),
    targetId: String(raw.target_id ?? ""),
    kind: String(raw.kind ?? ""),
    state: String(raw.state ?? ""),
    branch: (raw.branch as string | null) ?? null,
    commitSha: (raw.commit_sha as string | null) ?? null,
    prUrl: (raw.pr_url as string | null) ?? null,
    error: (raw.error as string | null) ?? null,
    createdAt: String(raw.created_at ?? ""),
  };
}

export async function manualExport(
  projectId: string,
  kind: ManualExportKind,
  dest?: string,
): Promise<ManualExportResult> {
  const body: Record<string, unknown> = { kind };
  if (dest) body.dest = dest;
  const r = (await jsonOk(
    await sidecarFetch(`${publishBase(projectId)}/manual-export`, {
      method: "POST",
      headers: UI_ORIGIN,
      body: JSON.stringify(body),
    }),
    "manual export",
  )) as Record<string, unknown>;
  return {
    kind: (r.kind as ManualExportKind) ?? kind,
    path: (r.path as string | null) ?? null,
    diff: (r.patch as string | null) ?? (r.diff as string | null) ?? null,
    command: (r.command as string | null) ?? null,
    runHint: (r.run_hint as string | null) ?? null,
  };
}

export async function getPublishAuthStatus(projectId: string): Promise<PublishAuthStatus> {
  const r = (await jsonOk(
    await sidecarFetch(`${publishBase(projectId)}/auth-status`, { headers: UI_ORIGIN }),
    "publish auth status",
  )) as Record<string, unknown>;
  return {
    ghPresent: Boolean(r.gh_present),
    tokenInKeychain: Boolean(r.token_in_keychain),
    login: (r.login as string | null) ?? null,
  };
}

export async function getPublishEvents(projectId: string): Promise<PublishEvent[]> {
  const r = (await jsonOk(
    await sidecarFetch(`${publishBase(projectId)}/events`, { headers: UI_ORIGIN }),
    "publish events",
  )) as { events?: Array<Record<string, unknown>> };
  return (r.events ?? []).map(publishEventFrom);
}

export async function getPublishTargets(projectId: string): Promise<PublishTarget[]> {
  const r = (await jsonOk(
    await sidecarFetch(`${publishBase(projectId)}/targets`, { headers: UI_ORIGIN }),
    "publish targets",
  )) as { targets?: Array<Record<string, unknown>> };
  return (r.targets ?? []).map(publishTargetFrom);
}

// ── F102 P3/P4 — GitHub publishing (existing-repo PR + new repo) ─────────────
//
// The backend owns ALL git/gh egress, the secret scan, and event redaction.
// These clients model the success shapes and map the backend's 409 reasons into
// a typed `PublishBlocked` error so the UI can branch (scan findings + override,
// clobber refusal + unrelated paths, not-delivered/no-origin, etc.). No token
// ever crosses the wire.

/** A single secret/path scan finding (redacted excerpt only — never a raw secret). */
export interface PublishScanFinding {
  path: string;
  kind: string;
  line: number | null;
  redactedExcerpt: string;
}

export interface PublishPrResult {
  branch: string;
  base: string;
  commitSha: string;
  prUrl: string;
  events: PublishEvent[];
}

export interface PublishRepoResult {
  localOnly: boolean;
  repoUrl: string | null;
  localPath: string | null;
  private: boolean | null;
  commitSha: string;
  fileList: string[];
  events: PublishEvent[];
}

/**
 * A 409 from a GitHub publish route. `reason` is the stable machine code
 * (`not_delivered`, `open_tasks`, `no_origin`, `not_a_git_repo`,
 * `not_existing_target`, `repo_state_unsafe`, `clobber_unrelated_changes`,
 * `secret_scan_hit`, `invalid_repo_name`, `local_dest_exists`, `egress_failed`).
 * `findings` is present on a scan hit (retryable with `override:true`);
 * `dirtyPaths` is present on a clobber refusal.
 */
export class PublishBlocked extends Error {
  reason: string;
  findings: PublishScanFinding[] | null;
  dirtyPaths: string[] | null;
  /** The full merge-gate blocker code list (e.g. ["tests_missing"]) when the
   * refusal came from the evidence gate; null otherwise. `reason` is the first
   * of these. */
  blockers: string[] | null;
  status: number;
  constructor(
    reason: string,
    message: string,
    opts: {
      findings?: PublishScanFinding[] | null;
      dirtyPaths?: string[] | null;
      blockers?: string[] | null;
      status?: number;
    } = {},
  ) {
    super(message);
    this.name = "PublishBlocked";
    this.reason = reason;
    this.findings = opts.findings ?? null;
    this.dirtyPaths = opts.dirtyPaths ?? null;
    this.blockers = opts.blockers ?? null;
    this.status = opts.status ?? 409;
  }
}

function scanFindingFrom(raw: Record<string, unknown>): PublishScanFinding {
  return {
    path: String(raw.path ?? ""),
    kind: String(raw.kind ?? ""),
    line: typeof raw.line === "number" ? raw.line : null,
    redactedExcerpt: String(raw.redacted_excerpt ?? ""),
  };
}

/**
 * Map a non-2xx publish response into a typed error. The backend returns
 * `{detail: {error, detail}}` for a `PublishGateError`. Returns the parsed JSON
 * body on success.
 */
async function publishOk(res: Response, what: string): Promise<Record<string, unknown>> {
  if (res.ok) return (await res.json()) as Record<string, unknown>;
  let detail: { error?: string; detail?: unknown } | null = null;
  try {
    const body = (await res.json()) as { detail?: { error?: string; detail?: unknown } };
    detail = body.detail ?? null;
  } catch {
    /* non-JSON body */
  }
  const reason = detail?.error;
  if (reason) {
    const ctx = (detail?.detail ?? null) as Record<string, unknown> | null;
    if (reason === "secret_scan_hit") {
      const findings = Array.isArray(ctx?.findings)
        ? (ctx?.findings as Array<Record<string, unknown>>).map(scanFindingFrom)
        : [];
      throw new PublishBlocked(reason, "Secret scan found sensitive content.", {
        findings,
        status: res.status,
      });
    }
    if (reason === "clobber_unrelated_changes") {
      const dirtyPaths = Array.isArray(ctx?.unrelated_paths)
        ? (ctx?.unrelated_paths as unknown[]).map(String)
        : [];
      throw new PublishBlocked(reason, "Unrelated local changes would be clobbered.", {
        dirtyPaths,
        status: res.status,
      });
    }
    const blockers = Array.isArray(ctx?.blockers)
      ? (ctx?.blockers as unknown[]).map(String)
      : null;
    throw new PublishBlocked(reason, reason, { blockers, status: res.status });
  }
  throw new Error(`${what} failed (${res.status})`);
}

export async function publishExistingRepoPr(
  projectId: string,
  // F135: optional PM-drafted branch/title/body; omitted → F102 defaults.
  opts: {
    override?: boolean;
    branch?: string | null;
    title?: string | null;
    bodyOverride?: string | null;
  } = {},
): Promise<PublishPrResult> {
  const body: Record<string, unknown> = { override: Boolean(opts.override) };
  if (opts.branch) body.branch = opts.branch;
  if (opts.title) body.title = opts.title;
  if (opts.bodyOverride) body.body_override = opts.bodyOverride;
  const raw = await publishOk(
    await sidecarFetch(`${publishBase(projectId)}/existing-repo-pr`, {
      method: "POST",
      headers: UI_ORIGIN,
      body: JSON.stringify(body),
    }),
    "open PR",
  );
  return {
    branch: String(raw.branch ?? ""),
    base: String(raw.base ?? ""),
    commitSha: String(raw.commit_sha ?? ""),
    prUrl: String(raw.pr_url ?? ""),
    events: ((raw.events as Array<Record<string, unknown>>) ?? []).map(publishEventFrom),
  };
}

export async function publishNewGithubRepo(
  projectId: string,
  opts: { repoName: string; private?: boolean; localOnly?: boolean; override?: boolean },
): Promise<PublishRepoResult> {
  const raw = await publishOk(
    await sidecarFetch(`${publishBase(projectId)}/new-github-repo`, {
      method: "POST",
      headers: UI_ORIGIN,
      body: JSON.stringify({
        repo_name: opts.repoName,
        private: opts.private ?? true,
        local_only: Boolean(opts.localOnly),
        override: Boolean(opts.override),
      }),
    }),
    "create repo",
  );
  return {
    localOnly: Boolean(raw.local_only),
    repoUrl: (raw.repo_url as string | null) ?? null,
    localPath: (raw.local_path as string | null) ?? null,
    private: typeof raw.private === "boolean" ? raw.private : null,
    commitSha: String(raw.commit_sha ?? ""),
    fileList: Array.isArray(raw.initial_files)
      ? (raw.initial_files as unknown[]).map(String)
      : [],
    events: ((raw.events as Array<Record<string, unknown>>) ?? []).map(publishEventFrom),
  };
}

// --- F145: AI Wizard + PM control plane ------------------------------------

export interface WizardModel {
  routeId: string;
  family: string;
  providerClass: string;
}

export interface WizardCharter {
  north_star: string;
  audience: string;
  modality: string;
  definition_of_done: string;
  entrypoint: string;
  scope_notes?: string;
  team_recipe: string;
  autonomous: boolean;
}

export interface WizardTurn {
  reply: string;
  ready: boolean;
  charter: Record<string, unknown>;
  missing: string[];
  error?: string;
}

function modelFrom(raw: Record<string, unknown>): WizardModel {
  return {
    routeId: String(raw.route_id ?? ""),
    family: String(raw.family ?? ""),
    providerClass: String(raw.provider_class ?? ""),
  };
}

export async function getWizardModels(): Promise<WizardModel[]> {
  const r = (await jsonOk(
    await sidecarFetch("/coding/wizard/models", { headers: UI_ORIGIN }),
    "wizard models",
  )) as { routes: Array<Record<string, unknown>> };
  return (r.routes ?? []).map(modelFrom);
}

export async function wizardStart(
  modelRoute: string,
): Promise<{ sessionId: string; reply: string; availableRoutes: WizardModel[] }> {
  const r = (await jsonOk(
    await sidecarFetch("/coding/wizard/start", {
      method: "POST",
      headers: UI_ORIGIN,
      body: JSON.stringify({ model_route: modelRoute }),
    }),
    "wizard start",
  )) as { session_id: string; reply: string; available_routes: Array<Record<string, unknown>> };
  return {
    sessionId: r.session_id,
    reply: r.reply,
    availableRoutes: (r.available_routes ?? []).map(modelFrom),
  };
}

export async function wizardMessage(
  sessionId: string,
  message: string,
): Promise<WizardTurn> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/wizard/${encodeURIComponent(sessionId)}/message`, {
      method: "POST",
      headers: UI_ORIGIN,
      body: JSON.stringify({ message }),
    }),
    "wizard message",
  )) as WizardTurn;
  return r;
}

export async function wizardCreate(
  sessionId: string,
  projectId: string,
): Promise<{ projectId: string; teamSize: number; runSetupConfirmed: boolean; warnings: string[] }> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/wizard/${encodeURIComponent(sessionId)}/create`, {
      method: "POST",
      headers: UI_ORIGIN,
      body: JSON.stringify({ project_id: projectId }),
    }),
    "wizard create",
  )) as { project_id: string; team_size: number; run_setup_confirmed: boolean; warnings: string[] };
  return {
    projectId: r.project_id,
    teamSize: r.team_size,
    runSetupConfirmed: r.run_setup_confirmed,
    warnings: r.warnings ?? [],
  };
}

export interface PmChange {
  changeId: string;
  summary: string;
  details: Array<{ field: string; before: unknown; after: unknown }>;
  surface: string;
  autonomy: { warning: boolean; suggested_cap: number | null } | null;
  status: string;
  at: string;
}

function pmChangeFrom(raw: Record<string, unknown>): PmChange {
  return {
    changeId: String(raw.change_id ?? ""),
    summary: String(raw.summary ?? ""),
    details: (raw.details as PmChange["details"]) ?? [],
    surface: String(raw.surface ?? "pop"),
    autonomy: (raw.autonomy as PmChange["autonomy"]) ?? null,
    status: String(raw.status ?? "pending"),
    at: String(raw.at ?? ""),
  };
}

export async function listPmChanges(projectId: string): Promise<PmChange[]> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(projectId)}/pm-changes`, {
      headers: UI_ORIGIN,
    }),
    "pm changes",
  )) as { pending: Array<Record<string, unknown>> };
  return (r.pending ?? []).map(pmChangeFrom);
}

export async function resolvePmChange(
  projectId: string,
  changeId: string,
  action: "accept" | "decline",
): Promise<PmChange> {
  const r = (await jsonOk(
    await sidecarFetch(
      `/coding/projects/${encodeURIComponent(projectId)}/pm-changes/${encodeURIComponent(changeId)}/${action}`,
      { method: "POST", headers: UI_ORIGIN },
    ),
    `pm change ${action}`,
  )) as { change: Record<string, unknown> };
  return pmChangeFrom(r.change);
}

export async function pmControl(
  projectId: string,
  input: { directive?: string; actions?: unknown[]; surface?: "pop" | "log" },
): Promise<{ applied: PmChange[]; refusals: Array<Record<string, unknown>> }> {
  const r = (await jsonOk(
    await sidecarFetch(`/coding/projects/${encodeURIComponent(projectId)}/pm-control`, {
      method: "POST",
      headers: UI_ORIGIN,
      body: JSON.stringify(input),
    }),
    "pm control",
  )) as { applied: Array<Record<string, unknown>>; refusals: Array<Record<string, unknown>> };
  return { applied: (r.applied ?? []).map(pmChangeFrom), refusals: r.refusals ?? [] };
}
