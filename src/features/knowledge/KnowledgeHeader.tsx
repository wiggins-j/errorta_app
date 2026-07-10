import { type ReactNode, useEffect, useState } from "react";

import { sidecarHealth, type CorpusBackendHealth } from "../../lib/api";
import { corpusCountLabel } from "../../lib/api/corpus";
import CorpusPicker from "../corpus/CorpusPicker";
import type { KnowledgeCorpusSelection } from "./useKnowledgeCorpusSelection";
import "./knowledge.css";

interface Props {
  title: string;
  /** Optional internal spec id (e.g. "F004 …"). Omitted on user-facing pages —
   * these dev references aren't meant for users. */
  spec?: string;
  selection: KnowledgeCorpusSelection;
  actions?: ReactNode;
  /** F132 — a short plain-language explainer of what this panel is for and how
   * to set it up, shown under the title (the Judge/Briefs onboarding steps were
   * removed, so the app itself explains the knowledge panels). */
  blurb?: ReactNode;
  /** F134 — open the always-available Knowledge Quick Start guide. When omitted,
   * the control is not rendered. */
  onOpenQuickStart?: () => void;
}

function backendLabel(backend: CorpusBackendHealth | null): string {
  if (!backend) return "Backend status unavailable";
  const kind = backend.kind.replace(/_/g, " ");
  const detail = backend.detail ?? {};
  const baseUrl = typeof detail.base_url === "string" ? detail.base_url : null;
  const mode = typeof detail.mode === "string" ? detail.mode : null;
  if (baseUrl) return `${kind} at ${baseUrl}`;
  if (mode) return `${kind}: ${mode}`;
  return kind;
}

export default function KnowledgeHeader({
  title,
  spec,
  selection,
  actions,
  blurb,
  onOpenQuickStart,
}: Props) {
  const [backend, setBackend] = useState<CorpusBackendHealth | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function ping() {
      try {
        const health = await sidecarHealth();
        if (!cancelled) setBackend(health.corpus_backend ?? null);
      } catch {
        if (!cancelled) setBackend(null);
      }
    }
    void ping();
    const id = window.setInterval(ping, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const selected = selection.selected;
  const showCoordinationWarning =
    backend?.retrieval_coordinated === false && backend.kind !== "local";

  return (
    <header className="knowledge-header">
      <div className="knowledge-title-row">
        <div>
          <span className="knowledge-eyebrow">Knowledge</span>
          <h1>{title}</h1>
          {spec ? <p className="feature-pane-spec">{spec}</p> : null}
          {blurb ? <p className="knowledge-blurb">{blurb}</p> : null}
        </div>
        {actions || onOpenQuickStart ? (
          <div className="knowledge-header-actions">
            {onOpenQuickStart ? (
              <button
                type="button"
                className="knowledge-quickstart-button"
                onClick={onOpenQuickStart}
              >
                <span aria-hidden="true">?</span>
                <span className="knowledge-quickstart-label">Quick Start</span>
              </button>
            ) : null}
            {actions}
          </div>
        ) : null}
      </div>
      <div className="knowledge-corpus-bar">
        <CorpusPicker
          label="Active corpus"
          value={selection.selectedName}
          onChange={selection.setSelectedName}
          corpora={selection.corpora}
          loading={selection.loading}
          allowEmpty
          emptyLabel="Select corpus"
          noCorporaLabel="No corpora yet"
        />
        <div className="knowledge-corpus-summary" aria-live="polite">
          {selected ? (
            <>
              <span className={`knowledge-badge knowledge-badge-${selected.source}`}>
                {selected.source}
              </span>
              <span className={`knowledge-badge knowledge-badge-${selected.status}`}>
                {selected.status}
              </span>
              <span className="knowledge-corpus-count">{corpusCountLabel(selected)}</span>
            </>
          ) : (
            <span className="knowledge-corpus-count">No corpus selected</span>
          )}
        </div>
      </div>
      {selection.error ? (
        <div className="knowledge-callout knowledge-callout-error" role="alert">
          {selection.error}
        </div>
      ) : null}
      <div className="knowledge-backend-line">
        <span>{backendLabel(backend)}</span>
        <span>
          Retrieval{" "}
          {backend?.retrieval_coordinated === false
            ? "not coordinated"
            : "coordinated or local"}
        </span>
      </div>
      {showCoordinationWarning ? (
        <div className="knowledge-callout knowledge-callout-warning" role="status">
          Corpus listing is coming from remote AIAR, but retrieval is not yet
          coordinated with that backend. You can inspect availability here;
          retrieval alignment is tracked separately.
        </div>
      ) : null}
    </header>
  );
}
