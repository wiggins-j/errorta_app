// F001 Judge — verdict review UX entry point.
import { useRef, useState } from "react";
import type { PriorVerdictPayload, VerdictResponse } from "../../lib/api/judge";
import CorrectionEditor from "./CorrectionEditor";
import JudgeModelPicker from "./JudgeModelPicker";
import JudgeReplay from "./JudgeReplay";
import MetricsDashboard from "./MetricsDashboard";
import { consumePendingPrompt } from "./pendingPrompt";
import PromptRunner from "./PromptRunner";
import { ToastProvider } from "./toast";
import VerdictDiff from "./VerdictDiff";
import VerdictPanel from "./VerdictPanel";
import "./judge.css";

interface Props {
  /** Optional corpus identifier from the upstream Corpus tab. */
  corpus?: string | null;
}

type JudgeTab = "metrics" | "replay";

const TAB_ORDER: JudgeTab[] = ["metrics", "replay"];

export default function JudgeFeature({ corpus }: Props = {}) {
  const [result, setResult] = useState<VerdictResponse | null>(null);
  const [judgeModel, setJudgeModel] = useState<string | null>(null);
  const [metricsRefresh, setMetricsRefresh] = useState<number>(0);
  const [priors, setPriors] = useState<PriorVerdictPayload[]>([]);
  const [selectedPriorIndex, setSelectedPriorIndex] = useState<number>(0);
  const [tab, setTab] = useState<JudgeTab>("metrics");
  const [selectedCorpus, setSelectedCorpus] = useState<string | null>(
    corpus ?? null,
  );
  // F109 — one-shot handoff from the welcome "Suggested prompt" Run. Consumed
  // exactly once on mount (read-and-clear) so a later navigation to Judge does
  // not re-prefill a stale prompt.
  const [initialPrompt] = useState<string | null>(() => consumePendingPrompt());

  const handleResult = (r: VerdictResponse) => {
    setResult(r);
    // Reset the prior list — PromptRunner's onPriors will repopulate.
    setPriors([]);
    setSelectedPriorIndex(0);
  };

  return (
    <ToastProvider>
      <div className="judge-feature feature-pane">
        <div className="judge-header">
          <h2>Judge</h2>
          <p className="judge-subtitle">
            Ask a question, get a graded verdict, and teach Errorta from your
            corrections.
          </p>
        </div>

        <JudgeModelPicker onModelChange={setJudgeModel} />

        <PromptRunner
          judgeModel={judgeModel}
          corpus={selectedCorpus}
          initialPrompt={initialPrompt}
          onResult={handleResult}
          onPriors={(p) => {
            setPriors(p);
            setSelectedPriorIndex(0);
          }}
        />

        {result && (
          <>
            <div>
              <strong>Answer</strong>
              <div className="answer-panel">{result.answer || "(empty)"}</div>
            </div>
            {/* First-class labeled wedge surface — sits ABOVE VerdictPanel. */}
            <VerdictDiff
              current={result.verdict}
              priors={priors}
              selectedIndex={selectedPriorIndex}
              onSelectPrior={setSelectedPriorIndex}
            />
            <VerdictPanel verdict={result.verdict} />
            {result.prior_correction && (
              <div className="answer-panel" style={{ background: "#f5f3ff" }}>
                <strong>Prior accepted correction for this prompt:</strong>
                {"\n"}
                {result.prior_correction}
              </div>
            )}
            <CorrectionEditor
              verdictId={result.id}
              answer={result.answer}
              verdict={result.verdict}
              onAccepted={() => setMetricsRefresh((n) => n + 1)}
            />
          </>
        )}

        <JudgeTabList tab={tab} onChange={setTab} />

        {tab === "metrics" && <MetricsDashboard refreshKey={metricsRefresh} />}
        {tab === "replay" && (
          <JudgeReplay
            corpus={selectedCorpus}
            onCorpusChange={setSelectedCorpus}
          />
        )}
      </div>
    </ToastProvider>
  );
}

function JudgeTabList({
  tab,
  onChange,
}: {
  tab: JudgeTab;
  onChange: (t: JudgeTab) => void;
}) {
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const focusTab = (index: number) => {
    const next = TAB_ORDER[index];
    onChange(next);
    // Roving tabindex: move focus to the newly active tab.
    setTimeout(() => {
      tabRefs.current[index]?.focus();
    }, 0);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const currentIdx = TAB_ORDER.indexOf(tab);
    if (e.key === "ArrowRight") {
      e.preventDefault();
      focusTab((currentIdx + 1) % TAB_ORDER.length);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      focusTab((currentIdx - 1 + TAB_ORDER.length) % TAB_ORDER.length);
    } else if (e.key === "Home") {
      e.preventDefault();
      focusTab(0);
    } else if (e.key === "End") {
      e.preventDefault();
      focusTab(TAB_ORDER.length - 1);
    }
  };

  return (
    <div
      className="judge-tabs"
      role="tablist"
      aria-label="Judge section tabs"
      onKeyDown={onKeyDown}
    >
      {TAB_ORDER.map((t, i) => {
        const selected = tab === t;
        const label = t === "metrics" ? "Metrics" : "Replay";
        return (
          <button
            key={t}
            ref={(el) => {
              tabRefs.current[i] = el;
            }}
            type="button"
            role="tab"
            aria-selected={selected}
            tabIndex={selected ? 0 : -1}
            data-testid={`judge-tab-${t}`}
            onClick={() => onChange(t)}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}
