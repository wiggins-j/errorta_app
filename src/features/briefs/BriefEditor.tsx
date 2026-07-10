// F008 Briefs — center pane: markdown editor with debounced validation.
import { useEffect, useRef, useState } from "react";
import {
  type ConnectorStatus,
  type DryRunSourceProjection,
  type ValidateResponse,
  updateBrief,
  validateBrief,
} from "../../lib/api/briefs";
import BriefHistoryDropdown from "./BriefHistoryDropdown";
import DryRunResults from "./DryRunResults";
import "../../styles/badges.css";

interface Props {
  briefId: string;
  initialMarkdown: string;
  initialParseErrors?: Array<Record<string, unknown>>;
  /** When set, this projection map is rendered as DryRunResults under the
   *  connectors block. Owned by the parent so /briefs/{id}/validate?dry_run=true
   *  responses from BriefControls' "Validate (preview)" flow are surfaced. */
  dryRunProjections?: Record<string, DryRunSourceProjection> | null;
}

const DEBOUNCE_MS = 500;

function describeError(err: Record<string, unknown>): string {
  const message =
    (typeof err.message === "string" && err.message) ||
    (typeof err.msg === "string" && err.msg) ||
    JSON.stringify(err);
  return message;
}

export default function BriefEditor({
  briefId,
  initialMarkdown,
  initialParseErrors,
  dryRunProjections,
}: Props) {
  const [markdown, setMarkdown] = useState<string>(initialMarkdown);
  const [parseErrors, setParseErrors] = useState<Array<Record<string, unknown>>>(
    initialParseErrors ?? [],
  );
  const [connectors, setConnectors] = useState<Record<string, ConnectorStatus>>({});
  const [localProjections, setLocalProjections] = useState<
    Record<string, DryRunSourceProjection> | null
  >(null);
  const [validating, setValidating] = useState<boolean>(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Reset when the active brief changes.
  useEffect(() => {
    setMarkdown(initialMarkdown);
    setParseErrors(initialParseErrors ?? []);
    setConnectors({});
    setSaveError(null);
  }, [briefId, initialMarkdown, initialParseErrors]);

  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, []);

  const scheduleValidate = (text: string) => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(async () => {
      setValidating(true);
      setSaveError(null);
      try {
        // Persist first (validate runs against the on-disk brief.md).
        await updateBrief(briefId, text);
        const result: ValidateResponse = await validateBrief(briefId);
        setParseErrors(result.errors ?? []);
        setConnectors(result.connectors ?? {});
        if (result.dry_run_projection) {
          setLocalProjections(result.dry_run_projection);
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setSaveError(msg);
      } finally {
        setValidating(false);
      }
    }, DEBOUNCE_MS);
  };

  const onChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const next = e.target.value;
    setMarkdown(next);
    scheduleValidate(next);
  };

  return (
    <section className="briefs-pane briefs-editor" aria-label="Brief editor">
      <div className="briefs-editor-toolbar" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <h3 style={{ margin: 0, flex: 1 }}>Brief</h3>
        <BriefHistoryDropdown
          briefId={briefId}
          onRestore={(restoredMarkdown) => {
            // Cancel any pending debounced validate so it doesn't clobber the
            // freshly-restored disk state with the stale in-flight markdown.
            if (debounceRef.current) {
              clearTimeout(debounceRef.current);
              debounceRef.current = null;
            }
            setMarkdown(restoredMarkdown);
            setParseErrors([]);
            setConnectors({});
            setSaveError(null);
            setValidating(false);
          }}
        />
      </div>
      {parseErrors.length > 0 && (
        <div className="briefs-parse-banner" role="alert">
          <strong>Parse errors</strong>
          <ul>
            {parseErrors.map((err, idx) => (
              <li key={idx}>{describeError(err)}</li>
            ))}
          </ul>
        </div>
      )}
      {saveError && (
        <div className="briefs-parse-banner" role="alert">
          {saveError}
        </div>
      )}
      <textarea
        value={markdown}
        onChange={onChange}
        spellCheck={false}
        aria-label="Brief markdown"
      />
      <div>
        <h3>Connectors {validating && <span className="briefs-list-item-meta">checking…</span>}</h3>
        {Object.keys(connectors).length === 0 ? (
          <div className="briefs-list-item-meta">No connector status yet.</div>
        ) : (
          Object.entries(connectors).map(([name, status]) => (
            <div key={name} className="briefs-connector-row">
              <span className={`pin-badge ${status.ok ? "pin-pinned" : "pin-absent"}`}>
                {status.ok ? "OK" : "ERROR"}
              </span>
              <strong>{name}</strong>
              {status.reason && (
                <span className="briefs-list-item-meta">{status.reason}</span>
              )}
            </div>
          ))
        )}
      </div>
      <DryRunResults projections={dryRunProjections ?? localProjections} />
    </section>
  );
}
