// F037 expert callouts — run-time panel.
// Lets the user request a configured expert during a live run and
// approve/reject callouts that are awaiting a decision. Renders nothing
// when the room has no enabled escalation roster.
import { useCallback, useEffect, useState } from "react";

import { getRoomFull } from "../../lib/api/councilRoom";
import {
  approveCallout,
  listCallouts,
  rejectCallout,
  requestCallout,
} from "../../lib/api/council";
import type { CouncilCalloutRecord } from "./types";

interface RosterTarget {
  id: string;
  name: string;
}

interface Props {
  runId: string;
  roomId: string | null;
  live: boolean;
}

export default function CouncilCalloutPanel({ runId, roomId, live }: Props) {
  const [enabled, setEnabled] = useState(false);
  const [roster, setRoster] = useState<RosterTarget[]>([]);
  const [target, setTarget] = useState<string>("");
  const [question, setQuestion] = useState<string>("");
  const [callouts, setCallouts] = useState<CouncilCalloutRecord[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Load the escalation policy + roster from the room config.
  useEffect(() => {
    if (!roomId) return;
    let cancelled = false;
    getRoomFull(roomId)
      .then((resp) => {
        if (cancelled) return;
        const room = resp.room as Record<string, unknown>;
        const policy = (room.escalation_policy as Record<string, unknown>) ?? {};
        const list = (room.escalation_roster as Array<Record<string, unknown>>) ?? [];
        const targets = list.map((e) => ({
          id: String(e.id),
          name: String(e.name || e.id),
        }));
        setEnabled(policy.enabled === true && targets.length > 0);
        setRoster(targets);
        if (targets.length > 0) setTarget((t) => t || targets[0].id);
      })
      .catch(() => {
        if (!cancelled) setEnabled(false);
      });
    return () => {
      cancelled = true;
    };
  }, [roomId]);

  const refresh = useCallback(() => {
    listCallouts(runId)
      .then(setCallouts)
      .catch(() => undefined);
  }, [runId]);

  // Poll callouts while the run is live so approval prompts surface.
  useEffect(() => {
    if (!enabled) return;
    refresh();
    if (!live) return;
    const id = setInterval(refresh, 1200);
    return () => clearInterval(id);
  }, [enabled, live, refresh]);

  const onAsk = useCallback(async () => {
    if (!target) return;
    setBusy(true);
    setError(null);
    try {
      await requestCallout(runId, { targetId: target, question });
      setQuestion("");
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [runId, target, question, refresh]);

  const onApprove = useCallback(
    async (calloutId: string) => {
      try {
        await approveCallout(runId, calloutId);
        refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    },
    [runId, refresh],
  );

  const onReject = useCallback(
    async (calloutId: string) => {
      try {
        await rejectCallout(runId, calloutId);
        refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    },
    [runId, refresh],
  );

  if (!enabled) return null;

  const awaiting = callouts.filter((c) => c.state === "awaiting_approval");

  return (
    <section className="council-callout-panel" aria-label="Expert callouts">
      <h4 className="council-callout-title">Expert callouts</h4>

      {awaiting.length > 0 && (
        <ul className="council-callout-approvals">
          {awaiting.map((c) => (
            <li key={c.calloutId} className="council-callout-approval">
              <span>
                Approve callout to <strong>{c.targetId}</strong>?
                {c.question ? ` "${c.question}"` : ""}
              </span>
              <span className="council-callout-approval-actions">
                <button type="button" onClick={() => onApprove(c.calloutId)}>
                  Approve
                </button>
                <button type="button" onClick={() => onReject(c.calloutId)}>
                  Reject
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}

      {live && (
        <div className="council-callout-ask">
          <label className="council-callout-field">
            <span>Expert</span>
            <select
              aria-label="Expert target"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
            >
              {roster.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </label>
          <label className="council-callout-field">
            <span>Question (optional)</span>
            <input
              type="text"
              aria-label="Callout question"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="What should the expert resolve?"
            />
          </label>
          <button type="button" onClick={onAsk} disabled={busy || !target}>
            Ask expert
          </button>
        </div>
      )}

      {error && <p className="council-callout-error" role="alert">{error}</p>}
    </section>
  );
}
