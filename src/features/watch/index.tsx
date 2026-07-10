// F005 — folder watch + auto-ingest feature pane.
import { useCallback, useEffect, useState } from "react";
import * as watchApi from "../../lib/api/watch";
import { hasCorpusCapability } from "../../lib/api/corpus";
import { FolderWatchButton } from "./FolderWatchButton";
import { FolderWatchDialog } from "./FolderWatchDialog";
import { FolderWatchBadge } from "./FolderWatchBadge";
import { FileSourceBadge } from "./FileSourceBadge";
import KnowledgeEmptyState from "../knowledge/KnowledgeEmptyState";
import KnowledgeHeader from "../knowledge/KnowledgeHeader";
import QuickStartGuide from "../knowledge/QuickStartGuide";
import { useKnowledgeCorpusSelection } from "../knowledge/useKnowledgeCorpusSelection";
import { useQuickStart } from "../knowledge/useQuickStart";
import { PANEL_BLURBS } from "../knowledge/quickStartContent";
import type { DeletionPolicy, WatchStatus, WatchStatusList } from "./types";

function isStatusList(x: WatchStatus | WatchStatusList): x is WatchStatusList {
  return Object.prototype.hasOwnProperty.call(x, "watchers");
}

export default function WatchFeature() {
  const selection = useKnowledgeCorpusSelection();
  const quickStart = useQuickStart();
  const [status, setStatus] = useState<WatchStatus | null>(null);
  const [pickedPath, setPickedPath] = useState<string | null>(null);
  const [mode, setMode] = useState<"start" | "change">("start");
  const [error, setError] = useState<string | null>(null);
  const selected = selection.selected;
  const activeCorpus = selection.selectedName;
  const canWatch = hasCorpusCapability(selected, "folder_watch");

  const refresh = useCallback(async () => {
    if (!activeCorpus || !canWatch) {
      setStatus(null);
      return;
    }
    try {
      const s = await watchApi.status(activeCorpus);
      if (isStatusList(s)) {
        setStatus(s.watchers[0] ?? null);
      } else {
        setStatus(s);
      }
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [activeCorpus, canWatch]);

  useEffect(() => {
    refresh();
    // Watcher health pip refreshes every 10s — cheap status call.
    const id = window.setInterval(refresh, 10_000);
    return () => window.clearInterval(id);
  }, [refresh]);

  async function handleStop() {
    if (!activeCorpus) return;
    await watchApi.stop(activeCorpus);
    await refresh();
  }
  async function handlePause() {
    if (!activeCorpus) return;
    await watchApi.pause(activeCorpus);
    await refresh();
  }
  async function handleResume() {
    if (!activeCorpus) return;
    await watchApi.resume(activeCorpus);
    await refresh();
  }
  async function handleSetPolicy(p: DeletionPolicy) {
    if (!activeCorpus) return;
    await watchApi.setDeletionPolicy(activeCorpus, p);
    await refresh();
  }
  async function handleForceRescan(corpus: string) {
    await watchApi.forceRescan(corpus);
    await refresh();
  }

  const watching = !!status?.watching;

  return (
    <section className="feature-pane">
      <KnowledgeHeader
        title="Folder Watcher"
        selection={selection}
        blurb={PANEL_BLURBS.watch}
        onOpenQuickStart={quickStart.openGuide}
      />

      {error ? (
        <p className="knowledge-callout knowledge-callout-error" role="alert">
          {error}
        </p>
      ) : null}

      {!activeCorpus ? (
        <KnowledgeEmptyState
          onOpenQuickStart={quickStart.openGuide}
          onDismissQuickStart={quickStart.dismiss}
          quickStartDismissed={quickStart.dismissed}
        />
      ) : !canWatch ? (
        <section className="knowledge-mode-panel" aria-label="Folder watch unavailable">
          <h2>Folder watch is local-only for this corpus</h2>
          <p>
            {selected?.name ?? activeCorpus} is {selected?.source ?? "not in the catalog"}.
            Folder Watcher needs a local corpus because it reads a folder from
            this machine and writes into the local corpus manifest.
          </p>
        </section>
      ) : null}

      {watching && status ? (
        <FolderWatchBadge
          status={status}
          onPause={handlePause}
          onResume={handleResume}
          onChange={() => {
            setMode("change");
            setPickedPath("");
          }}
          onStop={handleStop}
          onSetDeletionPolicy={handleSetPolicy}
          onForceRescan={handleForceRescan}
        />
      ) : null}

      {activeCorpus && canWatch && !watching && pickedPath === null ? (
        <div className="knowledge-mode-panel">
          <h2>Watch a folder</h2>
          <p className="feature-pane-note">
            Point this corpus at a folder. New files appear automatically; deleted
            files are handled per your policy. State persists across restarts.
          </p>
          <FolderWatchButton
            onPick={(p) => {
              setMode("start");
              setPickedPath(p);
            }}
          />
        </div>
      ) : null}

      {activeCorpus && canWatch && pickedPath === "" ? (
        <div className="knowledge-mode-actions">
          <FolderWatchButton
            label="Pick a folder"
            onPick={(p) => setPickedPath(p)}
          />
          <button
            type="button"
            onClick={() => {
              setPickedPath(null);
              setMode("start");
            }}
          >
            Cancel
          </button>
        </div>
      ) : null}

      {activeCorpus && canWatch && pickedPath ? (
        <FolderWatchDialog
          corpus={activeCorpus}
          path={pickedPath}
          mode={mode}
          onCancel={() => {
            setPickedPath(null);
            setMode("start");
          }}
          onStarted={async () => {
            setPickedPath(null);
            setMode("start");
            await refresh();
          }}
          onPickDifferent={() => setPickedPath("")}
        />
      ) : null}

      {/*
        File-list source-badge demo. Real list lives in F004's corpus pane;
        here we render a small legend so the badge component is reachable.
      */}
      <div
        className="folder-watch-legend"
      >
        <p>
          File source legend
        </p>
        <div>
          <FileSourceBadge source="watched" /> auto-ingested from this folder
          <FileSourceBadge source="uploaded" /> uploaded directly
        </div>
      </div>
      <QuickStartGuide open={quickStart.open} onClose={quickStart.closeGuide} />
    </section>
  );
}
