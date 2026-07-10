// F087-06 — Coding Mode workspace viewer (presentational).
//
// A window into an autonomous coding project: the North Star, the task backlog
// board, run controls (the guardrail + autonomy controls live in the PM
// Governance panel), the decision log, the artifact index, an intervention
// composer, and the
// human-gated merge-back accept. Data + callbacks are injected so the panel is
// testable and the container owns the API wiring.
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import "./coding.css";
import FileEditor from "./FileEditor";
import { formatTokens } from "./formatTokens";
import MemberContextReport from "./MemberContextReport";
import {
  getFile,
  getPmChat,
  pmAsk,
  type PmChatTurn,
  type CodingArtifact,
  type CodingFile,
  type CodingDecision,
  type Delivery,
  type CodingInterjection,
  type CodingPmReply,
  type CodingPr,
  type CodingProject,
  type CodingTask,
  type CodingToolEvent,
  type CodingTurn,
  type GovernanceApproval,
  type GovernanceArtifact,
  type GovernanceSummary,
  type RunStatus,
  type TestCommand,
  type TestRun,
} from "../../lib/api/coding";
import { runStopReason } from "../../lib/api/coding";
import { deriveRunPhase, type RunPhase } from "./runPhase";

const RUN_BUTTON_INTENT_BRIDGE_MS = 4_000;

// Feature flag: the Test Commands editor panel. Scrapped for now — the tester
// still runs any registered commands, but the UI for viewing/editing them is
// hidden. Flip to true to bring the panel back.
const TEST_COMMANDS_ENABLED = false;

const TASK_STATES: Array<{ key: string; label: string }> = [
  { key: "todo", label: "To do" },
  { key: "doing", label: "Doing" },
  { key: "blocked", label: "Blocked" },
  { key: "done", label: "Done" },
];

const PR_STATUS_OPTIONS = ["open", "mergeable", "changes_requested", "merged", "closed", "conflict", "abandoned", "superseded"];

type TaskBadgeTone = "neutral" | "ok" | "warn" | "error";

interface TaskBadge {
  label: string;
  tone: TaskBadgeTone;
}

/**
 * F143: per-turn token badge in the Run log — "↓in ↑out" when the provider
 * reported usage, or a muted "— tok" (never 0) when it did not, so an unreported
 * turn is never mistaken for a free one.
 */
function TurnTokens({ turn }: { turn: CodingTurn }) {
  const u = turn.usage;
  const measured = Boolean(u?.measured) && (u?.inputTokens != null || u?.outputTokens != null);
  if (!measured || !u) {
    return (
      <span className="coding-turn-tokens coding-turn-tokens-none" title="Tokens not reported by this provider">
        — tok
      </span>
    );
  }
  return (
    <span
      className="coding-turn-tokens"
      title={`${u.inputTokens ?? 0} input · ${u.outputTokens ?? 0} output tokens`}
    >
      ↓{formatTokens(u.inputTokens ?? 0)} ↑{formatTokens(u.outputTokens ?? 0)}
    </span>
  );
}

/**
 * F143-01 Slice F — the per-turn Context Report, mounted inside a turn's detail as
 * its own collapsed `<details>` so it fetches the `.../composition` endpoint only
 * when the operator expands it (no N fetches for a long turn list). Self-contained
 * via MemberContextReport, which fetches on mount.
 *
 * NOTE (deeper integration): the Team Log narrative panel does not carry turn_id, so
 * the Context Report is surfaced here on the verbatim Turns list (which does). To
 * reach it from a Team Log row, the team-log endpoint would need to thread the
 * originating turn_id per entry — that plumbing is intentionally out of this slice.
 */
function TurnContextReport({ projectId, turn }: { projectId: string; turn: CodingTurn }) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="coding-turn-ctxreport"
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary>Context report</summary>
      {open ? (
        <MemberContextReport
          projectId={projectId}
          taskId={turn.taskId}
          turnId={turn.turnId}
          label={turn.role}
        />
      ) : null}
    </details>
  );
}

function parseTime(value: string): number {
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function formatTime(value: string): string {
  const parsed = parseTime(value);
  if (!parsed) return "time unknown";
  return new Date(parsed).toLocaleString();
}

function newestPr(prs: CodingPr[]): CodingPr | null {
  let newest: CodingPr | null = null;
  for (const pr of prs) {
    if (!newest || parseTime(pr.updatedAt || pr.createdAt) > parseTime(newest.updatedAt || newest.createdAt)) {
      newest = pr;
    }
  }
  return newest;
}

function prStatusBadge(pr: CodingPr): TaskBadge {
  switch (pr.status) {
    case "merged":
      return { label: "merged", tone: "ok" };
    case "mergeable":
      return { label: "ready to merge", tone: "ok" };
    case "changes_requested":
      return { label: "changes requested", tone: "error" };
    case "conflict":
      return { label: "conflict", tone: "error" };
    case "closed":
    case "abandoned":
      return { label: pr.status.replace("_", " "), tone: "warn" };
    case "superseded":
      // F091: terminal — its work was redone on a merged sibling. warn tone
      // (matches abandoned), distinct from the error tone of changes_requested.
      return { label: "superseded", tone: "warn" };
    case "open":
      return { label: "PR open", tone: "neutral" };
    default:
      return { label: pr.status || "PR recorded", tone: "neutral" };
  }
}

function reviewBadge(pr: CodingPr): TaskBadge | null {
  if (pr.reviewerApproved === true) return { label: "review approved", tone: "ok" };
  if (pr.reviewerApproved === false) return { label: "review changes", tone: "error" };
  return null;
}

type SummaryTone = "complete" | "paused" | "warn" | "running";

// F093: map the run's terminal outcome to a headline badge. Every non-completion
// stop_reason renders a ⚠ badge (never blank) so the panel is honest about a
// stalled/budget-exhausted run, not just `definition_of_done`.
function summaryOutcome(
  running: boolean,
  runStatus: RunStatus | null | undefined,
  projectStatus: string,
): { label: string; tone: SummaryTone } {
  if (running) return { label: "▶ Running", tone: "running" };
  const reason = runStopReason(runStatus);
  switch (reason) {
    case "definition_of_done":
      return { label: "✓ Complete", tone: "complete" };
    case "checkpoint":
      return { label: "⏸ Paused at checkpoint", tone: "paused" };
    case "cancelled":
    case "interrupted":
      return { label: "⏸ Interrupted / Cancelled", tone: "paused" };
    case "no_progress":
    case "no_actionable_work":
    case "budget_exhausted":
    case "hard_blocker":
    case "member_unhealthy":
    case "worker_unproductive":
    case "completion_blocked":
      return { label: `⚠ Stopped without completing (${reason})`, tone: "warn" };
    default:
      return projectStatus === "done"
        ? { label: "✓ Complete", tone: "complete" }
        : { label: "Idle", tone: "warn" };
  }
}

function testBadge(pr: CodingPr | null, testRun: TestRun | null): TaskBadge | null {
  if (pr?.testsPassed === true) return { label: "tests passed", tone: "ok" };
  if (pr?.testsPassed === false) return { label: "tests failed", tone: "error" };
  if (testRun) {
    return testRun.passed
      ? { label: "tests passed", tone: "ok" }
      : { label: "tests failed", tone: "error" };
  }
  return null;
}

function artifactBodyText(artifact: GovernanceArtifact | null): string {
  if (!artifact) return "";
  if (artifact.bodyMarkdown?.trim()) return artifact.bodyMarkdown.trim();
  if (artifact.bodyJson) return JSON.stringify(artifact.bodyJson, null, 2);
  return "";
}

function approvalText(approval: GovernanceApproval): string {
  const actor = approval.requiredActor || "user";
  if (approval.state === "approved") {
    return `${actor} approved${approval.resolvedAt ? ` at ${formatTime(approval.resolvedAt)}` : ""}`;
  }
  if (approval.state === "rejected") {
    return `${actor} requested changes${approval.feedback ? `: ${approval.feedback}` : ""}`;
  }
  return `${actor} ${approval.state || "pending"}`;
}

function TaskDetailList({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <div className="coding-task-detail-sublist">
      <strong>{title}</strong>
      <ul>
        {items.map((item, index) => (
          <li key={`${title}-${index}-${item}`}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

function TaskArtifactReviewList({
  reviews,
  approvals,
}: {
  reviews: GovernanceSummary["reviews"];
  approvals: GovernanceSummary["approvals"];
}) {
  if (!reviews.length && !approvals.length) {
    return <p className="coding-empty">No review or acceptance records yet.</p>;
  }
  return (
    <div className="coding-task-review-block">
      {reviews.length ? (
        <>
          <strong>Reviews</strong>
          <ul>
            {reviews.map((review) => (
              <li key={review.reviewId}>
                {review.reviewerMemberId}: {review.verdict}
                {review.findings.length
                  ? ` (${review.findings.filter((finding) => finding.blocking).length} blocking findings)`
                  : ""}
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {approvals.length ? (
        <>
          <strong>Acceptance</strong>
          <ul>
            {approvals.map((approval) => (
              <li key={approval.approvalId}>{approvalText(approval)}</li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}

const RUN_TARGET_PRIORITY = [
  "index.html",
  "run.command",
  "start.command",
  "run.sh",
  "start.sh",
  "run.cmd",
  "start.cmd",
  "run.bat",
  "start.bat",
  "run.ps1",
  "start.ps1",
  "main.py",
  "app.py",
  "cli.py",
  "__main__.py",
];

function normalizeRunTargetPath(path: string): string | null {
  let cleaned = path.replace(/\\/g, "/");
  while (cleaned.startsWith("./")) cleaned = cleaned.slice(2);
  if (
    !cleaned ||
    cleaned.includes("\0") ||
    cleaned.startsWith("/") ||
    cleaned.startsWith("~") ||
    /^[A-Za-z]:/.test(cleaned)
  ) {
    return null;
  }
  const parts = cleaned.split("/");
  if (parts.some((part) => !part || part === "." || part === "..")) return null;
  return cleaned;
}

function fileName(path: string): string {
  return path.replace(/\\/g, "/").split("/").pop()?.toLowerCase() ?? "";
}

function findRunTarget(artifacts: CodingArtifact[]): CodingArtifact | null {
  const byName = new Map<string, CodingArtifact>();
  for (const artifact of artifacts) {
    if (!artifact.onMaster) continue;
    const safePath = normalizeRunTargetPath(artifact.path);
    if (!safePath) continue;
    const name = fileName(safePath);
    if (!byName.has(name)) byName.set(name, artifact);
  }
  for (const name of RUN_TARGET_PRIORITY) {
    const artifact = byName.get(name);
    if (artifact) return artifact;
  }
  for (const artifact of artifacts) {
    if (!artifact.onMaster) continue;
    const safePath = normalizeRunTargetPath(artifact.path);
    if (safePath && /^scripts\/.+\.(sh|cmd|bat|ps1|command)$/i.test(safePath)) {
      return artifact;
    }
  }
  return null;
}

function joinLocalPath(root: string, rel: string): string | null {
  if (!root || !rel) return "";
  const cleanRel = normalizeRunTargetPath(rel);
  if (!cleanRel) return null;
  if (/[\\/]$/.test(root)) return `${root}${cleanRel}`;
  return `${root}/${cleanRel}`;
}

function runTargetButtonLabel(artifact: CodingArtifact | null): string {
  if (!artifact) return "Run target unavailable";
  const name = fileName(artifact.path);
  if (name === "index.html") return "Run project";
  if (/\.(command|cmd|bat|ps1|sh)$/i.test(name)) return "Open run script";
  return "Open entry point";
}

export interface CodingProjectViewProps {
  project: CodingProject;
  tasks: CodingTask[];
  decisions: CodingDecision[];
  artifacts: CodingArtifact[];
  toolEvents: CodingToolEvent[];
  prs?: CodingPr[];
  turns?: CodingTurn[];
  governance?: GovernanceSummary | null;
  memberNameById?: Record<string, string>;
  onDownloadRunLog?: () => void;
  testCommands?: Record<string, TestCommand>;
  testRuns?: TestRun[];
  requireSandbox?: boolean;
  onSaveTestCommands?: (
    commands: Record<string, { argv: string[]; cwd?: string; timeoutSeconds?: number }>,
  ) => void;
  onToggleRequireSandbox?: (value: boolean) => void;
  /** F105: called after an in-app file save so the container can refresh artifacts + decisions. */
  onFileSaved?: () => void;
  onAddTask?: (title: string, role: string) => void;
  onInterject?: (message: string) => CodingInterjection | void | Promise<CodingInterjection | void>;
  onReviewMergeBack?: () => void;
  running?: boolean;
  runStatus?: RunStatus | null;
  /**
   * F121 Part A: the derived run-control phase (optimistic intent + polled
   * state). Drives the Start/Stop button + status region. Defaults to a value
   * inferred from `running` when omitted (back-compat for existing callers).
   */
  runPhase?: RunPhase;
  /**
   * F121 Part A: the live governance headline shown next to the working
   * affordance while the team runs (so "Working" isn't a static label).
   */
  workingHeadline?: string;
  onStartRun?: () => boolean | void;
  onResumeRun?: () => void;
  onCancelRun?: () => boolean | void;
  delivery?: Delivery | null;
  onOpenProjectPath?: (path: string) => void;
  onOpenRunTarget?: (path: string) => void;
  governanceSlot?: ReactNode;
  /** F101: the project's runtime run/preview panel (container-wired). */
  runtimeSlot?: ReactNode;
  /** F088-10: the project's grounding/corpus-binding panel (container-wired). */
  groundingSlot?: ReactNode;
  /** F102: the project's publish/handoff panel (container-wired). */
  publishSlot?: ReactNode;
  /** The human-readable Team Log panel (container-wired). */
  teamLogSlot?: ReactNode;
  /** F143: the per-project Token usage panel (container-wired, self-fetching). */
  tokenUsageSlot?: ReactNode;
  /** F135: onboarding (North Star inference + Work Request) for imported projects. */
  onboardingSlot?: ReactNode;
}

export default function CodingProjectView({
  project,
  tasks,
  decisions,
  artifacts,
  toolEvents,
  prs = [],
  turns = [],
  governance = null,
  memberNameById = {},
  onDownloadRunLog,
  testCommands = {},
  testRuns = [],
  requireSandbox = false,
  onSaveTestCommands,
  onToggleRequireSandbox,
  onFileSaved,
  onAddTask,
  onInterject,
  onReviewMergeBack,
  running = false,
  runStatus,
  runPhase,
  workingHeadline = "",
  onStartRun,
  onResumeRun,
  onCancelRun,
  delivery = null,
  onOpenProjectPath,
  onOpenRunTarget,
  governanceSlot,
  runtimeSlot,
  groundingSlot,
  publishSlot,
  teamLogSlot,
  tokenUsageSlot,
  onboardingSlot,
}: CodingProjectViewProps) {
  const [pmReply, setPmReply] = useState<CodingPmReply | null>(null);
  const [sendingMessage, setSendingMessage] = useState(false);
  // F141 WS-J — synchronous PM chat.
  const [pmChat, setPmChat] = useState<PmChatTurn[]>([]);
  const pmChatLogRef = useRef<HTMLUListElement>(null);
  const [pmAskInput, setPmAskInput] = useState("");
  const [askingPm, setAskingPm] = useState(false);
  const [pmAskError, setPmAskError] = useState<string | null>(null);
  const [newTask, setNewTask] = useState("");
  const [tcId, setTcId] = useState("");
  const [tcArgv, setTcArgv] = useState("");
  const [selectedArtifactPath, setSelectedArtifactPath] = useState<string | null>(null);
  const [file, setFile] = useState<CodingFile | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  // F105: bumping this re-runs the file-load effect after an in-app save so the
  // editor re-seeds with the new committed content + sha.
  const [fileReloadKey, setFileReloadKey] = useState(0);
  const [selectedPrId, setSelectedPrId] = useState<string | null>(null);
  const [prQuery, setPrQuery] = useState("");
  const [prStatus, setPrStatus] = useState("all");
  const [prSort, setPrSort] = useState<"newest" | "oldest">("newest");
  const [buttonIntent, setButtonIntent] = useState<"none" | "starting" | "stopping">("none");
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const latestTestRun = testRuns.length > 0 ? testRuns[testRuns.length - 1] : null;
  const tasksById = useMemo(() => new Map(tasks.map((task) => [task.taskId, task])), [tasks]);
  const selectedTask = selectedTaskId ? tasksById.get(selectedTaskId) ?? null : null;
  useEffect(() => {
    if (selectedTaskId && !tasksById.has(selectedTaskId)) {
      setSelectedTaskId(null);
    }
  }, [selectedTaskId, tasksById]);

  // F141 WS-J — load the PM chat thread for this project. Reset first so a
  // project switch never shows the previous project's conversation (the view is
  // not remounted between projects). Then seed only when the thread is still
  // empty, so a late-resolving load can't clobber an optimistic turn the user
  // just sent in THIS project.
  useEffect(() => {
    let cancelled = false;
    setPmChat([]);
    setPmAskError(null);
    getPmChat(project.id)
      .then((thread) => {
        if (!cancelled && thread.length > 0) {
          setPmChat((prev) => (prev.length === 0 ? thread : prev));
        }
      })
      .catch(() => {
        /* leave the thread as-is */
      });
    return () => {
      cancelled = true;
    };
  }, [project.id]);

  // Keep "Contact the PM" pinned to the newest turn: scroll the log to the bottom
  // whenever the conversation changes (a message sent adds an optimistic user turn;
  // a reply appends the PM turn) or the pending "Asking the PM…" state toggles, and
  // on initial thread load. Guarded — the log only exists once there's a turn.
  useEffect(() => {
    const el = pmChatLogRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [pmChat, askingPm]);

  // Same "Contact the PM" box, directive action: an authoritative interjection
  // that steers the running team (vs. askPm, an informational question).
  const sendDirective = async () => {
    const msg = pmAskInput.trim();
    if (!msg || sendingMessage) return;
    setSendingMessage(true);
    try {
      const sent = await onInterject?.(msg);
      setPmReply(sent?.pmReply ?? null);
      setPmAskInput("");
    } catch {
      setPmReply(null);
    } finally {
      setSendingMessage(false);
    }
  };

  const askPm = async () => {
    const msg = pmAskInput.trim();
    if (!msg || askingPm) return;
    setAskingPm(true);
    setPmAskError(null);
    // optimistic: show the user's turn immediately
    setPmChat((prev) => [...prev, { role: "user", message: msg, at: "" }]);
    setPmAskInput("");
    try {
      const res = await pmAsk(project.id, msg);
      setPmChat((prev) => [
        ...prev,
        { role: res.reply.role, message: res.reply.message, at: res.reply.at },
      ]);
      if (!res.answered && res.error) setPmAskError(res.reply.message);
    } catch (err) {
      // The request itself failed — roll back the optimistic user turn and
      // restore the input so the user can retry without retyping.
      setPmChat((prev) => prev.slice(0, -1));
      setPmAskInput(msg);
      setPmAskError(err instanceof Error ? err.message : String(err));
    } finally {
      setAskingPm(false);
    }
  };
  const governanceArtifactsById = useMemo(() => {
    const out = new Map<string, GovernanceArtifact>();
    for (const artifact of governance?.artifacts ?? []) {
      out.set(artifact.artifactId, artifact);
    }
    return out;
  }, [governance]);
  const governanceReviewsByArtifact = useMemo(() => {
    const out = new Map<string, GovernanceSummary["reviews"]>();
    for (const review of governance?.reviews ?? []) {
      const existing = out.get(review.artifactId) ?? [];
      existing.push(review);
      out.set(review.artifactId, existing);
    }
    return out;
  }, [governance]);
  const governanceApprovalsByArtifact = useMemo(() => {
    const out = new Map<string, GovernanceSummary["approvals"]>();
    for (const approval of governance?.approvals ?? []) {
      const existing = out.get(approval.artifactId) ?? [];
      existing.push(approval);
      out.set(approval.artifactId, existing);
    }
    return out;
  }, [governance]);
  const governancePlanSlicesById = useMemo(() => {
    const out = new Map<string, GovernanceSummary["planSlices"][number]>();
    for (const slice of governance?.planSlices ?? []) {
      out.set(slice.sliceId, slice);
    }
    return out;
  }, [governance]);
  const latestPrByTask = useMemo(() => {
    const grouped = new Map<string, CodingPr[]>();
    for (const pr of prs) {
      const existing = grouped.get(pr.taskId) ?? [];
      existing.push(pr);
      grouped.set(pr.taskId, existing);
    }
    const out = new Map<string, CodingPr>();
    for (const [taskId, taskPrs] of grouped) {
      const pr = newestPr(taskPrs);
      if (pr) out.set(taskId, pr);
    }
    return out;
  }, [prs]);
  const latestTestRunByTask = useMemo(() => {
    const out = new Map<string, TestRun>();
    for (const run of testRuns) {
      const current = out.get(run.taskId);
      if (!current || parseTime(run.at) >= parseTime(current.at)) {
        out.set(run.taskId, run);
      }
    }
    return out;
  }, [testRuns]);
  const taskBadges = useMemo(() => {
    function relatedPrForTask(task: CodingTask): CodingPr | null {
      const seen = new Set<string>();
      const queue = [task.taskId, ...task.dependsOn];
      while (queue.length) {
        const taskId = queue.shift() as string;
        if (seen.has(taskId)) continue;
        seen.add(taskId);
        const pr = latestPrByTask.get(taskId);
        if (pr) return pr;
        const depTask = tasksById.get(taskId);
        if (depTask) queue.push(...depTask.dependsOn);
      }
      return null;
    }

    const out = new Map<string, TaskBadge[]>();
    for (const task of tasks) {
      const pr = relatedPrForTask(task);
      const badges: TaskBadge[] = [];
      if (pr) {
        badges.push(prStatusBadge(pr));
        const reviewed = reviewBadge(pr);
        // F091: a superseded PR is terminal — don't show a stale "review changes".
        if (reviewed && pr.status !== "changes_requested" && pr.status !== "superseded")
          badges.push(reviewed);
      }
      const tested = testBadge(pr, latestTestRunByTask.get(task.taskId) ?? null);
      if (tested) badges.push(tested);
      out.set(task.taskId, badges);
    }
    return out;
  }, [latestPrByTask, latestTestRunByTask, tasks, tasksById]);
  // F121 Part A: the run-control phase. Prefer the container-supplied
  // `runPhase` (optimistic intent + poll); fall back to a state-only derivation
  // so callers that don't pass it (and tests) still get a sensible phase.
  const basePhase: RunPhase =
    runPhase ?? deriveRunPhase({ intent: "none", running, runStatus });
  const phase: RunPhase =
    buttonIntent === "starting" && basePhase === "idle"
      ? "starting"
      : buttonIntent === "stopping" && basePhase === "working"
        ? "stopping"
        : basePhase;
  const isStarting = phase === "starting";
  const isStopping = phase === "stopping";
  const isWorking = phase === "working";
  // The status region label per phase. While working, append the live
  // governance headline (active stage) so it isn't a static "Team working…".
  const statusLabel = isStarting
    ? "Starting workers…"
    : isStopping
      ? "Stopping…"
      : isWorking
        ? workingHeadline
          ? `Working — ${workingHeadline}`
          : "Working…"
        : runStatus?.canResume
          ? "Interrupted - ready to resume"
          : "Idle";
  const selectedArtifact = artifacts.find((a) => a.path === selectedArtifactPath) ?? null;
  const selectedPr = prs.find((pr) => pr.prId === selectedPrId) ?? null;
  const selectedSpecArtifact = selectedTask?.sourceSpecArtifactId
    ? governanceArtifactsById.get(selectedTask.sourceSpecArtifactId) ?? null
    : null;
  const selectedPlanArtifact = selectedTask?.sourcePlanArtifactId
    ? governanceArtifactsById.get(selectedTask.sourcePlanArtifactId) ?? null
    : null;
  const selectedPlanSlice = selectedTask?.sourceSliceId
    ? governancePlanSlicesById.get(selectedTask.sourceSliceId) ?? null
    : null;
  const selectedTaskPrs = selectedTask
    ? prs
        .filter((pr) => pr.taskId === selectedTask.taskId)
        .slice()
        .sort((a, b) => parseTime(b.updatedAt || b.createdAt) - parseTime(a.updatedAt || a.createdAt))
    : [];
  const selectedTaskTestRun = selectedTask
    ? latestTestRunByTask.get(selectedTask.taskId) ?? null
    : null;
  const selectedTaskTurns = selectedTask
    ? turns.filter((turn) => turn.taskId === selectedTask.taskId)
    : [];
  const selectedTaskToolEvents = selectedTask
    ? toolEvents.filter((event) => event.taskId === selectedTask.taskId)
    : [];
  // F127: reassignment notes — why this task changed hands (a member kept
  // producing unusable turns and the task was routed to a stronger one).
  const selectedTaskReassignments = selectedTask
    ? decisions.filter(
        (d) =>
          d.choice === "task_reassigned" &&
          d.relatedTaskIds.includes(selectedTask.taskId),
      )
    : [];
  // F093: completion headline.
  const summaryBadge = summaryOutcome(running, runStatus, project.status);
  const mergedCount = prs.filter((p) => p.status === "merged").length;
  const projectOpenPath = delivery?.deliveredTo || project.repoPath || "";
  const runTargetArtifact = useMemo(() => findRunTarget(artifacts), [artifacts]);
  const runTargetPath = projectOpenPath && runTargetArtifact
    ? (joinLocalPath(projectOpenPath, runTargetArtifact.path) ?? "")
    : "";
  const runTargetLabel = runTargetPath
    ? runTargetButtonLabel(runTargetArtifact)
    : "Run target unavailable";
  const prBranchById = (id: string | null): string =>
    (id && prs.find((p) => p.prId === id)?.branch) || id || "—";
  const prStatusOptions = useMemo(() => {
    const statuses = new Set(PR_STATUS_OPTIONS);
    for (const pr of prs) {
      if (pr.status) statuses.add(pr.status);
    }
    return Array.from(statuses);
  }, [prs]);
  const visiblePrs = useMemo(() => {
    const query = prQuery.trim().toLowerCase();
    return [...prs]
      .filter((pr) => prStatus === "all" || pr.status === prStatus)
      .filter((pr) => {
        if (!query) return true;
        return [pr.prId, pr.taskId, pr.branch, pr.status].some((value) =>
          value.toLowerCase().includes(query),
        );
      })
      .sort((a, b) => {
        const delta = parseTime(a.createdAt) - parseTime(b.createdAt);
        return prSort === "oldest" ? delta : -delta;
      });
  }, [prQuery, prSort, prStatus, prs]);

  useEffect(() => {
    if (buttonIntent === "none") return undefined;
    if (
      (buttonIntent === "starting" && basePhase !== "idle") ||
      (buttonIntent === "stopping" && basePhase !== "working")
    ) {
      setButtonIntent("none");
      return undefined;
    }
    const id = window.setTimeout(
      () => setButtonIntent("none"),
      RUN_BUTTON_INTENT_BRIDGE_MS,
    );
    return () => window.clearTimeout(id);
  }, [basePhase, buttonIntent]);

  const startRun = () => {
    const accepted = onStartRun?.();
    if (accepted !== false) setButtonIntent("starting");
  };

  const stopRun = () => {
    const accepted = onCancelRun?.();
    if (accepted !== false) setButtonIntent("stopping");
  };

  useEffect(() => {
    let cancelled = false;
    if (!selectedArtifact) {
      setFile(null);
      setFileLoading(false);
      setFileError(null);
      return () => {
        cancelled = true;
      };
    }
    if (!selectedArtifact.onMaster) {
      setFile({
        path: selectedArtifact.path,
        content: null,
        truncated: false,
        encoding: "utf-8",
        bytes: 0,
        onMaster: false,
      });
      setFileLoading(false);
      setFileError(null);
      return () => {
        cancelled = true;
      };
    }
    setFile(null);
    setFileLoading(true);
    setFileError(null);
    void getFile(project.id, selectedArtifact.path)
      .then((loaded) => {
        if (!cancelled) setFile(loaded);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setFileError(err instanceof Error ? err.message : "Could not load file.");
        }
      })
      .finally(() => {
        if (!cancelled) setFileLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [project.id, selectedArtifact, fileReloadKey]);

  return (
    <section className="coding-view" aria-label={`Coding project ${project.id}`}>
      <section className="coding-controls" aria-label="Run controls">
        {isStopping ? (
          // Stop already requested — keep the button visible but disabled while
          // the run drains so the click clearly registered.
          <button type="button" className="coding-btn" disabled aria-busy="true">
            <span className="coding-spinner" aria-hidden="true" /> Stopping…
          </button>
        ) : isWorking ? (
          <button type="button" className="coding-btn" onClick={stopRun}>
            Stop run
          </button>
        ) : isStarting ? (
          <button type="button" className="coding-btn" disabled aria-busy="true">
            <span className="coding-spinner" aria-hidden="true" /> Starting…
          </button>
        ) : runStatus?.canResume ? (
          <button type="button" className="coding-btn" onClick={() => onResumeRun?.()}>
            Resume run
          </button>
        ) : (
          <button type="button" className="coding-btn" onClick={startRun}>
            Start run
          </button>
        )}
        <span
          className={
            isWorking ? "coding-run-state coding-run-state-working" : "coding-run-state"
          }
          aria-live="polite"
        >
          {isWorking ? <span className="coding-pulse" aria-hidden="true" /> : null}
          {statusLabel}
        </span>
        {!running && !isStarting && !isStopping && runStatus?.recoverable ? (
          <span className="coding-recovery-note">
            In-flight tasks were returned to the backlog after restart.
          </span>
        ) : null}
      </section>

      <details className="coding-summary" aria-label="Project summary">
        <summary className="coding-summary-head">
          <span className="coding-summary-title">
            <span className="coding-summary-chevron" aria-hidden="true" />
            <h3>Project summary</h3>
          </span>
          <span className="coding-summary-head-meta">
            <span className="coding-summary-merged">
              {mergedCount} merged PR{mergedCount === 1 ? "" : "s"}
            </span>
            <span className={`coding-summary-badge coding-summary-${summaryBadge.tone}`}>
              {summaryBadge.label}
            </span>
          </span>
        </summary>
        <div className="coding-summary-body">
          <div className="coding-summary-actions" aria-label="Project actions">
            <button
              type="button"
              className="coding-btn coding-btn-small"
              disabled={!projectOpenPath}
              onClick={() => {
                if (projectOpenPath) onOpenProjectPath?.(projectOpenPath);
              }}
            >
              Open project
            </button>
            <button
              type="button"
              className="coding-btn coding-btn-small"
              disabled={!runTargetPath}
              onClick={() => {
                if (runTargetPath) onOpenRunTarget?.(runTargetPath);
              }}
            >
              {runTargetLabel}
            </button>
          </div>
          {projectOpenPath ? (
            <p className="coding-summary-path">
              <span>Location</span>
              <code>{projectOpenPath}</code>
            </p>
          ) : null}
          {delivery?.runHint ? (
            <p className="coding-summary-runhint">
              <span>Run hint</span>
              <code>{delivery.runHint}</code>
            </p>
          ) : !runTargetPath ? (
            <p className="coding-summary-empty">Run target: none yet.</p>
          ) : null}
          {project.completionSummary ? (
            <p className="coding-summary-text">{project.completionSummary}</p>
          ) : null}
          <div className="coding-summary-tests" role="group" aria-label="Test results">
            <h4>Test results</h4>
            {testRuns.length === 0 ? (
              <p className="coding-summary-empty">
                No automated tests were configured - completion is gated on review
                approval.
              </p>
            ) : (
              <ul>
                {testRuns.map((run) => (
                  <li key={run.testRunId}>
                    <span className={run.passed ? "coding-tc-pass" : "coding-tc-fail"}>
                      {run.passed ? "passed" : "failed"}
                    </span>{" "}
                    {run.commandIds.length ? run.commandIds.join(", ") : "—"}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </details>

      {onboardingSlot}
      {teamLogSlot}
      {governanceSlot}
      {runtimeSlot}

      <section className="coding-board" aria-label="Task backlog">
        {TASK_STATES.map(({ key, label }) => {
          const col = tasks.filter((t) => t.state === key);
          return (
            <div key={key} className="coding-col" role="group" aria-label={`${label} tasks`}>
              <h3>
                {label} <span className="coding-count">{col.length}</span>
              </h3>
              <ul>
                {col.map((t) => {
                  const assigneeName = t.assigneeMemberId
                    ? memberNameById[t.assigneeMemberId] || t.assigneeMemberId
                    : "";
                  return (
                    <li
                      key={t.taskId}
                      className={`coding-task coding-role-${t.role}${
                        selectedTaskId === t.taskId ? " is-selected" : ""
                      }`}
                    >
                      <button
                        type="button"
                        className="coding-task-card"
                        onClick={() => setSelectedTaskId(t.taskId)}
                        aria-expanded={selectedTaskId === t.taskId}
                        aria-controls={selectedTaskId === t.taskId ? "coding-task-detail" : undefined}
                        aria-label={`Open task details for ${t.title}`}
                      >
                        <span className="coding-task-meta">
                          <span className="coding-task-role">{t.role}</span>
                          {(taskBadges.get(t.taskId) ?? []).map((badge) => (
                            <span
                              key={`${t.taskId}-${badge.label}`}
                              className={`coding-task-badge coding-task-badge-${badge.tone}`}
                            >
                              {badge.label}
                            </span>
                          ))}
                          {/* F135: which model the PM bound to this task, and why. */}
                          {t.modelAssignment?.route_id ? (
                            <span
                              className={`coding-task-model coding-task-model-${
                                t.modelAssignment.source ?? "assigned"
                              }`}
                              title={t.modelAssignment.rationale || undefined}
                            >
                              {t.modelAssignment.route_id}
                              {t.modelAssignment.difficulty_tier
                                ? ` · ${t.modelAssignment.difficulty_tier}`
                                : ""}
                              {t.modelAssignment.source
                                ? ` · ${t.modelAssignment.source}`
                                : ""}
                              {t.modelAssignment.escalation_count
                                ? ` · esc ${t.modelAssignment.escalation_count}`
                                : ""}
                            </span>
                          ) : null}
                        </span>
                        <span className="coding-task-title">{t.title}</span>
                        {t.assigneeMemberId ? (
                          <span
                            className="coding-task-assignee"
                            title={assigneeName === t.assigneeMemberId ? undefined : t.assigneeMemberId}
                          >
                            {assigneeName}
                          </span>
                        ) : null}
                        {t.reasonSummary ? (
                          <span className="coding-task-reason" title={t.detail || undefined}>
                            {t.prId ? `${t.prId} · ` : ""}
                            {t.reasonSummary}
                          </span>
                        ) : null}
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })}
      </section>

      {selectedTask ? (
        <section
          id="coding-task-detail"
          className="coding-task-detail"
          aria-label="Task detail"
        >
          <div className="coding-task-detail-head">
            <div>
              <span className="coding-task-role">{selectedTask.role}</span>
              <h3>{selectedTask.title}</h3>
              <p>
                {selectedTask.taskId} · {selectedTask.state}
                {selectedTask.assigneeMemberId
                  ? ` · ${memberNameById[selectedTask.assigneeMemberId] || selectedTask.assigneeMemberId}`
                  : ""}
              </p>
            </div>
            <button
              type="button"
              className="coding-btn coding-btn-small"
              onClick={() => setSelectedTaskId(null)}
            >
              Close
            </button>
          </div>

          {selectedTask.reasonSummary || selectedTask.detail ? (
            <div className="coding-task-reason-block">
              <h4>Why this was sent back</h4>
              {selectedTask.reasonSummary ? (
                <p className="coding-task-reason-summary">
                  {selectedTask.prId ? (
                    <span className="coding-task-reason-pr">{selectedTask.prId}</span>
                  ) : null}{" "}
                  {selectedTask.reasonSummary}
                </p>
              ) : null}
              {selectedTask.detail ? (
                <p className="coding-task-reason-detail">{selectedTask.detail}</p>
              ) : null}
            </div>
          ) : null}

          <div className="coding-task-detail-grid">
            <section>
              <h4>Spec</h4>
              {selectedSpecArtifact ? (
                <>
                  <p className="coding-task-detail-kicker">
                    {selectedSpecArtifact.title} · v{selectedSpecArtifact.version} ·{" "}
                    {selectedSpecArtifact.state}
                  </p>
                  {artifactBodyText(selectedSpecArtifact) ? (
                    <pre>{artifactBodyText(selectedSpecArtifact)}</pre>
                  ) : (
                    <p className="coding-empty">No spec body recorded.</p>
                  )}
                  <TaskArtifactReviewList
                    reviews={governanceReviewsByArtifact.get(selectedSpecArtifact.artifactId) ?? []}
                    approvals={governanceApprovalsByArtifact.get(selectedSpecArtifact.artifactId) ?? []}
                  />
                </>
              ) : (
                <p className="coding-empty">No source spec is linked to this task.</p>
              )}
            </section>

            <section>
              <h4>Plan</h4>
              {selectedPlanArtifact ? (
                <>
                  <p className="coding-task-detail-kicker">
                    {selectedPlanArtifact.title} · v{selectedPlanArtifact.version} ·{" "}
                    {selectedPlanArtifact.state}
                  </p>
                  {artifactBodyText(selectedPlanArtifact) ? (
                    <pre>{artifactBodyText(selectedPlanArtifact)}</pre>
                  ) : null}
                  <TaskArtifactReviewList
                    reviews={governanceReviewsByArtifact.get(selectedPlanArtifact.artifactId) ?? []}
                    approvals={governanceApprovalsByArtifact.get(selectedPlanArtifact.artifactId) ?? []}
                  />
                </>
              ) : null}
              {selectedPlanSlice ? (
                <div className="coding-task-plan-slice">
                  <p className="coding-task-detail-kicker">
                    Slice {selectedPlanSlice.sliceId}: {selectedPlanSlice.title}
                  </p>
                  {selectedPlanSlice.detail ? <p>{selectedPlanSlice.detail}</p> : null}
                  <TaskDetailList title="Acceptance" items={selectedPlanSlice.doneWhen} />
                  <TaskDetailList title="Tests" items={selectedPlanSlice.tests} />
                  <TaskDetailList title="Review focus" items={selectedPlanSlice.reviewFocus} />
                </div>
              ) : !selectedPlanArtifact ? (
                <p className="coding-empty">No source plan or plan slice is linked to this task.</p>
              ) : null}
            </section>

            <section>
              <h4>Implementation PR Review</h4>
              {selectedTaskPrs.length ? (
                <ul className="coding-task-detail-list">
                  {selectedTaskPrs.map((pr) => (
                    <li key={pr.prId}>
                      <strong>{pr.branch}</strong>
                      <span>{pr.status}</span>
                      <span>
                        review:{" "}
                        {pr.reviewerApproved == null
                          ? "pending"
                          : pr.reviewerApproved
                            ? "approved"
                            : "changes requested"}
                      </span>
                      <span>
                        tests:{" "}
                        {pr.testsPassed == null
                          ? selectedTaskTestRun
                            ? selectedTaskTestRun.passed
                              ? "passed"
                              : "failed"
                            : "pending"
                          : pr.testsPassed
                            ? "passed"
                            : "failed"}
                      </span>
                      {pr.conflicts.length ? <span>conflicts: {pr.conflicts.join(", ")}</span> : null}
                      {pr.supersededByPrId ? (
                        <span>superseded by {prBranchById(pr.supersededByPrId)}</span>
                      ) : null}
                      {pr.reviewFindings.length ? (
                        <ul className="coding-task-review-findings">
                          {pr.reviewFindings.map((f, i) => (
                            <li
                              key={`${pr.prId}-finding-${i}`}
                              className={f.blocking ? "is-blocking" : undefined}
                            >
                              <strong>
                                {f.blocking ? "⛔ " : ""}
                                {f.severity ? `[${f.severity}] ` : ""}
                                {f.title || "Finding"}
                                {f.path ? ` — ${f.path}` : ""}
                              </strong>
                              {f.body ? <span>{f.body}</span> : null}
                            </li>
                          ))}
                        </ul>
                      ) : pr.reviewerApproved === false ? (
                        <span className="coding-empty">
                          Changes requested (no detailed findings recorded).
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="coding-empty">No implementation PR is linked to this task yet.</p>
              )}
            </section>

            <section>
              <h4>Activity Evidence</h4>
              <dl className="coding-task-detail-facts">
                <div>
                  <dt>Dependencies</dt>
                  <dd>{selectedTask.dependsOn.length ? selectedTask.dependsOn.join(", ") : "none"}</dd>
                </div>
                <div>
                  <dt>Governance required</dt>
                  <dd>{selectedTask.governanceRequired ? "yes" : "no"}</dd>
                </div>
                <div>
                  <dt>Turns</dt>
                  <dd>{selectedTaskTurns.length}</dd>
                </div>
                <div>
                  <dt>Tool events</dt>
                  <dd>{selectedTaskToolEvents.length}</dd>
                </div>
              </dl>
              {selectedTaskReassignments.length ? (
                <ul className="coding-task-detail-list coding-task-reassignments">
                  {selectedTaskReassignments.map((d) => (
                    <li key={d.decisionId}>
                      <strong>↪ Reassigned</strong>
                      <span>{d.rationale}</span>
                    </li>
                  ))}
                </ul>
              ) : null}
              {selectedTaskTurns.length ? (
                <ul className="coding-task-detail-list">
                  {selectedTaskTurns.slice(-3).map((turn) => (
                    <li key={turn.turnId}>
                      <strong>{turn.role}</strong>
                      <span>{turn.outcome}</span>
                      {turn.reason ? <span>{turn.reason}</span> : null}
                    </li>
                  ))}
                </ul>
              ) : null}
            </section>
          </div>
        </section>
      ) : null}

      <form
        className="coding-addtask"
        onSubmit={(e) => {
          e.preventDefault();
          if (newTask.trim()) {
            onAddTask?.(newTask.trim(), "dev");
            setNewTask("");
          }
        }}
      >
        <input
          value={newTask}
          onChange={(e) => setNewTask(e.target.value)}
          placeholder="Add a task…"
          aria-label="New task title"
        />
        <button type="submit" className="coding-btn">
          Add task
        </button>
      </form>

      <section className="coding-pm-chat coding-contact-pm">
        <label className="coding-field-label" htmlFor="coding-pm-ask">
          Contact the PM
        </label>
        <span className="coding-field-hint">
          Ask a question for a quick answer (a conversation — it doesn’t change
          what the team builds), or send a directive to steer the work.
        </span>
        {pmChat.length > 0 ? (
          <ul
            className="coding-pm-chat-log"
            aria-label="PM conversation"
            ref={pmChatLogRef}
          >
            {pmChat.map((turn, i) => (
              <li
                key={i}
                className={`coding-pm-chat-turn coding-pm-chat-${turn.role === "user" ? "user" : "pm"}`}
              >
                <span className="coding-pm-chat-who">
                  {turn.role === "user" ? "You" : "PM"}
                </span>
                <p>{turn.message}</p>
              </li>
            ))}
          </ul>
        ) : null}
        <div className="coding-pm-chat-compose">
          <textarea
            id="coding-pm-ask"
            value={pmAskInput}
            onChange={(e) => setPmAskInput(e.target.value)}
            placeholder="Ask a question or send a directive…"
            rows={2}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                void askPm();
              }
            }}
          />
          <div className="coding-contact-actions">
            <button
              type="button"
              className="coding-btn coding-btn-primary"
              disabled={!pmAskInput.trim() || askingPm}
              onClick={() => void askPm()}
            >
              {askingPm ? "Asking the PM…" : "Ask a question"}
            </button>
            <button
              type="button"
              className="coding-btn"
              disabled={!pmAskInput.trim() || sendingMessage}
              onClick={() => void sendDirective()}
            >
              {sendingMessage ? "Sending…" : "Send directive"}
            </button>
          </div>
        </div>
        {pmAskError ? (
          <p className="coding-pm-chat-error" role="alert">
            {pmAskError}
          </p>
        ) : null}
        {pmReply ? (
          <div className="coding-pm-reply" role="status" aria-live="polite">
            <span className="coding-task-role">PM</span>
            <p>{pmReply.message}</p>
            {pmReply.progress ? (
              <span className="coding-pm-progress">
                {pmReply.progress.percent}% done - {pmReply.progress.done}/{pmReply.progress.total} tasks
              </span>
            ) : null}
          </div>
        ) : null}
      </section>

      {/* Project Grounding lives just below the Send message composer. */}
      {groundingSlot}
      {publishSlot}

      <div className="coding-lower">
        {/* Decisions moved into the Run Log panel below (converged). */}
        <details className="coding-panel coding-artifacts">
          <summary>
            <span>Files touched</span>
            <span className="coding-count">{artifacts.length}</span>
          </summary>
          <section aria-label="Artifacts">
            {artifacts.length === 0 ? (
              <p className="coding-empty">No files yet.</p>
            ) : (
              <ul>
                {artifacts.map((a) => (
                  <li key={a.path}>
                    <button
                      type="button"
                      className={`coding-link-btn${a.onMaster ? "" : " coding-link-btn-disabled"}`}
                      disabled={!a.onMaster}
                      onClick={() => {
                        if (a.onMaster) setSelectedArtifactPath(a.path);
                      }}
                    >
                      <code>{a.path}</code>
                    </button>
                    <span className="coding-art-status">{a.status}</span>
                    {!a.onMaster ? (
                      <span className="coding-file-note">not on master yet</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
            {selectedArtifact ? (
              <div className="coding-file-viewer" aria-label="File viewer">
                <div className="coding-file-viewer-head">
                  <strong>{selectedArtifact.path}</strong>
                  <button
                    type="button"
                    className="coding-btn coding-btn-small"
                    onClick={() => setSelectedArtifactPath(null)}
                  >
                    Close
                  </button>
                </div>
                <span className="coding-art-status">{selectedArtifact.status}</span>
                {selectedArtifact.summary ? (
                  <p className="coding-file-summary">{selectedArtifact.summary}</p>
                ) : null}
                {fileLoading ? <p role="status">Loading file...</p> : null}
                {fileError ? (
                  <p role="alert" className="coding-file-note coding-file-error">
                    Could not load file.
                  </p>
                ) : null}
                {!fileLoading && !fileError && file ? (
                  <>
                    {file.onMaster === false ? (
                      <p className="coding-file-note">not on master yet</p>
                    ) : file.encoding === "binary" ? (
                      <p className="coding-file-note">Binary file - not shown.</p>
                    ) : file.truncated ? (
                      // Truncated files stay read-only (no concurrency token for
                      // the full blob), per the F105 non-goals.
                      <>
                        <p className="coding-file-note">
                          Showing first 256 KiB of {file.bytes} bytes — read-only.
                        </p>
                        <div className="coding-file-actions">
                          <button
                            type="button"
                            className="coding-btn coding-btn-small"
                            disabled={!file.content}
                            onClick={() => {
                              if (file.content && navigator.clipboard) {
                                void navigator.clipboard.writeText(file.content);
                              }
                            }}
                          >
                            Copy
                          </button>
                        </div>
                        <pre className="coding-file-body" aria-label="File contents">
                          {file.content}
                        </pre>
                      </>
                    ) : (
                      // F105: editable in-app editor for eligible utf-8 text.
                      <FileEditor
                        projectId={project.id}
                        file={file}
                        running={running}
                        onSaved={() => {
                          // Re-fetch the file so the editor re-seeds with the new
                          // committed sha, and let the parent refresh artifacts
                          // + the decision log (the human_file_edit event).
                          setFileReloadKey((k) => k + 1);
                          onFileSaved?.();
                        }}
                      />
                    )}
                  </>
                ) : null}
              </div>
            ) : null}
          </section>
        </details>

        {/* Tool events moved into the Run Log panel below (converged). */}
      </div>

      {TEST_COMMANDS_ENABLED ? (
      <details className="coding-panel coding-testcommands" aria-label="Test Commands">
        <summary>
          <span>Test Commands</span>
          <span className="coding-count">{Object.keys(testCommands).length}</span>
        </summary>
        <section aria-label="Test command settings">
          <p className="coding-field-hint">
            The tester can only validate work by running these named commands; its
            verdict comes from the real exit code. Argv only (no shell).
          </p>
          <label className="coding-toggle">
            <input
              type="checkbox"
              checked={requireSandbox}
              onChange={(e) => onToggleRequireSandbox?.(e.target.checked)}
            />
            <span>Require an OS sandbox (fail closed if none is available)</span>
          </label>
          {Object.keys(testCommands).length === 0 ? (
            <p className="coding-empty">No test commands configured.</p>
          ) : (
            <ul className="coding-tc-list">
              {Object.entries(testCommands).map(([id, cmd]) => (
                <li key={id} className="coding-tc-item">
                  <code className="coding-tc-id">{id}</code>
                  <code className="coding-tc-argv">{cmd.argv.join(" ")}</code>
                  <button
                    type="button"
                    className="coding-btn coding-btn-small"
                    aria-label={`Remove test command ${id}`}
                    onClick={() => {
                      const next: Record<string, { argv: string[]; timeoutSeconds: number }> = {};
                      for (const [k, v] of Object.entries(testCommands)) {
                        if (k !== id) next[k] = { argv: v.argv, timeoutSeconds: v.timeoutSeconds };
                      }
                      onSaveTestCommands?.(next);
                    }}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}
          <form
            className="coding-tc-form"
            onSubmit={(e) => {
              e.preventDefault();
              const id = tcId.trim();
              const argv = tcArgv.trim().split(/\s+/).filter(Boolean);
              if (!id || argv.length === 0) return;
              const next: Record<string, { argv: string[]; timeoutSeconds: number }> = {};
              for (const [k, v] of Object.entries(testCommands)) {
                next[k] = { argv: v.argv, timeoutSeconds: v.timeoutSeconds };
              }
              next[id] = { argv, timeoutSeconds: 120 };
              onSaveTestCommands?.(next);
              setTcId("");
              setTcArgv("");
            }}
          >
            <input
              value={tcId}
              onChange={(e) => setTcId(e.target.value)}
              placeholder="id (e.g. unit)"
              aria-label="Test command id"
            />
            <input
              value={tcArgv}
              onChange={(e) => setTcArgv(e.target.value)}
              placeholder="argv (e.g. python -m pytest -q)"
              aria-label="Test command argv"
            />
            <button type="submit" className="coding-btn">
              Add test command
            </button>
          </form>
          {latestTestRun ? (
            <p
              className={`coding-tc-verdict coding-tc-${latestTestRun.passed ? "pass" : "fail"}`}
              aria-live="polite"
            >
              Latest test run: {latestTestRun.passed ? "passed" : "failed"}
              {latestTestRun.commandIds.length
                ? ` (${latestTestRun.commandIds.join(", ")})`
                : ""}
              {latestTestRun.sandbox ? ` — sandbox: ${latestTestRun.sandbox}` : ""}
            </p>
          ) : null}
        </section>
      </details>
      ) : null}

      <details className="coding-panel coding-prs">
        <summary>
          <span>Pull requests</span>
          <span className="coding-count">{prs.length}</span>
        </summary>
        <section aria-label="Pull requests">
          <p className="coding-field-hint">
            Each task is built on its own branch and integrated into master via a
            PR the PM merges - only after the reviewer approves AND the tests pass.
          </p>
          {prs.length === 0 ? (
            <p className="coding-empty">No pull requests yet.</p>
          ) : (
            <>
              <div className="coding-pr-controls">
                <input
                  value={prQuery}
                  onChange={(e) => setPrQuery(e.target.value)}
                  placeholder="Search pull requests"
                  aria-label="Search pull requests"
                />
                <select
                  value={prStatus}
                  onChange={(e) => setPrStatus(e.target.value)}
                  aria-label="Filter pull requests by status"
                >
                  <option value="all">All statuses</option>
                  {prStatusOptions.map((status) => (
                    <option key={status} value={status}>
                      {status}
                    </option>
                  ))}
                </select>
                <select
                  value={prSort}
                  onChange={(e) => setPrSort(e.target.value as "newest" | "oldest")}
                  aria-label="Sort pull requests"
                >
                  <option value="newest">Newest first</option>
                  <option value="oldest">Oldest first</option>
                </select>
              </div>
              {visiblePrs.length === 0 ? (
                <p className="coding-empty">No pull requests match.</p>
              ) : (
                <ul className="coding-pr-list">
                  {visiblePrs.map((pr) => (
                    <li key={pr.prId} className={`coding-pr coding-pr-${pr.status}`}>
                      <button
                        type="button"
                        className="coding-link-btn coding-pr-open"
                        onClick={() => setSelectedPrId(pr.prId)}
                      >
                        <code className="coding-pr-branch">{pr.branch}</code>
                      </button>
                      <span className="coding-pr-status">{pr.status}</span>
                      <span className="coding-pr-flag">{formatTime(pr.createdAt)}</span>
                      <span className="coding-pr-flag">
                        review: {pr.reviewerApproved == null ? "-" : pr.reviewerApproved ? "passed" : "changes"}
                      </span>
                      <span className="coding-pr-flag">
                        tests: {pr.testsPassed == null ? "-" : pr.testsPassed ? "passed" : "failed"}
                      </span>
                      {pr.conflicts.length ? (
                        <span className="coding-pr-conflict">conflicts: {pr.conflicts.join(", ")}</span>
                      ) : null}
                      {pr.supersededByPrId ? (
                        <span className="coding-pr-flag">
                          superseded by {prBranchById(pr.supersededByPrId)}
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
              {selectedPr ? (
                <div className="coding-pr-detail" aria-label="Pull request detail">
                  <div className="coding-file-viewer-head">
                    <strong>{selectedPr.branch}</strong>
                    <button
                      type="button"
                      className="coding-btn coding-btn-small"
                      onClick={() => setSelectedPrId(null)}
                    >
                      Close
                    </button>
                  </div>
                  <dl>
                    <div>
                      <dt>ID</dt>
                      <dd>{selectedPr.prId}</dd>
                    </div>
                    <div>
                      <dt>Task</dt>
                      <dd>{selectedPr.taskId}</dd>
                    </div>
                    <div>
                      <dt>Status</dt>
                      <dd>{selectedPr.status}</dd>
                    </div>
                    {selectedPr.supersededByPrId ? (
                      <div>
                        <dt>Superseded by</dt>
                        <dd>{prBranchById(selectedPr.supersededByPrId)}</dd>
                      </div>
                    ) : null}
                    <div>
                      <dt>Created</dt>
                      <dd>{formatTime(selectedPr.createdAt)}</dd>
                    </div>
                    <div>
                      <dt>Updated</dt>
                      <dd>{formatTime(selectedPr.updatedAt)}</dd>
                    </div>
                    <div>
                      <dt>Review</dt>
                      <dd>{selectedPr.reviewerApproved == null ? "pending" : selectedPr.reviewerApproved ? "approved" : "changes requested"}</dd>
                    </div>
                    <div>
                      <dt>Tests</dt>
                      <dd>{selectedPr.testsPassed == null ? "pending" : selectedPr.testsPassed ? "passed" : "failed"}</dd>
                    </div>
                    {selectedPr.conflicts.length ? (
                      <div>
                        <dt>Conflicts</dt>
                        <dd>{selectedPr.conflicts.join(", ")}</dd>
                      </div>
                    ) : null}
                  </dl>
                </div>
              ) : null}
            </>
          )}
        </section>
      </details>

      {tokenUsageSlot}

      <details className="coding-panel coding-runlog">
        <summary>
          <span>Run log</span>
          <span className="coding-count">{turns.length} turns</span>
        </summary>
        <section aria-label="Run log">
          <div className="coding-runlog-head">
          <button
            type="button"
            className="coding-btn coding-btn-small"
            onClick={() => onDownloadRunLog?.()}
          >
            Download full transcript
          </button>
          </div>
          <p className="coding-field-hint">
            Every member turn, verbatim: the exact prompt each member received and
            its raw response, plus the outcome - so you can verify each member did
            its job.
          </p>
          {turns.length === 0 ? (
            <p className="coding-empty">No turns yet.</p>
          ) : (
            <ol className="coding-turns">
              {turns.slice().reverse().map((t) => (
                <li key={t.turnId} className={`coding-turn coding-role-${t.role}`}>
                  <details>
                    <summary>
                      <span className="coding-task-role">{t.role}</span>
                      <span className="coding-turn-outcome">{t.outcome}</span>
                      {t.modelAssignment?.route_id ? (
                        <span className="coding-turn-model">{t.modelAssignment.route_id}</span>
                      ) : null}
                      {t.reason ? <span className="coding-turn-reason">{t.reason}</span> : null}
                      <TurnTokens turn={t} />
                      <span className="coding-turn-dur">{t.durationMs}ms</span>
                    </summary>
                    <div className="coding-turn-body">
                      {t.modelAssignment ? (
                        <p className="coding-field-hint">
                          Model {t.modelAssignment.route_id} · {t.modelAssignment.difficulty_tier ?? "mid"}
                          {t.modelAssignment.escalation_count
                            ? ` · escalation ${t.modelAssignment.escalation_count}`
                            : ""}
                          {t.modelAssignment.rationale ? ` — ${t.modelAssignment.rationale}` : ""}
                        </p>
                      ) : null}
                      <h4>Prompt</h4>
                      <pre className="coding-turn-text">{t.prompt}</pre>
                      <h4>Response</h4>
                      <pre className="coding-turn-text">{t.response}</pre>
                      <TurnContextReport projectId={project.id} turn={t} />
                    </div>
                  </details>
                </li>
              ))}
            </ol>
          )}

          <details className="coding-runlog-sub coding-decisions">
            <summary>
              <span>Decisions</span>
              <span className="coding-count">{decisions.length}</span>
            </summary>
            <section aria-label="Decision log">
              {decisions.length === 0 ? (
                <p className="coding-empty">No decisions yet.</p>
              ) : (
                <ul>
                  {decisions.slice().reverse().map((d) => (
                    <li key={d.decisionId}>
                      <strong>{d.title}</strong>: {d.choice} - {d.rationale}
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </details>

          <details className="coding-runlog-sub coding-tool-events">
            <summary>
              <span>Tool events</span>
              <span className="coding-count">{toolEvents.length}</span>
            </summary>
            <section aria-label="Tool events">
              {toolEvents.length === 0 ? (
                <p className="coding-empty">No tool calls yet.</p>
              ) : (
                <ul>
                  {toolEvents.map((e) => (
                    <li key={e.eventId} className={`coding-tool-event coding-tool-${e.status}`}>
                      <span className="coding-task-role">{e.tool}</span>
                      <span className="coding-art-status">{e.status}</span>
                      {e.path ? <code>{e.path}</code> : null}
                      {e.error ? <span className="coding-tool-error">{e.error}</span> : null}
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </details>
        </section>
      </details>

      <section className="coding-mergeback" aria-label="Merge">
        <h3>Merge</h3>
        <p>
          Review the team&apos;s work before accepting. Nothing touches your tree
          until you review the diff and explicitly accept.
        </p>
        <button
          type="button"
          className="coding-btn"
          onClick={() => onReviewMergeBack?.()}
        >
          Review diff…
        </button>
      </section>
    </section>
  );
}
