// F046 — Artifacts tab. Diffs/patches/test summaries produced by child runs,
// surfaced by hash + ref (no raw bytes rendered as trusted content).

import { useCallback, useEffect, useState } from "react";
import { listChildRuns } from "../../../lib/api/council";
import type { CouncilChildRun } from "../types";

interface ArtifactRow {
  key: string;
  childRunId: string;
  taskKind: string;
  kind: string;
  sha?: string;
  bytes?: number;
}

function collect(children: CouncilChildRun[]): ArtifactRow[] {
  const rows: ArtifactRow[] = [];
  for (const c of children) {
    const refs = Array.isArray(c.artifactRefs) ? c.artifactRefs : [];
    refs.forEach((ref, i) => {
      rows.push({
        key: `${c.childRunId}-${i}`,
        childRunId: c.childRunId,
        taskKind: c.taskKind,
        kind: String(ref.kind ?? ref.class_ ?? "artifact"),
        sha: (ref.content_sha256 ?? ref.sha256) as string | undefined,
        bytes: ref.bytes as number | undefined,
      });
    });
    if (c.summaryRef && typeof c.summaryRef === "object") {
      const s = c.summaryRef as Record<string, unknown>;
      rows.push({
        key: `${c.childRunId}-summary`,
        childRunId: c.childRunId,
        taskKind: c.taskKind,
        kind: "child_run_summary",
        sha: s.content_sha256 as string | undefined,
        bytes: s.payload_bytes as number | undefined,
      });
    }
  }
  return rows;
}

export default function ArtifactsTab({ runId }: { runId: string | null }) {
  const [children, setChildren] = useState<CouncilChildRun[]>([]);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!runId) {
      setChildren([]);
      return;
    }
    try {
      setChildren(await listChildRuns(runId));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [runId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  if (!runId) return <p className="work-rail-empty">No active run.</p>;
  if (error) return <p className="work-rail-error" role="alert">{error}</p>;
  const rows = collect(children);
  if (rows.length === 0)
    return <p className="work-rail-empty">No artifacts yet.</p>;

  return (
    <ul className="work-rail-list" aria-label="Artifacts">
      {rows.map((r) => (
        <li key={r.key} className="work-rail-item">
          <div className="work-rail-item-head">
            <strong>{r.kind}</strong>
            <span className="work-rail-chip">{r.taskKind}</span>
          </div>
          <div className="work-rail-meta">
            child {r.childRunId.slice(0, 10)}…
            {r.sha ? ` · ${String(r.sha).slice(0, 12)}…` : ""}
            {typeof r.bytes === "number" ? ` · ${r.bytes} B` : ""}
          </div>
        </li>
      ))}
    </ul>
  );
}
