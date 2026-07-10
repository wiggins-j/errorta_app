// F125 — Building Progress disclosure. Projects the current governance phase
// into a collapsed summary, with the full stage stepper available on open.
// Artifact-backed completed / needs-action steps can open the read-only drawer.
import { useEffect, useRef, useState } from "react";
import type {
  GovernanceStage,
  GovernanceStatus,
  GovernanceStepState,
} from "../../lib/api/coding";

export interface GovernanceStatusPanelProps {
  status: GovernanceStatus | null;
  /** Open the artifact drawer for a specific reviewable stage. */
  onOpenStage?: (stage: GovernanceStage) => void;
  /** F100-02: open the read-only Brainstorm viewer. */
  onOpenBrainstorm?: () => void;
  /** F100-02: open the Brainstorm viewer with the comment box focused. */
  onCommentBrainstorm?: () => void;
}

// The stepper stages, in order, with their user-facing labels.
const STEP_LABELS: Record<string, string> = {
  brainstorm: "Brainstorm",
  spec: "Spec",
  plan: "Plan",
  build: "Build",
  done: "Done",
};
const STEP_ORDER = ["brainstorm", "spec", "plan", "build", "done"] as const;
const ARTIFACT_STAGES = new Set<GovernanceStage>(["brainstorm", "spec", "plan"]);
const CLICKABLE_STEP_STATES = new Set<GovernanceStepState>([
  "approved",
  "changes_requested",
  "stuck",
]);

// Short human label for a status pill (kept here so no raw backend strings leak).
const STATUS_LABEL: Record<string, string> = {
  drafting: "Drafting",
  under_review: "Under review",
  changes_requested: "Changes requested",
  approved: "Approved",
  building: "Building",
  stuck: "Needs you",
};

function pillClass(status: string | null): string {
  switch (status) {
    case "drafting":
      return "coding-gov-pill coding-gov-pill-drafting";
    case "under_review":
      return "coding-gov-pill coding-gov-pill-under-review";
    case "changes_requested":
      return "coding-gov-pill coding-gov-pill-changes-requested";
    case "approved":
      return "coding-gov-pill coding-gov-pill-approved";
    case "building":
      return "coding-gov-pill coding-gov-pill-building";
    case "stuck":
      return "coding-gov-pill coding-gov-pill-stuck";
    default:
      return "coding-gov-pill";
  }
}

function stepClass(state: GovernanceStepState, isCurrent: boolean): string {
  const parts = ["coding-gov-step", `coding-gov-step-${state}`];
  if (isCurrent) parts.push("coding-gov-step-current");
  return parts.join(" ");
}

// A short, screen-reader-friendly description of where a step is.
function stepStateLabel(state: GovernanceStepState): string {
  switch (state) {
    case "approved":
      return "approved";
    case "under_review":
      return "under review";
    case "changes_requested":
      return "changes requested";
    case "drafting":
      return "in progress";
    case "building":
      return "building";
    case "stuck":
      return "needs you";
    default:
      return "pending";
  }
}

function canOpenStage(stage: GovernanceStage, state: GovernanceStepState): boolean {
  return ARTIFACT_STAGES.has(stage) && CLICKABLE_STEP_STATES.has(state);
}

/**
 * Renders nothing when governance is off or there is no live status — the strip
 * only appears once a governed run has a stage/status to show.
 */
export default function GovernanceStatusPanel({
  status,
  onOpenStage,
  onOpenBrainstorm,
  onCommentBrainstorm,
}: GovernanceStatusPanelProps) {
  // F125: collapsed by default, BUT auto-expanded when the run is stuck / needs
  // the human — otherwise the blocking Read/Comment/Accept call-to-action is
  // buried inside the disclosure exactly when it must be acted on. The user can
  // still collapse it manually; we only force-open on the transition INTO a stuck
  // state, not on every poll. Hooks run before the early return (Rules of Hooks).
  const stuckNow =
    !!status && (status.status === "stuck" || status.needsHuman === true);
  const [open, setOpen] = useState(stuckNow);
  const wasStuck = useRef(stuckNow);
  useEffect(() => {
    if (stuckNow && !wasStuck.current) setOpen(true);
    wasStuck.current = stuckNow;
  }, [stuckNow]);

  if (!status || status.mode === "off" || status.status == null) return null;

  // F100-02: a not-converging review loop pauses and awaits the human. This is
  // not brainstorm-only — the spec/plan stages get stuck too, so the copy +
  // viewer follow the LIVE stage (a stuck spec must offer to accept the spec).
  const isStuck = stuckNow;
  const stuckLabel = (STEP_LABELS[status.stage ?? ""] ?? "draft").toLowerCase();
  const buildSuffix =
    status.stage === "build" && status.buildProgress
      ? ` (${status.buildProgress.done}/${status.buildProgress.total})`
      : "";
  // Compose the visible headline: "<headline>[· actor][ (done/total)]". The
  // backend already builds the "Stage — status" headline; we append the actor
  // and build progress here.
  const actorSuffix = status.actorLabel ? ` · ${status.actorLabel}` : "";

  return (
    <details
      className="coding-gov-status"
      aria-label="Governance status"
      data-stage={status.stage}
      data-status={status.status}
      open={open}
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="coding-gov-status-summary">
        <span className="coding-gov-summary-main">
          <span className="coding-gov-summary-title">Building Progress</span>
          <span className="coding-gov-summary-status">
            <span className="coding-gov-headline-text">{status.headline}</span>
            {actorSuffix ? <span className="coding-gov-actor">{actorSuffix}</span> : null}
            {buildSuffix ? <span className="coding-gov-build">{buildSuffix}</span> : null}
          </span>
        </span>
        <span className={pillClass(status.status)}>
          {STATUS_LABEL[status.status] ?? status.status}
        </span>
      </summary>
      <div className="coding-gov-status-body">
        {isStuck ? (
          <div className="coding-gov-stuck" role="region" aria-label="Governance needs you">
            <p className="coding-gov-stuck-msg">
              The team can&apos;t converge on this {stuckLabel}
              {status.reviewRound ? ` after ${status.reviewRound} rounds` : ""}. Read it,
              comment to steer the PM, or accept it as-is to continue.
            </p>
            <div className="coding-gov-stuck-actions">
              {onOpenBrainstorm ? (
                <button type="button" className="coding-btn" onClick={onOpenBrainstorm}>
                  Read {stuckLabel}
                </button>
              ) : null}
              {onCommentBrainstorm ? (
                <button type="button" className="coding-btn" onClick={onCommentBrainstorm}>
                  Comment
                </button>
              ) : null}
              {onOpenBrainstorm ? (
                <button
                  type="button"
                  className="coding-btn coding-btn-primary"
                  onClick={onOpenBrainstorm}
                >
                  Accept &amp; continue
                </button>
              ) : null}
            </div>
          </div>
        ) : null}
        <ol className="coding-gov-stepper" aria-label="Governance stages">
          {STEP_ORDER.map((stage) => {
            const step = status.steps.find((s) => s.stage === stage);
            const state: GovernanceStepState = step ? step.state : "pending";
            const isCurrent = status.stage === stage;
            const marker =
              state === "approved"
                ? "✓"
                : state === "changes_requested" || state === "stuck"
                  ? "!"
                  : "•";
            const stepContent = (
              <>
                <span className="coding-gov-step-marker" aria-hidden="true">
                  {marker}
                </span>
                <span className="coding-gov-step-label">{STEP_LABELS[stage]}</span>
                <span className="coding-gov-step-state">{stepStateLabel(state)}</span>
              </>
            );
            const clickable = !!onOpenStage && canOpenStage(stage, state);
            return (
              <li
                key={stage}
                className={stepClass(state, isCurrent)}
                aria-current={isCurrent ? "step" : undefined}
              >
                {clickable ? (
                  <button
                    type="button"
                    className="coding-gov-step-button"
                    onClick={() => onOpenStage(stage)}
                    aria-label={`Open ${STEP_LABELS[stage]} details`}
                  >
                    {stepContent}
                  </button>
                ) : (
                  <span className="coding-gov-step-static">{stepContent}</span>
                )}
              </li>
            );
          })}
        </ol>
      </div>
    </details>
  );
}
