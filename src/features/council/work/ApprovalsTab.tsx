// F046 — Approvals tab. Lists F041 pending decisions and wires Approve/Reject.

import { useCallback, useEffect, useState } from "react";
import {
  approvePendingDecision,
  listPendingDecisions,
  rejectPendingDecision,
} from "../../../lib/api/council";
import type { CouncilPendingDecision } from "../types";

export default function ApprovalsTab({ runId }: { runId: string | null }) {
  const [decisions, setDecisions] = useState<CouncilPendingDecision[]>([]);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!runId) {
      setDecisions([]);
      return;
    }
    try {
      setDecisions(await listPendingDecisions(runId, "pending"));
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

  const act = useCallback(
    async (decisionId: string, approve: boolean) => {
      if (!runId) return;
      setBusyId(decisionId);
      try {
        if (approve) await approvePendingDecision(runId, decisionId);
        else await rejectPendingDecision(runId, decisionId);
        await refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusyId(null);
      }
    },
    [runId, refresh],
  );

  if (!runId) return <p className="work-rail-empty">No active run.</p>;
  if (error) return <p className="work-rail-error" role="alert">{error}</p>;
  if (decisions.length === 0)
    return <p className="work-rail-empty">No pending approvals.</p>;

  return (
    <ul className="work-rail-list" aria-label="Pending approvals">
      {decisions.map((d) => (
        <li key={d.decisionId} className="work-rail-item">
          <div className="work-rail-item-head">
            <strong>{d.reasonCode}</strong>
            <span className="work-rail-chip">{d.phase}</span>
          </div>
          <div className="work-rail-meta">
            {d.riskClass ? `risk: ${d.riskClass} · ` : ""}
            {String(d.safeRequest.tool_id ?? d.requester.member_id ?? "")}
          </div>
          <div className="work-rail-actions">
            <button
              type="button"
              disabled={busyId === d.decisionId}
              onClick={() => act(d.decisionId, true)}
              data-testid={`approve-${d.decisionId}`}
            >
              Allow once
            </button>
            <button
              type="button"
              disabled={busyId === d.decisionId}
              onClick={() => act(d.decisionId, false)}
              data-testid={`reject-${d.decisionId}`}
            >
              Deny
            </button>
          </div>
        </li>
      ))}
    </ul>
  );
}
