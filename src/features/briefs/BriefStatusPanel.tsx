// F008 Briefs — right rail: per-source progress + collapsible compliance/failures.
// Uses EventSource to stream updates while the brief is RUNNING; falls back to
// a JSON snapshot from GET /briefs/{id}/status for any other state.
import { useEffect, useState } from "react";
import { type LiveStatus, statusBrief } from "../../lib/api/briefs";
import { getSidecarBase } from "../../lib/sidecarPort";
import { TERMINAL_STATES, type BriefStateValue } from "./types";

interface Props {
  briefId: string;
  state: BriefStateValue;
}

export default function BriefStatusPanel({ briefId, state }: Props) {
  const [snapshot, setSnapshot] = useState<LiveStatus | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);

  // Always pull a JSON snapshot when the brief or state changes so the panel
  // has something to render even before SSE delivers a frame.
  useEffect(() => {
    let cancelled = false;
    setSnapshot(null);
    setStreamError(null);
    statusBrief(briefId)
      .then((s) => {
        if (!cancelled) setSnapshot(s);
      })
      .catch((err) => {
        if (!cancelled) {
          setStreamError(err instanceof Error ? err.message : String(err));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [briefId, state]);

  // Open EventSource only while the brief is in an active state.
  useEffect(() => {
    if (state !== "RUNNING" && state !== "PAUSED") return;
    let es: EventSource | null = null;
    let cancelled = false;

    (async () => {
      const base = await getSidecarBase();
      if (cancelled) return;
      try {
        es = new EventSource(`${base}/briefs/${encodeURIComponent(briefId)}/status`);
        es.onmessage = (ev) => {
          try {
            const data = JSON.parse(ev.data) as LiveStatus;
            setSnapshot(data);
            if (
              data.state &&
              TERMINAL_STATES.has(data.state as BriefStateValue) &&
              es
            ) {
              es.close();
              es = null;
            }
          } catch {
            // ignore non-JSON keepalives
          }
        };
        es.onerror = () => {
          setStreamError("event stream interrupted");
        };
      } catch (err) {
        setStreamError(err instanceof Error ? err.message : String(err));
      }
    })();

    return () => {
      cancelled = true;
      if (es) es.close();
    };
  }, [briefId, state]);

  const currentState = snapshot?.state ?? state;
  const isTerminal =
    !!snapshot?.state && TERMINAL_STATES.has(snapshot.state as BriefStateValue);
  const liveLabel = snapshot
    ? `Brief is ${currentState}. ${snapshot.ingested_count} documents ingested.`
    : "Loading brief status…";

  return (
    <section className="briefs-pane briefs-status" aria-label="Brief status">
      <h3>Status</h3>
      {streamError && (
        <div className="briefs-parse-banner" role="alert">
          {streamError}
        </div>
      )}
      <div
        role="status"
        aria-live={isTerminal ? "assertive" : "polite"}
        aria-atomic="true"
        data-testid="brief-status-live"
      >
        {liveLabel}
      </div>
      {!snapshot ? (
        <div className="briefs-list-item-meta">Loading status…</div>
      ) : (
        <>
          <div className="briefs-list-item-meta">
            state <strong>{snapshot.state}</strong>
            {snapshot.run_id && <> · run {snapshot.run_id}</>}
            <> · ingested {snapshot.ingested_count}</>
          </div>
          {snapshot.per_source.length > 0 && (
            <table
              className="briefs-source-table"
              aria-label="Per-source collection progress"
            >
              <thead>
                <tr>
                  <th>source</th>
                  <th>state</th>
                  <th>collected</th>
                  <th>refused</th>
                </tr>
              </thead>
              <tbody>
                {snapshot.per_source.map((ps) => (
                  <tr
                    key={ps.name}
                    aria-label={`${ps.name} source: ${ps.state}, ${ps.docs_collected} collected, ${ps.docs_refused} refused`}
                  >
                    <td>{ps.name}</td>
                    <td>{ps.state}</td>
                    <td>{ps.docs_collected}</td>
                    <td>{ps.docs_refused}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <details className="briefs-collapsible">
            <summary aria-label={`Compliance refusals, ${snapshot.compliance_refusals.length} entries`}>
              Compliance refusals ({snapshot.compliance_refusals.length})
            </summary>
            <pre>{JSON.stringify(snapshot.compliance_refusals, null, 2)}</pre>
          </details>
          <details className="briefs-collapsible">
            <summary aria-label={`Failures, ${snapshot.failures.length} entries`}>
              Failures ({snapshot.failures.length})
            </summary>
            <pre>{JSON.stringify(snapshot.failures, null, 2)}</pre>
          </details>
          <details className="briefs-collapsible">
            <summary aria-label="Raw status snapshot JSON">Raw snapshot</summary>
            <pre>{JSON.stringify(snapshot, null, 2)}</pre>
          </details>
        </>
      )}
    </section>
  );
}
