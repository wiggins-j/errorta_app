// F100-02 — Brainstorm viewer. A read-only drawer that shows the LATEST
// brainstorm artifact (markdown rendered as preformatted text — the coding
// feature has no markdown renderer, so we never inject HTML), its version /
// state / the latest review's findings, and the three human controls:
//   • Comment → Send to PM (an interjection tagged with the viewed artifact).
//     When the run is stopped, also offer "Send & resume".
//   • Accept this brainstorm & continue (a human override that force-accepts the
//     EXACT viewed artifact, advancing the loop to drafting_spec).
// It never edits the live artifact and renders no member ids / tokens.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import * as api from "../../lib/api/coding";
import type {
  GovernanceArtifact,
  GovernanceReview,
  GovernanceStage,
  GovernanceSummary,
} from "../../lib/api/coding";

// Each governed stage maps to an artifact kind + the labels used in the copy.
// This is why the viewer can show/accept the SPEC (or plan) when that stage is
// the one stuck — not just the brainstorm. (Bugfix: a stuck spec used to open
// the already-approved brainstorm, so "Accept & continue" did nothing.)
type ReviewableStage = "brainstorm" | "spec" | "plan";
const STAGE_META: Record<
  ReviewableStage,
  { kind: string; label: string; next: string }
> = {
  brainstorm: { kind: "brainstorm", label: "brainstorm", next: "spec" },
  spec: { kind: "spec", label: "spec", next: "plan" },
  plan: { kind: "implementation_plan", label: "plan", next: "build" },
};

function stageMetaFor(stage: GovernanceStage | undefined) {
  if (stage && stage in STAGE_META) return STAGE_META[stage as ReviewableStage];
  return STAGE_META.brainstorm;
}

export interface BrainstormViewerProps {
  projectId: string;
  /** The current governance summary (provides the artifact list + reviews). */
  summary: GovernanceSummary | null;
  /**
   * Which governed stage's artifact to show/accept. Defaults to "brainstorm"
   * for back-compat; the parent passes the live stuck stage so a stuck spec/plan
   * opens (and accepts) the RIGHT artifact.
   */
  stage?: GovernanceStage;
  /** Whether a run is currently in progress (controls "Send & resume"). */
  running: boolean;
  /** Close the drawer. */
  onClose: () => void;
  /** Refresh the parent's governance summary after a comment/accept. */
  onChanged: () => void;
  /** If true, start with the comment box focused (the "Comment" stuck action). */
  startCommenting?: boolean;
}

/** Find the newest artifact (highest version) of the given kind in the summary. */
function latestArtifactForKind(
  summary: GovernanceSummary | null,
  kind: string,
): GovernanceArtifact | null {
  if (!summary) return null;
  const matching = summary.artifacts.filter((a) => a.artifactKind === kind);
  if (matching.length === 0) return null;
  return matching.reduce((best, a) => (a.version > best.version ? a : best));
}

/** The latest review attached to the given artifact id, if any. */
function latestReviewFor(
  summary: GovernanceSummary | null,
  artifactId: string,
): GovernanceReview | null {
  if (!summary) return null;
  const matching = summary.reviews.filter((r) => r.artifactId === artifactId);
  if (matching.length === 0) return null;
  // The summary preserves append order; the last one is the most recent.
  return matching[matching.length - 1];
}

/** Pull the spec acceptance-criteria list out of body_json, if present. */
function acceptanceCriteriaOf(bodyJson: Record<string, unknown> | undefined): string[] {
  const raw = bodyJson?.acceptance_criteria;
  if (!Array.isArray(raw)) return [];
  return raw.map((c) => String(c)).filter((c) => c.trim());
}

/**
 * The artifact body. Renders the written markdown when present; when it is
 * empty/whitespace, renders the structured fields instead (for a spec, the
 * acceptance criteria; for any kind, a readable dump of body_json) so the drawer
 * NEVER shows a bare blank box.
 */
function ArtifactBody({
  artifact,
  label,
}: {
  artifact: GovernanceArtifact;
  label: string;
}) {
  const body = (artifact.bodyMarkdown ?? "").trim();
  if (body) {
    return (
      <pre className="coding-brainstorm-body" aria-label="Artifact content">
        {body}
      </pre>
    );
  }

  const criteria = acceptanceCriteriaOf(artifact.bodyJson);
  const hasJson =
    artifact.bodyJson != null && Object.keys(artifact.bodyJson).length > 0;
  return (
    <div className="coding-brainstorm-body" aria-label="Artifact content">
      <p className="coding-empty">
        (this {label} has no written body — showing structured fields)
      </p>
      {criteria.length ? (
        <div>
          <strong>Acceptance criteria</strong>
          <ul>
            {criteria.map((c, idx) => (
              <li key={idx}>{c}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {hasJson ? (
        <pre aria-label="Structured fields">
          {JSON.stringify(artifact.bodyJson, null, 2)}
        </pre>
      ) : !criteria.length ? (
        <p className="coding-empty">(no content)</p>
      ) : null}
    </div>
  );
}

export default function BrainstormViewer({
  projectId,
  summary,
  stage,
  running,
  onClose,
  onChanged,
  startCommenting,
}: BrainstormViewerProps) {
  const meta = stageMetaFor(stage);
  const titleLabel = meta.label.charAt(0).toUpperCase() + meta.label.slice(1);
  // The newest artifact for THIS stage in the CURRENT summary (recomputed every
  // refresh) — brainstorm by default, or the spec/plan when that stage is stuck.
  const newest = latestArtifactForKind(summary, meta.kind);

  // The artifact we OPENED — captured once, on first mount, so accept/comment
  // always target exactly the id the user is looking at. If the live summary
  // later advances to a newer version, we keep showing the opened one and
  // surface a "newer version available" hint instead of silently swapping.
  const openedRef = useRef<GovernanceArtifact | null>(newest);
  const opened = openedRef.current;
  const openedId = opened?.artifactId ?? null;

  const [artifact, setArtifact] = useState<GovernanceArtifact | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [sentNote, setSentNote] = useState<string | null>(null);
  const [confirmingAccept, setConfirmingAccept] = useState(false);

  const commentRef = useRef<HTMLTextAreaElement | null>(null);
  // Bumped when the user clicks "Refresh" to adopt the newer version.
  const [refreshTick, setRefreshTick] = useState(0);

  // Adopt the newest version (clicked Refresh): re-point the opened ref so the
  // body/findings/accept-target all move to the new id, and pull the parent's
  // summary forward too.
  const adoptNewest = useCallback(() => {
    if (newest) openedRef.current = newest;
    setRefreshTick((n) => n + 1);
    onChanged();
  }, [newest, onChanged]);

  // Fetch the full body for the opened brainstorm id (the summary omits it).
  useEffect(() => {
    if (!openedId) {
      setArtifact(null);
      return;
    }
    let cancelled = false;
    setLoadError(null);
    api
      .getGovernanceArtifact(projectId, openedId)
      .then((a) => {
        if (!cancelled) setArtifact(a);
      })
      .catch((e) => {
        if (!cancelled) {
          setArtifact(null);
          setLoadError(e instanceof Error ? e.message : String(e));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, openedId, refreshTick]);

  useEffect(() => {
    if (startCommenting) commentRef.current?.focus();
  }, [startCommenting]);

  // A newer brainstorm version exists than the one we opened.
  const hasNewerVersion =
    !!opened && !!newest && newest.artifactId !== opened.artifactId;

  const review = useMemo(
    () => (openedId ? latestReviewFor(summary, openedId) : null),
    [summary, openedId],
  );

  const doComment = useCallback(
    // `continue_` true = post the comment AND re-drive the governance loop so the
    // PM revises the artifact with this direction + the latest review findings in
    // context. Uses /run/continue (NOT the crash-recovery /run/resume, which 409s
    // a review-stopped run). `continue_` false = queue the comment for the PM's
    // next turn and show a visible confirmation (so it doesn't read as a no-op).
    async (continue_: boolean) => {
      if (!openedId) return;
      const text = comment.trim();
      if (!text) return;
      setBusy(true);
      setActionError(null);
      setSentNote(null);
      try {
        await api.interject(projectId, text, openedId);
        if (continue_) {
          await api.continueRun(projectId);
          setComment("");
          onChanged();
          onClose();
          return;
        }
        setComment("");
        setSentNote("Sent to the PM — it will be addressed on the next run.");
        onChanged();
      } catch (e) {
        setActionError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [projectId, openedId, comment, onChanged, onClose],
  );

  const doAccept = useCallback(async () => {
    if (!openedId) return;
    setBusy(true);
    setActionError(null);
    try {
      await api.acceptGovernanceArtifact(projectId, openedId);
      setConfirmingAccept(false);
      onChanged();
      onClose();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [projectId, openedId, onChanged, onClose]);

  return (
    <div className="coding-brainstorm-overlay" role="presentation" onClick={onClose}>
      <section
        className="coding-brainstorm-drawer"
        role="dialog"
        aria-modal="true"
        aria-label={titleLabel}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="coding-brainstorm-head">
          <div className="coding-brainstorm-title">
            <strong>{artifact?.title || opened?.title || titleLabel}</strong>
            {opened ? (
              <span className="coding-brainstorm-meta">
                <span className="coding-brainstorm-version">v{opened.version}</span>
                <span className={`coding-art-status coding-gov-${opened.state}`}>
                  {opened.state}
                </span>
              </span>
            ) : null}
          </div>
          <button
            type="button"
            className="coding-btn coding-brainstorm-close"
            onClick={onClose}
          >
            Close
          </button>
        </header>

        {hasNewerVersion ? (
          <p className="coding-brainstorm-newer" role="status">
            A newer version of this {meta.label} is available.{" "}
            <button type="button" className="coding-link" onClick={adoptNewest}>
              Refresh
            </button>
          </p>
        ) : null}

        {!opened ? (
          <p className="coding-empty">No {meta.label} yet.</p>
        ) : loadError ? (
          <p className="coding-error" role="alert">
            {loadError}
          </p>
        ) : artifact == null ? (
          <p className="coding-empty">Loading {meta.label}…</p>
        ) : (
          <ArtifactBody artifact={artifact} label={meta.label} />
        )}

        {review && review.findings.length ? (
          <div className="coding-brainstorm-findings" aria-label="Latest review findings">
            <h4>Latest review findings</h4>
            <ul>
              {review.findings.map((f, idx) => (
                <li
                  key={idx}
                  className={f.blocking ? "coding-finding-blocking" : "coding-finding"}
                >
                  <span className="coding-finding-sev">{f.severity}</span>
                  <strong>{f.title}</strong>
                  {f.body ? <span className="coding-finding-body">{f.body}</span> : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {actionError ? (
          <p className="coding-error" role="alert">
            {actionError}
          </p>
        ) : null}

        {sentNote ? (
          <p className="coding-brainstorm-sent" role="status">
            {sentNote}
          </p>
        ) : null}

        <div className="coding-brainstorm-actions">
          <label htmlFor="coding-brainstorm-comment">Comment to the PM</label>
          <textarea
            id="coding-brainstorm-comment"
            ref={commentRef}
            className="coding-brainstorm-comment"
            value={comment}
            onChange={(e) => {
              setComment(e.target.value);
              if (sentNote) setSentNote(null);
            }}
            placeholder="Tell the PM what to change or clarify…"
            rows={3}
            disabled={busy || !opened}
          />
          <div className="coding-brainstorm-buttons">
            <button
              type="button"
              className="coding-btn"
              onClick={() => void doComment(false)}
              disabled={busy || !opened || !comment.trim()}
            >
              Send to PM
            </button>
            {!running ? (
              <button
                type="button"
                className="coding-btn"
                onClick={() => void doComment(true)}
                disabled={busy || !opened || !comment.trim()}
              >
                Send &amp; continue
              </button>
            ) : null}
            {confirmingAccept ? (
              <span className="coding-brainstorm-confirm">
                <span>Accept this {meta.label} and continue to the {meta.next}?</span>
                <button
                  type="button"
                  className="coding-btn coding-btn-primary"
                  onClick={() => void doAccept()}
                  disabled={busy}
                >
                  Confirm accept
                </button>
                <button
                  type="button"
                  className="coding-btn"
                  onClick={() => setConfirmingAccept(false)}
                  disabled={busy}
                >
                  Cancel
                </button>
              </span>
            ) : (
              <button
                type="button"
                className="coding-btn coding-btn-primary"
                onClick={() => setConfirmingAccept(true)}
                disabled={busy || !opened}
              >
                Accept this {meta.label} &amp; continue
              </button>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
