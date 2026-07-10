// F008 Briefs — feature pane entry point.
//
// Three-column layout: BriefsList (left) / BriefEditor (center) / BriefStatusPanel (right),
// with BriefControls under the editor. The brief lifecycle (DRAFT → VALIDATING →
// RUNNING → …) gates which buttons render — see ./types.ts and the FSM in
// python/errorta_briefs/lifecycle.py.
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  type BriefDetail,
  type BriefSummary,
  deleteBrief,
  getBrief,
  listBriefs,
  pauseBrief,
  refreshBrief,
  runBrief,
  validateBrief,
} from "../../lib/api/briefs";
import BriefControls from "./BriefControls";
import BriefEditor from "./BriefEditor";
import BriefsList from "./BriefsList";
import BriefStatusPanel from "./BriefStatusPanel";
import CreateBriefModal from "./CreateBriefModal";
import KnowledgeEmptyState from "../knowledge/KnowledgeEmptyState";
import KnowledgeHeader from "../knowledge/KnowledgeHeader";
import QuickStartGuide from "../knowledge/QuickStartGuide";
import { useKnowledgeCorpusSelection } from "../knowledge/useKnowledgeCorpusSelection";
import { useQuickStart } from "../knowledge/useQuickStart";
import { PANEL_BLURBS } from "../knowledge/quickStartContent";
import { navigateKnowledge } from "../../lib/featureNavigation";
import type { BriefStateValue } from "./types";
import "./briefs.css";

export default function BriefsFeature() {
  const selection = useKnowledgeCorpusSelection();
  const quickStart = useQuickStart();
  const [briefs, setBriefs] = useState<BriefSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<BriefDetail | null>(null);
  const [showCreate, setShowCreate] = useState<boolean>(false);
  const [busy, setBusy] = useState<boolean>(false);
  const [actionMessage, setActionMessage] = useState<
    { kind: "error" | "success"; text: string } | null
  >(null);
  const setActionError = (text: string | null) =>
    setActionMessage(text === null ? null : { kind: "error", text });

  const reloadList = useCallback(async () => {
    try {
      const list = await listBriefs();
      setBriefs(list);
      return list;
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
      return [];
    }
  }, []);

  useEffect(() => {
    void reloadList();
  }, [reloadList]);

  const visibleBriefs = useMemo(() => {
    if (!selection.selectedName) return briefs;
    return briefs.filter((b) => b.corpus_name === selection.selectedName);
  }, [briefs, selection.selectedName]);

  useEffect(() => {
    if (!selection.selectedName || !selectedId) return;
    const selectedBrief = briefs.find((b) => b.brief_id === selectedId);
    if (selectedBrief && selectedBrief.corpus_name !== selection.selectedName) {
      setSelectedId(visibleBriefs[0]?.brief_id ?? null);
    }
  }, [briefs, selectedId, selection.selectedName, visibleBriefs]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    getBrief(selectedId)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((err) => {
        if (!cancelled) {
          setActionError(err instanceof Error ? err.message : String(err));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const runAction = useCallback(
    async (fn: () => Promise<unknown>) => {
      if (!selectedId) return;
      setBusy(true);
      setActionError(null);
      try {
        await fn();
        await reloadList();
        const fresh = await getBrief(selectedId);
        setDetail(fresh);
      } catch (err) {
        setActionError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    },
    [selectedId, reloadList],
  );

  const onCreated = async (briefId: string, corpusName?: string) => {
    setShowCreate(false);
    await reloadList();
    if (corpusName) selection.setSelectedName(corpusName);
    setSelectedId(briefId);
  };

  const onArchive = async () => {
    if (!selectedId) return;
    setBusy(true);
    setActionError(null);
    try {
      await deleteBrief(selectedId);
      setSelectedId(null);
      setDetail(null);
      await reloadList();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const state: BriefStateValue = (detail?.manifest.state as BriefStateValue) ?? "DRAFT";

  return (
    <div className="briefs-feature feature-pane">
      <KnowledgeHeader
        title="Briefs"
        selection={selection}
        blurb={PANEL_BLURBS.briefs}
        onOpenQuickStart={quickStart.openGuide}
        actions={
          <button type="button" onClick={() => setShowCreate(true)}>
            Create brief
          </button>
        }
      />
      {actionMessage && (
        <div
          className="briefs-parse-banner"
          role={actionMessage.kind === "error" ? "alert" : "status"}
          data-kind={actionMessage.kind}
        >
          {actionMessage.text}
        </div>
      )}
      <div className="briefs-layout">
        <section className="briefs-pane" aria-label="Briefs list">
          <h3>Briefs</h3>
          <BriefsList
            briefs={visibleBriefs}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onBriefCreated={onCreated}
            initialCorpusName={selection.selectedName}
          />
        </section>
        {selectedId && detail ? (
          <>
            <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem", minHeight: 0 }}>
              <BriefEditor
                briefId={selectedId}
                initialMarkdown={detail.markdown}
                initialParseErrors={detail.manifest.parse_errors}
              />
              <BriefControls
                state={state}
                briefId={selectedId}
                markdown={detail.markdown}
                busy={busy}
                onValidate={() => runAction(() => validateBrief(selectedId))}
                onRun={() => runAction(() => runBrief(selectedId))}
                onPause={() => runAction(() => pauseBrief(selectedId))}
                onRefresh={() => runAction(() => refreshBrief(selectedId))}
                onArchive={onArchive}
                onOpenCorpus={() =>
                  navigateKnowledge({
                    feature: "corpus",
                    corpus: detail.manifest.corpus_name,
                  })
                }
                onExported={(info) =>
                  setActionMessage({
                    kind: "success",
                    text: `Exported ${info.slug}.md to ${info.dir}`,
                  })
                }
              />
            </div>
            <BriefStatusPanel briefId={selectedId} state={state} />
          </>
        ) : (
          <section
            className="briefs-pane briefs-details-empty"
            aria-label="Brief details"
          >
            {briefs.length === 0 && !selection.selectedName ? (
              <KnowledgeEmptyState
                onOpenQuickStart={quickStart.openGuide}
                onDismissQuickStart={quickStart.dismiss}
                quickStartDismissed={quickStart.dismissed}
              />
            ) : (
              <div className="briefs-empty">
                {visibleBriefs.length === 0
                  ? "No briefs target the active corpus yet."
                  : "Select a brief to view details."}
              </div>
            )}
          </section>
        )}
      </div>
      {showCreate && (
        <CreateBriefModal
          onCreated={onCreated}
          onCancel={() => setShowCreate(false)}
          initialCorpusName={selection.selectedName}
        />
      )}
      <QuickStartGuide open={quickStart.open} onClose={quickStart.closeGuide} />
    </div>
  );
}
