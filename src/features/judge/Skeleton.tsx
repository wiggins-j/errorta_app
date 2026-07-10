// F001-polish — generic skeleton loader.
//
// variant=verdict   → stacked rows mimicking VerdictPanel's row layout.
// variant=metrics   → grid of stat tiles mimicking MetricsDashboard's summary.

interface Props {
  variant: "verdict" | "metrics";
  rows?: number;
}

export default function Skeleton({ variant, rows = 3 }: Props) {
  const safeRows = Math.max(1, rows);
  if (variant === "metrics") {
    return (
      <div
        className="skeleton skeleton-metrics"
        role="status"
        aria-label="Loading metrics"
        data-variant="metrics"
      >
        {Array.from({ length: safeRows }).map((_, i) => (
          <div
            key={i}
            className="skeleton-row skeleton-metric-tile"
            data-testid="skeleton-row"
          />
        ))}
      </div>
    );
  }
  return (
    <div
      className="skeleton skeleton-verdict"
      role="status"
      aria-label="Loading verdict"
      data-variant="verdict"
    >
      {Array.from({ length: safeRows }).map((_, i) => (
        <div
          key={i}
          className="skeleton-row"
          data-testid="skeleton-row"
        />
      ))}
    </div>
  );
}
