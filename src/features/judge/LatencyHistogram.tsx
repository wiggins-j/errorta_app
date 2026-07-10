import type { LatencyHistogram as LatencyHistogramData } from "../../lib/api/judge";

interface Props {
  data?: LatencyHistogramData | null;
}

const VIEW_WIDTH = 280;
const CHART_HEIGHT = 120;
const VIEW_HEIGHT = 160;
const BAR_GAP = 4;
const BUCKET_COUNT = 6;
const BAR_WIDTH = (VIEW_WIDTH - BAR_GAP * (BUCKET_COUNT + 1)) / BUCKET_COUNT;
// Maximum ms value on the marker x-axis. Markers (p50/p95/p99) past this
// are clamped to the right edge.
const AXIS_MAX_MS = 2000;

const EMPTY_MESSAGE = "Run prompts to populate.";

function hasData(data?: LatencyHistogramData | null): boolean {
  if (!data || !Array.isArray(data.buckets) || data.buckets.length === 0) {
    return false;
  }
  return data.buckets.some((b) => (b.count ?? 0) > 0);
}

function markerX(ms: number): number {
  const clamped = Math.max(0, Math.min(AXIS_MAX_MS, ms));
  return (clamped / AXIS_MAX_MS) * VIEW_WIDTH;
}

export default function LatencyHistogram({ data }: Props) {
  if (!hasData(data)) {
    return (
      <div
        className="latency-histogram-empty"
        role="status"
        style={{ color: "#666", fontSize: "0.85rem", padding: "0.5rem 0" }}
      >
        {EMPTY_MESSAGE}
      </div>
    );
  }

  const histogram = data as LatencyHistogramData;
  const counts = histogram.buckets.map((b) => b.count ?? 0);
  const maxCount = Math.max(...counts, 1);

  const markers: Array<{ key: string; label: string; ms: number }> = [];
  if (histogram.p50_ms !== null && histogram.p50_ms !== undefined) {
    markers.push({ key: "p50", label: "p50", ms: histogram.p50_ms });
  }
  if (histogram.p95_ms !== null && histogram.p95_ms !== undefined) {
    markers.push({ key: "p95", label: "p95", ms: histogram.p95_ms });
  }
  if (histogram.p99_ms !== null && histogram.p99_ms !== undefined) {
    markers.push({ key: "p99", label: "p99", ms: histogram.p99_ms });
  }

  return (
    <svg
      className="latency-histogram"
      role="img"
      aria-label="Judge latency histogram"
      viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
      preserveAspectRatio="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      {histogram.buckets.map((bucket, index) => {
        const count = bucket.count ?? 0;
        const ratio = count / maxCount;
        const barHeight = ratio * CHART_HEIGHT;
        const x = BAR_GAP + index * (BAR_WIDTH + BAR_GAP);
        const y = CHART_HEIGHT - barHeight;
        const label = `${bucket.label} ms: ${count} verdict${count === 1 ? "" : "s"}`;
        return (
          <g key={bucket.label}>
            <rect
              x={x}
              y={y}
              width={BAR_WIDTH}
              height={barHeight}
              fill="var(--accent)"
              rx={2}
              aria-label={label}
              data-testid={`latency-bar-${bucket.label}`}
            >
              <title>{label}</title>
            </rect>
            <text
              x={x + BAR_WIDTH / 2}
              y={CHART_HEIGHT + 12}
              textAnchor="middle"
              fontSize={9}
              fill="currentColor"
              opacity={0.7}
            >
              {bucket.label}
            </text>
          </g>
        );
      })}
      {markers.map((m, i) => {
        const x = markerX(m.ms);
        const textY = CHART_HEIGHT + 28 + (i % 2) * 12;
        const labelText = `${m.label}: ${Math.round(m.ms)} ms`;
        return (
          <g key={m.key} data-testid={`latency-marker-${m.key}`}>
            <line
              x1={x}
              x2={x}
              y1={0}
              y2={CHART_HEIGHT}
              stroke="currentColor"
              strokeOpacity={0.6}
              strokeDasharray="3 2"
              strokeWidth={1}
              aria-label={labelText}
            >
              <title>{labelText}</title>
            </line>
            <text
              x={x}
              y={textY}
              textAnchor="middle"
              fontSize={10}
              fill="currentColor"
            >
              {labelText}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
