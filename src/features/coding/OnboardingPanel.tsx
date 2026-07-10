// F135 — per-project onboarding: infer a North Star ("Understand this project").
// F137 — the Current Focus goals panel (multi-item, ordered, lifecycle-managed)
// replaces the F135 single-string Work Request textarea. Rendered near the top of
// an imported project's view.
import { useEffect, useRef, useState } from "react";

import * as api from "../../lib/api/coding";
import type { CodingProject, NorthStarProposal, RefreshPreview } from "../../lib/api/coding";
import RefreshProjectModal from "./RefreshProjectModal";
import CurrentFocusPanel from "./CurrentFocusPanel";

export default function OnboardingPanel({
  project,
  running,
  onChanged,
  onError,
}: {
  project: CodingProject;
  running?: boolean;
  onChanged: () => void;
  onError: (msg: string) => void;
}) {
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const [proposal, setProposal] = useState<NorthStarProposal | null>(null);
  const [scanning, setScanning] = useState(false);

  useEffect(() => {
    api.getNorthStarProposal(project.id).then(setProposal).catch(() => setProposal(null));
  }, [project.id]);

  // F138 — staleness of Errorta's snapshot vs the imported repo / remote.
  const [refreshPreview, setRefreshPreview] = useState<RefreshPreview | null>(null);
  const [showRefresh, setShowRefresh] = useState(false);
  const loadRefreshPreview = () => {
    if (!project.importSource) return;
    api
      .getRefreshPreview(project.id)
      .then((p) => {
        if (mounted.current) setRefreshPreview(p);
      })
      .catch(() => {
        if (mounted.current) setRefreshPreview(null);
      });
  };
  useEffect(() => {
    setRefreshPreview(null);
    loadRefreshPreview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.id, project.importSource?.clonedRef]);

  const understand = async () => {
    setScanning(true);
    try {
      const job = await api.startOrientationScan(project.id);
      let status = job;
      for (let i = 0; i < 120 && status.status !== "done" && status.status !== "error"; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        if (!mounted.current) return;
        status = await api.orientationScanStatus(project.id, job.jobId);
      }
      if (!mounted.current) return;
      if (status.status === "error") {
        onError(`Scan failed: ${status.message ?? "unknown"}`);
      } else {
        setProposal(await api.getNorthStarProposal(project.id));
      }
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
    } finally {
      setScanning(false);
    }
  };

  const accept = async () => {
    try {
      await api.acceptNorthStarProposal(project.id);
      setProposal((p) => (p ? { ...p, accepted: true } : p));
      onChanged();
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
    }
  };

  const isImported = Boolean(project.importSource);

  return (
    <section className="coding-onboarding" aria-label="Project onboarding">
      {isImported ? (
        <p className="coding-provenance">
          Imported from{" "}
          {project.importSource?.originUrl
            ? project.importSource.originUrl
            : project.importSource?.kind.replace(/_/g, " ")}
          {project.importSource?.clonedRef ? ` (${project.importSource.clonedRef})` : ""}
        </p>
      ) : null}

      {isImported ? (
        <div className="coding-refresh-row">
          {refreshPreview && (refreshPreview.remoteAhead ?? 0) > 0 ? (
            <span className="coding-refresh-badge" role="status">
              snapshot is {refreshPreview.remoteAhead} behind origin/
              {refreshPreview.defaultBranch ?? "?"}
            </span>
          ) : refreshPreview && refreshPreview.repoDiffers ? (
            <span className="coding-refresh-badge" role="status">
              snapshot differs from your folder
            </span>
          ) : null}
          <button
            type="button"
            className="coding-btn coding-btn-ghost coding-refresh-btn"
            onClick={() => setShowRefresh(true)}
            disabled={running || !refreshPreview}
            title={!refreshPreview ? "Refresh preview is still loading" : undefined}
          >
            Refresh
          </button>
        </div>
      ) : null}

      {isImported && !project.northStar ? (
        <div className="coding-understand">
          <button
            type="button"
            className="coding-btn coding-btn-primary"
            onClick={() => void understand()}
            disabled={scanning || running}
          >
            {scanning ? "Understanding…" : "Understand this project"}
          </button>
          <span className="coding-field-hint">
            The team reads the README and code and proposes a North Star for you to review.
          </span>
        </div>
      ) : null}

      {proposal && !proposal.accepted ? (
        <div className="coding-proposal" aria-label="North Star proposal">
          <h4>Proposed North Star</h4>
          {proposal.lowSignal ? (
            <p className="coding-location-note" role="status">
              {proposal.summary || "Not enough signal to propose a North Star — add one manually."}
            </p>
          ) : (
            <>
              <p className="coding-proposal-ns">{proposal.northStar}</p>
              {proposal.definitionOfDone ? (
                <p className="coding-proposal-dod">
                  <strong>Definition of done:</strong> {proposal.definitionOfDone}
                </p>
              ) : null}
              {proposal.summary ? <p className="coding-proposal-summary">{proposal.summary}</p> : null}
              {proposal.detectedStack.length > 0 ? (
                <p className="coding-proposal-stack">Stack: {proposal.detectedStack.join(", ")}</p>
              ) : null}
              <div className="coding-proposal-actions">
                <button type="button" className="coding-btn coding-btn-primary" onClick={() => void accept()}>
                  Accept as North Star
                </button>
                <button type="button" className="coding-btn coding-btn-ghost" onClick={() => setProposal(null)}>
                  Dismiss
                </button>
              </div>
            </>
          )}
        </div>
      ) : null}

      <CurrentFocusPanel
        projectId={project.id}
        running={running}
        onError={onError}
        onChanged={onChanged}
        phase={project.phase}
        northStar={project.northStar}
      />

      <RefreshProjectModal
        isOpen={showRefresh}
        projectId={project.id}
        preview={refreshPreview}
        onClose={() => setShowRefresh(false)}
        onRefreshed={() => {
          loadRefreshPreview();
          onChanged();
        }}
      />
    </section>
  );
}
