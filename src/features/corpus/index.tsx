import { useState } from "react";

import CorpusDropZone from "./CorpusDropZone";
import DeleteCorpusButton from "./DeleteCorpusButton";
import RefreshDiffModal from "./RefreshDiffModal";
import WelcomeInstaller from "../welcome/WelcomeInstaller";
import KnowledgeEmptyState from "../knowledge/KnowledgeEmptyState";
import KnowledgeHeader from "../knowledge/KnowledgeHeader";
import QuickStartGuide from "../knowledge/QuickStartGuide";
import { useQuickStart } from "../knowledge/useQuickStart";
import { PANEL_BLURBS } from "../knowledge/quickStartContent";
import {
  draftLocalCorpus,
  useKnowledgeCorpusSelection,
} from "../knowledge/useKnowledgeCorpusSelection";
import {
  corpusCountLabel,
  deleteCorpus,
  hasCorpusCapability,
  refreshPreview,
  type CorpusSummary,
} from "../../lib/api/corpus";
import { navigateKnowledge } from "../../lib/featureNavigation";
import type { RefreshDiffResponse } from "./types";

function sanitizeCorpusName(value: string): string {
  return value.replace(/[^a-zA-Z0-9_-]/g, "");
}

function RemoteCorpusPanel({ corpus, onBuildLocal }: {
  corpus: CorpusSummary;
  onBuildLocal: () => void;
}) {
  return (
    <section className="knowledge-mode-panel" aria-label="Remote corpus summary">
      <h2>{corpus.name}</h2>
      <p>
        This corpus is listed by remote AIAR. Errorta can show its catalog
        readiness here, but file browsing, local upload, refresh diff, and
        folder watch are disabled until the remote backend advertises those
        capabilities.
      </p>
      <div className="knowledge-metric-grid">
        <div className="knowledge-metric">
          <span>Readiness</span>
          <strong>{corpus.status}</strong>
        </div>
        <div className="knowledge-metric">
          <span>Unit</span>
          <strong>{corpus.unit ?? "chunks"}</strong>
        </div>
        <div className="knowledge-metric">
          <span>Count</span>
          <strong>{corpusCountLabel(corpus)}</strong>
        </div>
        <div className="knowledge-metric">
          <span>Source</span>
          <strong>{corpus.source}</strong>
        </div>
      </div>
      <div className="knowledge-mode-actions">
        <button
          type="button"
          onClick={() => navigateKnowledge({ feature: "briefs", corpus: corpus.name })}
        >
          View briefs
        </button>
        <button type="button" onClick={onBuildLocal}>
          Build local corpus
        </button>
      </div>
    </section>
  );
}

export default function CorpusFeature() {
  const selection = useKnowledgeCorpusSelection();
  const quickStart = useQuickStart();
  const [customMode, setCustomMode] = useState(false);
  const [newCorpus, setNewCorpus] = useState("");
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [diffOpen, setDiffOpen] = useState(false);
  const [diff, setDiff] = useState<RefreshDiffResponse | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState<string | null>(null);
  const activeCorpus = customMode ? newCorpus.trim() : selection.selectedName;
  const activeSummary = customMode && activeCorpus
    ? draftLocalCorpus(activeCorpus)
    : selection.selected;
  const canRefresh = hasCorpusCapability(activeSummary, "refresh_preview");
  const canUpload = hasCorpusCapability(activeSummary, "upload_files");

  const onCheckForChanges = async () => {
    if (!activeCorpus || !canRefresh) return;
    setDiffOpen(true);
    setDiffLoading(true);
    setDiffError(null);
    setDiff(null);
    try {
      const result = await refreshPreview(activeCorpus);
      setDiff(result);
    } catch (err) {
      setDiffError(err instanceof Error ? err.message : String(err));
    } finally {
      setDiffLoading(false);
    }
  };

  const onDeleteCorpus = async (name: string) => {
    try {
      await deleteCorpus(name);
      const items = await selection.reload();
      setDeleteError(null);
      // Clear the active selection if the deleted corpus was the one in view.
      // Pick from the freshly-reloaded list, not the stale closure-captured
      // `selection.corpora` (which still contains the just-deleted `name`).
      if (selection.selectedName === name) {
        const next = items.find((c) => c.name !== name)?.name ?? "";
        selection.setSelectedName(next);
      }
      if (customMode && newCorpus.trim() === name) {
        setCustomMode(false);
        setNewCorpus("");
      }
    } catch (err) {
      // Mirror the file-delete error surface (CorpusDropZone) — a failed
      // corpus delete must tell the user, not fail silently with a stale list.
      setDeleteError(err instanceof Error ? err.message : String(err));
    }
  };

  const onCloseDiff = () => {
    setDiffOpen(false);
    setDiff(null);
    setDiffError(null);
    setDiffLoading(false);
  };

  return (
    <section className="feature-pane">
      <KnowledgeHeader
        title="Corpus"
        selection={selection}
        blurb={PANEL_BLURBS.corpus}
        onOpenQuickStart={quickStart.openGuide}
      />
      <div className="knowledge-mode-actions">
        <button
          type="button"
          onClick={onCheckForChanges}
          disabled={!activeCorpus || !canRefresh}
        >
          Check for changes
        </button>
        <button type="button" onClick={() => {
          setCustomMode(true);
          setNewCorpus("");
        }}>
          New local corpus
        </button>
        {activeSummary?.source === "local" && activeSummary.name ? (
          <DeleteCorpusButton
            name={activeSummary.name}
            onDelete={() => onDeleteCorpus(activeSummary.name)}
          />
        ) : null}
      </div>
      {customMode ? (
        <label className="corpus-name-row">
          <span>New corpus</span>
          <input
            type="text"
            value={newCorpus}
            onChange={(e) => setNewCorpus(sanitizeCorpusName(e.target.value))}
            placeholder="corpus-id"
            aria-label="New corpus name"
            spellCheck={false}
          />
        </label>
      ) : null}
      {deleteError ? <p className="error-note">{deleteError}</p> : null}
      <details className="corpus-sample-corpus">
        <summary>Add a sample corpus</summary>
        <p className="corpus-sample-blurb">
          New here? Install Errorta&apos;s own documentation as a small starter
          corpus to try retrieval and the judge loop, then delete it whenever.
        </p>
        <WelcomeInstaller variant="panel" />
      </details>
      {!activeCorpus ? (
        <KnowledgeEmptyState
          onCreateLocal={() => {
            setCustomMode(true);
            setNewCorpus("");
          }}
          onOpenQuickStart={quickStart.openGuide}
          onDismissQuickStart={quickStart.dismiss}
          quickStartDismissed={quickStart.dismissed}
        />
      ) : activeSummary?.source === "unknown" ? (
        <section className="knowledge-mode-panel" aria-label="Missing corpus">
          <h2>Corpus not found</h2>
          <p>
            {activeCorpus} is still selected, but it is not in the current
            catalog. Pick an existing corpus from the header or create a new
            local corpus with this name.
          </p>
          <div className="knowledge-mode-actions">
            <button type="button" onClick={() => setCustomMode(true)}>
              Create local corpus
            </button>
          </div>
        </section>
      ) : activeSummary?.source !== "local" || !canUpload ? (
        activeSummary ? (
          <RemoteCorpusPanel
            corpus={activeSummary}
            onBuildLocal={() => setCustomMode(true)}
          />
        ) : null
      ) : (
        <CorpusDropZone corpus={activeCorpus} />
      )}
      <RefreshDiffModal
        isOpen={diffOpen}
        onClose={onCloseDiff}
        corpus={activeCorpus}
        diff={diff}
        loading={diffLoading}
        error={diffError}
      />
      <QuickStartGuide open={quickStart.open} onClose={quickStart.closeGuide} />
    </section>
  );
}
