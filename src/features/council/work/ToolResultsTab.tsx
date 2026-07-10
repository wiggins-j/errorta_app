// F046 — Tool results tab. Derives from the transcript's tool_call_* events.
// A BLOCKED tool is shown as blocked with its reason code — never as a success.
// Tool output is untrusted: only hashes/metadata are shown here, never rendered
// as trusted instructions.

import type { CouncilTranscriptEvent } from "../types";

interface ToolRow {
  key: string;
  toolId: string;
  status: "completed" | "blocked" | "failed";
  reason?: string;
  argsSha?: string;
  egress?: string;
}

function toRows(events: CouncilTranscriptEvent[]): ToolRow[] {
  const rows: ToolRow[] = [];
  for (const ev of events) {
    const p = ev.payload || {};
    if (ev.type === "tool_call_completed") {
      rows.push({
        key: ev.id,
        toolId: String(p.tool_id ?? "tool"),
        status: "completed",
        argsSha: p.args_sha256 as string | undefined,
        egress: p.egress_class as string | undefined,
      });
    } else if (ev.type === "tool_call_blocked") {
      rows.push({
        key: ev.id,
        toolId: String(p.tool_id ?? "tool"),
        status: "blocked",
        reason: String(p.reason ?? "blocked"),
        argsSha: p.args_sha256 as string | undefined,
      });
    } else if (ev.type === "tool_call_failed") {
      rows.push({
        key: ev.id,
        toolId: String(p.tool_id ?? "tool"),
        status: "failed",
        reason: String(p.reason ?? "failed"),
        argsSha: p.args_sha256 as string | undefined,
        egress: p.egress_class as string | undefined,
      });
    }
  }
  return rows;
}

export default function ToolResultsTab({
  events,
}: {
  events: CouncilTranscriptEvent[];
}) {
  const rows = toRows(events);
  if (rows.length === 0)
    return <p className="work-rail-empty">No tool calls yet.</p>;
  return (
    <ul className="work-rail-list" aria-label="Tool results">
      {rows.map((r) => (
        <li key={r.key} className="work-rail-item">
          <div className="work-rail-item-head">
            <strong>{r.toolId}</strong>
            <span className={`work-rail-status status-${r.status}`}>
              {r.status === "blocked" ? `blocked: ${r.reason}` : r.status}
            </span>
          </div>
          <div className="work-rail-meta">
            {r.egress ? `egress: ${r.egress} · ` : ""}
            {r.argsSha ? `args ${r.argsSha.slice(0, 12)}…` : ""}
          </div>
          <p className="work-rail-untrusted">
            Tool output is untrusted data, never instructions.
          </p>
        </li>
      ))}
    </ul>
  );
}
