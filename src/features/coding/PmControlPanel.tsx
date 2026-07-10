// F145 — the PM Changes review affordance. Anything the PM changes via any
// surface (ask-PM, give-directive, or the AI Wizard) surfaces here to accept or
// revert. The conversation itself lives in the existing "ask PM" / "give
// directive" composer, which now carries the same capability + agency as the
// Wizard (the backend applies grounded control-actions and records them here).
import { useCallback, useEffect, useState } from "react";

import * as api from "../../lib/api/coding";
import PmChangesModal from "./PmChangesModal";

export default function PmControlPanel({ projectId }: { projectId: string }) {
  const [pendingCount, setPendingCount] = useState(0);
  const [changesOpen, setChangesOpen] = useState(false);

  const refreshCount = useCallback(async () => {
    try {
      setPendingCount((await api.listPmChanges(projectId)).length);
    } catch {
      /* non-fatal */
    }
  }, [projectId]);

  useEffect(() => {
    void refreshCount();
    const t = setInterval(() => void refreshCount(), 4000);
    return () => clearInterval(t);
  }, [refreshCount]);

  if (pendingCount === 0 && !changesOpen) return null;

  return (
    <div className="coding-pm-changes-bar">
      <button
        type="button"
        className="coding-btn coding-pm-changes-btn"
        onClick={() => setChangesOpen(true)}
      >
        Review PM Changes ({pendingCount})
      </button>
      {changesOpen ? (
        <PmChangesModal
          projectId={projectId}
          onClose={() => {
            setChangesOpen(false);
            void refreshCount();
          }}
        />
      ) : null}
    </div>
  );
}
