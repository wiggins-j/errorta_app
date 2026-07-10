import type { MetricsTrendDay } from "../../lib/api/judge";

/**
 * Maps a pass-rate value (0..1) to a CSS custom property color token.
 *
 * Thresholds are inclusive at the boundaries:
 *   pass_rate >= 0.7  -> var(--ok)
 *   0.4 <= pass_rate  -> var(--warn)
 *   pass_rate <  0.4  -> var(--error)
 *   null              -> var(--bg-elevated)  (no-data stub)
 */
export function getBarColor(passRate: number | null): string {
  if (passRate === null) return "var(--bg-elevated)";
  if (passRate >= 0.7) return "var(--ok)";
  if (passRate >= 0.4) return "var(--warn)";
  return "var(--error)";
}

interface Props {
  data: MetricsTrendDay[];
}

const BAR_WIDTH = 35;
const BAR_GAP = 5;
const BAR_STRIDE = BAR_WIDTH + BAR_GAP; // 40
const CHART_HEIGHT = 120; // bar plot area; labels live below
const VIEW_HEIGHT = 140;
const VIEW_WIDTH = 280;

export default function PassRateChart({ data }: Props) {
  return (
    <svg
      className="pass-rate-chart"
      role="img"
      aria-label="7-day pass-rate trend chart"
      viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
      preserveAspectRatio="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      {data.map((day, index) => {
        const xOffset = index * BAR_STRIDE;
        const labelX = xOffset + BAR_WIDTH / 2;
        const mmdd = day.date.slice(5);
        const isNoData = day.pass_rate === null || day.total === 0;

        if (isNoData) {
          const stubHeight = 4;
          const stubY = CHART_HEIGHT - stubHeight; // 116
          const ariaLabel = `${day.date}: no data`;
          return (
            <g key={day.date}>
              <rect
                x={xOffset}
                y={stubY}
                width={BAR_WIDTH}
                height={stubHeight}
                fill="var(--bg-elevated)"
                rx={2}
                aria-label={ariaLabel}
              >
                <title>{ariaLabel}</title>
              </rect>
              <text
                x={labelX}
                y={132}
                textAnchor="middle"
                fontSize={10}
                fill="currentColor"
                opacity={0.7}
              >
                {mmdd}
              </text>
            </g>
          );
        }

        const passRate = day.pass_rate as number;
        const barHeight = passRate * 100; // px within 120 chart area
        const y = CHART_HEIGHT - barHeight;
        const pct = Math.round(passRate * 100);
        const info = `${day.date}: ${pct}% pass rate (${day.pass}/${day.total})`;
        const color = getBarColor(passRate);

        return (
          <g key={day.date}>
            <rect
              x={xOffset}
              y={y}
              width={BAR_WIDTH}
              height={barHeight}
              fill={color}
              rx={2}
              aria-label={info}
            >
              <title>{info}</title>
            </rect>
            <text
              x={labelX}
              y={132}
              textAnchor="middle"
              fontSize={10}
              fill="currentColor"
              opacity={0.7}
            >
              {mmdd}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
