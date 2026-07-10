import { useEffect, useRef, useState } from "react";
import { fetchMetrics, type MetricsResponse } from "../../lib/api/judge";
import EmptyState from "./EmptyState";
import LatencyHistogram from "./LatencyHistogram";
import PassRateChart from "./PassRateChart";
import Skeleton from "./Skeleton";
import VerdictTimeline from "./VerdictTimeline";
import { useToast } from "./toast";

interface Props {
  refreshKey?: number;
}

function fmtRate(r: number | null | undefined): string {
  if (r === null || r === undefined) return "—";
  return `${(r * 100).toFixed(0)}%`;
}

export default function MetricsDashboard({ refreshKey = 0 }: Props) {
  const [data, setData] = useState<MetricsResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const toast = useToast();
  // Pin toast in a ref so the effect closure doesn't re-fire when provider
  // identity changes.
  const toastRef = useRef(toast);
  toastRef.current = toast;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchMetrics()
      .then((r) => {
        if (!cancelled) {
          setData(r);
          setLoading(false);
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setLoading(false);
          toastRef.current.show({
            message: "Couldn't load judge metrics.",
            details: e instanceof Error ? e.message : String(e),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  if (loading && !data) {
    return (
      <div className="metrics-dashboard">
        <h3>Metrics</h3>
        <Skeleton variant="metrics" rows={4} />
      </div>
    );
  }

  if (data && data.total === 0) {
    return (
      <div className="metrics-dashboard">
        <h3>Metrics</h3>
        <EmptyState
          title="No verdicts yet"
          message="Run a prompt above to start seeing pass/fail trends here."
        />
      </div>
    );
  }

  if (!data) {
    // Fetch failed; the toast already surfaced the error.
    return (
      <div className="metrics-dashboard">
        <h3>Metrics</h3>
        <EmptyState message="Metrics unavailable." />
      </div>
    );
  }

  const trendTotals = data.trend_7d.reduce((s, d) => s + d.total, 0);
  const trendPasses = data.trend_7d.reduce((s, d) => s + d.pass, 0);
  const trendSummary = `7-day trend: ${trendPasses} passes of ${trendTotals} verdicts across ${data.trend_7d.length} days`;

  return (
    <div className="metrics-dashboard">
      <h3>Metrics</h3>
      <div className="summary-row">
        <div className="stat">
          <span className="label">Pass rate (all-time)</span>
          <span
            className="value"
            aria-label={`${fmtRate(data.pass_rate)} pass rate all-time`}
          >
            {fmtRate(data.pass_rate)}
          </span>
        </div>
        <div className="stat">
          <span className="label">Pass rate (7d)</span>
          <span
            className="value"
            aria-label={`${fmtRate(data.pass_rate_7d)} pass rate last 7 days`}
          >
            {fmtRate(data.pass_rate_7d)}
          </span>
        </div>
        <div className="stat">
          <span className="label">Verdicts (7d)</span>
          <span
            className="value"
            aria-label={`${data.total_7d} verdicts in last 7 days`}
          >
            {data.total_7d}
          </span>
        </div>
        <div className="stat">
          <span className="label">Total</span>
          <span
            className="value"
            aria-label={`${data.total} verdicts total`}
          >
            {data.total}
          </span>
        </div>
      </div>
      <div>
        <label className="metric-label">7-day trend (pass rate)</label>
        <div
          aria-live="polite"
          style={{
            position: "absolute",
            width: 1,
            height: 1,
            padding: 0,
            margin: -1,
            overflow: "hidden",
            clip: "rect(0,0,0,0)",
            whiteSpace: "nowrap",
            border: 0,
          }}
        >
          {trendSummary}
        </div>
        <PassRateChart data={data.trend_7d} />
      </div>
      <div>
        <label className="metric-label">Judge latency</label>
        <LatencyHistogram data={data.latency_histogram ?? null} />
      </div>
      {data.most_corrected_prompts.length > 0 && (
        <div>
          <div className="label" style={{ fontSize: "0.75rem", color: "#666" }}>
            Most-corrected prompts
          </div>
          <ul className="most-corrected" style={{ listStyle: "none", padding: 0 }}>
            {data.most_corrected_prompts.map((p) => (
              <MostCorrectedCard key={p.prompt_signature || p.prompt} entry={p} />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

interface CardProps {
  entry: MetricsResponse["most_corrected_prompts"][number];
}

function MostCorrectedCard({ entry }: CardProps) {
  const [open, setOpen] = useState(false);
  const promptPreview =
    entry.prompt.slice(0, 80) + (entry.prompt.length > 80 ? "…" : "");
  return (
    <li className="most-corrected-card" data-testid="most-corrected-card">
      <button
        type="button"
        className="most-corrected-card-header"
        aria-expanded={open}
        aria-label={`${open ? "Collapse" : "Expand"} most-corrected prompt: ${entry.count} corrections, ${promptPreview}`}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="most-corrected-card-caret" aria-hidden>
          {open ? "▾" : "▸"}
        </span>
        <span className="most-corrected-card-count">{entry.count}×</span>
        <span className="most-corrected-card-prompt">{promptPreview}</span>
      </button>
      {open && (
        <div className="most-corrected-card-body">
          <VerdictTimeline entries={entry.verdict_timeline ?? []} />
        </div>
      )}
    </li>
  );
}
