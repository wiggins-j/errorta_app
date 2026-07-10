// F-WEDGE-DEEPEN-V1 — Judge replay tab.
//
// Replays every non-accepted verdict in the active corpus through the
// current pipeline and surfaces score deltas in a sortable table.
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import type { ReplayResult } from "../../lib/api/judge";
import { replayCorpusStream } from "../../lib/api/judge";
import { listCorpora } from "../../lib/api/onboarding";
import EmptyState from "./EmptyState";

interface Props {
  corpus: string | null | undefined;
  onCorpusChange?: (name: string | null) => void;
}

type SortDir = "asc" | "desc";

function truncate(s: string, n = 60): string {
  if (!s) return "";
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

function deltaClass(delta: number): string {
  if (delta > 0) return "delta-pos";
  if (delta < 0) return "delta-neg";
  return "delta-zero";
}

function improvementPct(r: ReplayResult): number {
  // score_delta is bounded [-1, 1]; show as percentage points.
  return Math.round(r.score_delta * 100);
}

export default function JudgeReplay({ corpus, onCorpusChange }: Props) {
  const [results, setResults] = useState<ReplayResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [dryRun, setDryRun] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [openRow, setOpenRow] = useState<number | null>(null);
  const [hasRun, setHasRun] = useState(false);
  const [corpora, setCorpora] = useState<string[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await listCorpora();
        if (!cancelled) {
          setCorpora(res.corpora.map((c) => c.name));
        }
      } catch {
        // Non-fatal: leave the dropdown empty if the lookup fails.
        if (!cancelled) setCorpora([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const sorted = useMemo(() => {
    const copy = [...results];
    copy.sort((a, b) =>
      sortDir === "desc"
        ? b.score_delta - a.score_delta
        : a.score_delta - b.score_delta,
    );
    return copy;
  }, [results, sortDir]);

  const runReplay = async () => {
    if (!corpus) {
      setError("Pick a corpus first.");
      return;
    }
    setError(null);
    setLoading(true);
    setOpenRow(null);
    setResults([]);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await replayCorpusStream(
        corpus,
        (r) => setResults((prev) => [...prev, r]),
        { dryRun, signal: controller.signal },
      );
      setHasRun(true);
    } catch (e) {
      if ((e as { name?: string })?.name === "AbortError") {
        setHasRun(true);
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setLoading(false);
      abortRef.current = null;
    }
  };

  const cancelReplay = () => {
    abortRef.current?.abort();
  };

  const handleCorpusSelect = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const v = e.target.value;
    onCorpusChange?.(v === "" ? null : v);
  };

  const showEmpty = hasRun && !loading && results.length === 0;

  return (
    <div className="judge-replay" data-testid="judge-replay">
      <div className="judge-replay-controls">
        <label>
          {"Corpus: "}
          <select
            value={corpus ?? ""}
            onChange={handleCorpusSelect}
            data-testid="corpus-select"
          >
            <option value="">— select —</option>
            {corpora.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label>
          <input
            type="checkbox"
            checked={dryRun}
            onChange={(e) => setDryRun(e.target.checked)}
            data-testid="dry-run-toggle"
          />
          {" Dry-run (preview only, no pipeline calls)"}
        </label>
        <button
          type="button"
          onClick={runReplay}
          disabled={loading || !corpus}
          data-testid="replay-button"
        >
          {loading ? "Replaying…" : "Replay All"}
        </button>
        {loading && (
          <button
            type="button"
            onClick={cancelReplay}
            data-testid="cancel-button"
          >
            Cancel
          </button>
        )}
      </div>

      {!corpus && (
        <p className="judge-replay-hint" data-testid="judge-replay-hint">
          Select a corpus above and click Replay All to compare verdict changes
          across your dataset.
        </p>
      )}

      {error && (
        <div role="alert" className="judge-replay-error">
          {error}
        </div>
      )}

      {showEmpty && (
        <EmptyState
          title="Nothing to replay"
          message="No verdicts in this corpus — run a few prompts first."
        />
      )}

      {results.length > 0 && (
        <table className="judge-replay-table" data-testid="replay-table">
          <thead>
            <tr>
              <th scope="col" aria-label="Row expansion toggle" />
              <th scope="col">Prompt</th>
              <th scope="col">Original</th>
              <th scope="col">Replay</th>
              <th
                scope="col"
                role="button"
                tabIndex={0}
                className="judge-replay-sort"
                onClick={() =>
                  setSortDir((d) => (d === "desc" ? "asc" : "desc"))
                }
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    if (e.key === " ") e.preventDefault();
                    setSortDir((d) => (d === "desc" ? "asc" : "desc"));
                  }
                }}
                data-testid="sort-improvement"
              >
                Δ improvement {sortDir === "desc" ? "▼" : "▲"}
              </th>
              <th scope="col">Grounding</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((r, i) => {
              const pct = improvementPct(r);
              const isOpen = openRow === i;
              const detailRowId = `replay-diff-row-${i}`;
              return (
                <Fragment key={`replay-${i}`}>
                  <tr
                    onClick={() => setOpenRow(isOpen ? null : i)}
                    aria-expanded={isOpen}
                    data-testid={`replay-row-${i}`}
                  >
                    <td className="judge-replay-chevron">
                      <button
                        type="button"
                        aria-expanded={isOpen}
                        aria-controls={detailRowId}
                        aria-label={isOpen ? "Collapse row details" : "Expand row details"}
                        data-testid={`expand-button-${i}`}
                        onClick={(e) => {
                          e.stopPropagation();
                          setOpenRow(isOpen ? null : i);
                        }}
                        style={{
                          background: "transparent",
                          border: "none",
                          cursor: "pointer",
                          padding: 0,
                          font: "inherit",
                        }}
                      >
                        <span
                          className={`chevron ${isOpen ? "chevron-open" : ""}`}
                          aria-hidden="true"
                        >
                          {"▶"}
                        </span>
                      </button>
                    </td>
                    <td title={r.prompt}>{truncate(r.prompt, 60)}</td>
                    <td>
                      <span
                        className={`rating-badge ${r.original_verdict.rating}`}
                      >
                        {r.original_verdict.rating}
                      </span>
                    </td>
                    <td>
                      <span
                        className={`rating-badge ${r.replay_verdict.rating || "unknown"}`}
                      >
                        {r.replay_verdict.rating || "—"}
                      </span>
                    </td>
                    <td>
                      <span
                        className={`delta-badge ${deltaClass(r.score_delta)}`}
                        data-testid={`delta-${i}`}
                      >
                        {pct > 0 ? `+${pct}` : pct}pp
                      </span>
                    </td>
                    <td>{r.grounding_change}</td>
                  </tr>
                  {isOpen && (
                    <tr
                      id={detailRowId}
                      className="judge-replay-diff-row"
                      data-testid={`replay-diff-${i}`}
                    >
                      <td colSpan={6}>
                        <div className="judge-replay-diff">
                          <div>
                            <strong>Original reason</strong>
                            <pre>{r.original_verdict.reason ?? "(none)"}</pre>
                          </div>
                          <div>
                            <strong>Replay reason</strong>
                            <pre>{r.replay_verdict.reason ?? "(none)"}</pre>
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
