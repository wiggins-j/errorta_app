// F046 — Child runs tab (F042). Statuses + cancel (cancelling the run cascades
// to outstanding child runs).

import { useCallback, useEffect, useState } from "react";
import { cancelRun, listChildRuns } from "../../../lib/api/council";
import type { CouncilChildRun } from "../types";

export default function ChildRunsTab({ runId }: { runId: string | null }) {
  const [children, setChildren] = useState<CouncilChildRun[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);

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
    if (!runId) return;
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, [refresh, runId]);

  const outstanding = children.some(
    (c) => c.status === "queued" || c.status === "running",
  );

  const onCancel = useCallback(async () => {
    if (!runId) return;
    setCancelling(true);
    try {
      await cancelRun(runId);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCancelling(false);
    }
  }, [runId, refresh]);

  if (!runId) return <p className="work-rail-empty">No active run.</p>;
  if (error) return <p className="work-rail-error" role="alert">{error}</p>;
  if (children.length === 0)
    return <p className="work-rail-empty">No child runs.</p>;

  return (
    <div>
      {outstanding && (
        <button
          type="button"
          className="work-rail-cancel"
          disabled={cancelling}
          onClick={onCancel}
          data-testid="cancel-children"
        >
          Cancel run (stops outstanding child runs)
        </button>
      )}
      <ul className="work-rail-list" aria-label="Child runs">
        {children.map((c) => (
          <li key={c.childRunId} className="work-rail-item">
            <div className="work-rail-item-head">
              <strong>{c.title || c.taskKind}</strong>
              <span className={`work-rail-status status-${c.status}`}>
                {c.status}
              </span>
            </div>
            <div className="work-rail-meta">
              {c.taskKind} · {c.memberId}
              {c.failure?.reason_code ? ` · ${String(c.failure.reason_code)}` : ""}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
