// F145 — the "PM Changes" review: Accept keeps / Decline reverts.
import { useCallback, useEffect, useState } from "react";

import * as api from "../../lib/api/coding";

export default function PmChangesModal({
  projectId,
  onClose,
}: {
  projectId: string;
  onClose: () => void;
}) {
  const [pending, setPending] = useState<api.PmChange[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setPending(await api.listPmChanges(projectId));
    } catch {
      setError("Couldn't load PM changes.");
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function resolve(changeId: string, action: "accept" | "decline") {
    setBusy(changeId);
    setError(null);
    try {
      await api.resolvePmChange(projectId, changeId, action);
      await load();
    } catch {
      setError(`Couldn't ${action} the change.`);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="coding-modal-backdrop" role="dialog" aria-label="PM Changes" aria-modal="true">
      <div className="coding-modal">
        <header className="coding-modal-head">
          <h2>PM Changes</h2>
          <button type="button" className="coding-btn coding-btn-ghost" onClick={onClose}>
            Close
          </button>
        </header>
        {error ? <p className="coding-field-error" role="alert">{error}</p> : null}
        {pending.length === 0 ? (
          <p className="coding-field-hint">No pending changes.</p>
        ) : (
          <ul aria-label="Pending PM changes">
            {pending.map((c) => (
              <li key={c.changeId} className="coding-pmc-row">
                <strong>{c.summary}</strong>
                <ul>
                  {c.details.map((d, i) => (
                    <li key={i} className="coding-field-hint">
                      {d.field}: {String(d.before)} → {String(d.after)}
                    </li>
                  ))}
                </ul>
                {c.autonomy?.warning ? (
                  <p className="coding-wizard-nudge" role="note">
                    This makes the run <strong>autonomous</strong> — it won't pause to ask you.
                    {c.autonomy.suggested_cap != null
                      ? ` Call cap: ${c.autonomy.suggested_cap}.`
                      : " No call cap (unlimited)."}
                  </p>
                ) : null}
                <div className="coding-pmc-actions">
                  <button
                    type="button"
                    className="coding-btn"
                    disabled={busy === c.changeId}
                    onClick={() => resolve(c.changeId, "accept")}
                  >
                    Accept
                  </button>
                  <button
                    type="button"
                    className="coding-btn coding-btn-ghost"
                    disabled={busy === c.changeId}
                    onClick={() => resolve(c.changeId, "decline")}
                  >
                    Decline (revert)
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
