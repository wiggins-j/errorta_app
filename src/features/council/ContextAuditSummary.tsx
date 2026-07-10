// F031 Phase 2 — Run-level audit summary (read-only, Phase 0/1 data only).
import type { CouncilRunAuditSummary } from "./types";

interface Props {
  summary: CouncilRunAuditSummary | null;
}

export default function ContextAuditSummary({ summary }: Props) {
  if (!summary) {
    return (
      <p className="council-empty">
        Audit summary appears here once a run is in progress.
      </p>
    );
  }
  const t = summary.totals;
  return (
    <div className="council-audit" aria-label="Run audit summary">
      <div>residency</div>
      <div>{summary.residencyOwner}</div>
      <div>status</div>
      <div>{summary.status}</div>
      <div>turns</div>
      <div>{t.turns}</div>
      <div>completed</div>
      <div>{t.completed}</div>
      <div>skipped/blocked</div>
      <div>{t.skipped} / {t.blocked}</div>
      <div>failed</div>
      <div>{t.failed}</div>
      <div>cancelled</div>
      <div>{t.cancelled}</div>
      <div>local calls</div>
      <div>{t.localCalls}</div>
      <div>fake calls</div>
      <div>{t.fakeCalls}</div>
      <div>remote calls</div>
      <div>{t.remoteCalls}</div>
      {summary.terminalReason && (
        <>
          <div>terminal reason</div>
          <div>{summary.terminalReason}</div>
        </>
      )}
    </div>
  );
}
