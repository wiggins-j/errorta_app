// F001-deepen-01 — first-class verdict-diff panel.
//
// NO NETWORK — purely presentational. This component must not call fetch,
// hit any API client, or trigger side effects. All data is passed in via
// props; the parent (features/judge/index.tsx) is responsible for fetches.
import type { PriorVerdictPayload, Verdict } from "../../lib/api/judge";

interface Props {
  current: Verdict;
  /** Full prior list (newest first). When empty the empty-state renders. */
  priors: PriorVerdictPayload[];
  /** Which prior the diff is rendered against. Defaults to 0 (most recent). */
  selectedIndex?: number;
  /** Called when the prior-picker selection changes (only when priors.length >= 2). */
  onSelectPrior?: (index: number) => void;
}

function arrow(prev: string, curr: string): string {
  const a = (prev || "").toLowerCase();
  const b = (curr || "").toLowerCase();
  if (a === b) return "=";
  return `${a || "?"} → ${b || "?"}`;
}

function deltaClass(delta: number): string {
  if (delta > 0) return "delta-pos";
  if (delta < 0) return "delta-neg";
  return "delta-zero";
}

function formatDelta(delta: number): string {
  const pct = Math.round(delta * 100);
  if (pct === 0) return "±0";
  return pct > 0 ? `+${pct}` : `${pct}`;
}

function formatCreated(raw?: string | null): string {
  if (!raw) return "earlier";
  // Render just the date portion in the picker; full ISO surfaces under it.
  try {
    return new Date(raw).toLocaleString();
  } catch {
    return raw;
  }
}

export default function VerdictDiff({
  current,
  priors,
  selectedIndex = 0,
  onSelectPrior,
}: Props) {
  // Empty-state: no prior exists — explain the wedge in-line.
  if (priors.length === 0) {
    return (
      <section
        className="verdict-diff verdict-diff-empty"
        aria-label="Compared to your last run"
      >
        <header className="verdict-diff-header">
          <h3>Compared to your last run</h3>
        </header>
        <p className="verdict-diff-empty-copy">
          Re-run this prompt to see how the verdict changes.
        </p>
      </section>
    );
  }

  const idx = Math.min(Math.max(0, selectedIndex), priors.length - 1);
  const priorPayload = priors[idx];
  const prior = priorPayload.verdict;

  const priorTags = new Set((prior?.failure_tags ?? []) as string[]);
  const currTags = new Set((current.failure_tags ?? []) as string[]);
  const addedTags = [...currTags].filter((t) => !priorTags.has(t));
  const removedTags = [...priorTags].filter((t) => !currTags.has(t));

  const priorConf = typeof prior?.confidence === "number" ? prior.confidence : 0;
  const currConf = typeof current.confidence === "number" ? current.confidence : 0;
  const delta = currConf - priorConf;

  return (
    <section className="verdict-diff" aria-label="Compared to your last run">
      <header className="verdict-diff-header">
        <h3>Compared to your last run</h3>
        {priors.length >= 2 && (
          <label className="verdict-diff-picker">
            <span className="visually-hidden">Pick a prior verdict</span>
            <select
              value={idx}
              onChange={(e) => onSelectPrior?.(Number(e.target.value))}
              aria-label="Pick a prior verdict"
            >
              {priors.map((p, i) => (
                <option key={i} value={i}>
                  {formatCreated(p.created_at)} · {p.judge_model ?? "(unknown)"}
                </option>
              ))}
            </select>
          </label>
        )}
      </header>

      <div className="verdict-diff-grid">
        <div className="verdict-diff-col">
          <div className="verdict-diff-label">Rating</div>
          <div className="verdict-diff-rating">
            {arrow(prior?.rating ?? "", current.rating ?? "")}
          </div>
        </div>
        <div className="verdict-diff-col">
          <div className="verdict-diff-label">Confidence</div>
          <div className={`verdict-diff-delta ${deltaClass(delta)}`}>
            {formatDelta(delta)}
            <span className="verdict-diff-delta-suffix">pp</span>
          </div>
        </div>
      </div>

      {(addedTags.length > 0 || removedTags.length > 0) && (
        <div className="verdict-diff-tags">
          {addedTags.map((t) => (
            <span key={`a-${t}`} className="tag-added">
              + {t}
            </span>
          ))}
          {removedTags.map((t) => (
            <span key={`r-${t}`} className="tag-removed">
              − {t}
            </span>
          ))}
        </div>
      )}

      <div className="verdict-diff-reasons">
        <div className="verdict-diff-reason-col">
          <div className="verdict-diff-label">Prior reason</div>
          <div className="verdict-diff-reason">{prior?.reason || "(no reason)"}</div>
        </div>
        <div className="verdict-diff-reason-col">
          <div className="verdict-diff-label">Current reason</div>
          <div className="verdict-diff-reason">{current.reason || "(no reason)"}</div>
        </div>
      </div>
    </section>
  );
}
