// F039 — review + human-accept gate for the auto-apply merge-back.
//
// When a run used code_write auto_apply, its proposed patch lives in an
// ISOLATED git workspace and never touches the user's files. This panel is the
// human gate: it shows the proposed changes + conflicts and only writes to the
// user's tree on an explicit "Apply to my files" click.
import { useCallback, useEffect, useState } from "react";

import {
  acceptApplyWorkspace,
  getApplyWorkspace,
  type ApplyWorkspacePreview,
  type ApplyWorkspaceResult,
} from "../../lib/api/council";

interface Props {
  runId: string;
}

export default function ApplyWorkspacePanel({ runId }: Props) {
  const [preview, setPreview] = useState<ApplyWorkspacePreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ApplyWorkspaceResult | null>(null);
  const [applying, setApplying] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setPreview(await getApplyWorkspace(runId));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    void load();
  }, [load]);

  const apply = useCallback(
    async (allowConflicts: boolean) => {
      setApplying(true);
      setError(null);
      try {
        setResult(await acceptApplyWorkspace(runId, { allowConflicts }));
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setApplying(false);
      }
    },
    [runId, load],
  );

  if (loading) return null;
  // No auto-apply workspace for this run, or nothing changed — render nothing.
  if (!preview || !preview.hasChanges) return null;

  const hasConflicts = preview.conflicts.length > 0;

  return (
    <section className="apply-workspace-panel" aria-label="Proposed file changes">
      <h4>Proposed changes to your files</h4>
      <p className="aw-source">
        These edits were made in an isolated copy and have <strong>not</strong>{" "}
        touched your files. Source: <code>{preview.source}</code>
      </p>

      <ul className="aw-changes" data-testid="apply-changed-files">
        {preview.changedFiles.map((c) => (
          <li key={c.path}>
            <span className={`aw-status aw-status-${c.status}`}>{c.status}</span>{" "}
            <code>{c.path}</code>
            {preview.conflicts.includes(c.path) && (
              <span className="aw-conflict" data-testid="apply-conflict-flag">
                {" "}
                — conflict: you edited this file since the run started
              </span>
            )}
          </li>
        ))}
      </ul>

      {preview.diff && (
        <details className="aw-diff">
          <summary>View full diff</summary>
          <pre data-testid="apply-diff">{preview.diff}</pre>
        </details>
      )}

      {result?.applied && (
        <p className="aw-applied" role="status" data-testid="apply-applied">
          Applied {result.written.length} file(s) to your tree
          {result.deleted.length > 0
            ? `, deleted ${result.deleted.length}`
            : ""}
          .
        </p>
      )}
      {error && (
        <p className="aw-error" role="alert" data-testid="apply-error">
          {error}
        </p>
      )}

      <div className="aw-actions">
        {!hasConflicts && (
          <button
            type="button"
            onClick={() => void apply(false)}
            disabled={applying || Boolean(result?.applied)}
            data-testid="apply-to-files"
          >
            Apply to my files
          </button>
        )}
        {hasConflicts && (
          <button
            type="button"
            className="aw-force"
            onClick={() => void apply(true)}
            disabled={applying || Boolean(result?.applied)}
            data-testid="apply-overwrite"
          >
            Overwrite conflicting files
          </button>
        )}
      </div>
    </section>
  );
}
