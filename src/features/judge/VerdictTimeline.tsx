// F001-deepen-03 — colored-dot timeline for a corrected prompt's verdict history.
import { useState } from "react";
import type { VerdictTimelineEntry } from "../../lib/api/judge";

interface Props {
  entries: VerdictTimelineEntry[];
}

function ratingClass(rating: string | undefined | null): string {
  const r = (rating || "").toLowerCase();
  if (r === "pass" || r === "partial" || r === "fail") return r;
  return "unknown";
}

function formatDate(iso: string | undefined | null): string {
  if (!iso) return "unknown date";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  // Human-readable: e.g. "Jun 8, 2026, 12:34 PM"
  try {
    return d.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function tooltipFor(entry: VerdictTimelineEntry): string {
  const parts: string[] = [];
  parts.push(`rating: ${entry.rating || "unknown"}`);
  if (entry.judge_model) parts.push(`judge: ${entry.judge_model}`);
  if (entry.created_at) parts.push(entry.created_at);
  if (entry.reason_snippet) parts.push(`— ${entry.reason_snippet}`);
  return parts.join("\n");
}

export default function VerdictTimeline({ entries }: Props) {
  const [selectedIdx, setSelectedIdx] = useState<number | null>(null);

  if (!entries || entries.length === 0) {
    return (
      <div className="verdict-timeline-empty" data-testid="verdict-timeline-empty">
        No verdict history yet.
      </div>
    );
  }

  const selected = selectedIdx !== null ? entries[selectedIdx] : null;

  return (
    <div>
      <div className="verdict-timeline" data-testid="verdict-timeline" role="list">
        {entries.map((e, i) => {
          const cls = ratingClass(e.rating);
          const isSelected = selectedIdx === i;
          return (
            <button
              key={`${e.created_at || "n"}-${i}`}
              type="button"
              role="listitem"
              className={`verdict-dot ${cls}${isSelected ? " selected" : ""}`}
              title={tooltipFor(e)}
              aria-label={`Verdict ${i + 1}: ${e.rating || "unknown"} on ${formatDate(e.created_at)}${e.judge_model ? " via " + e.judge_model : ""}`}
              onClick={() =>
                setSelectedIdx((cur) => (cur === i ? null : i))
              }
              data-testid="verdict-dot"
              data-rating={cls}
            />
          );
        })}
      </div>
      {selected && (
        <div className="verdict-timeline-detail" data-testid="verdict-timeline-detail">
          <div>
            <strong>{(selected.rating || "unknown").toUpperCase()}</strong>
          </div>
          <div className="verdict-timeline-detail-meta">
            {selected.judge_model ? `judge: ${selected.judge_model}` : "judge: —"}
            {selected.created_at ? ` · ${selected.created_at}` : ""}
          </div>
          {selected.reason_snippet ? (
            <div className="verdict-timeline-detail-reason">
              {selected.reason_snippet}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
