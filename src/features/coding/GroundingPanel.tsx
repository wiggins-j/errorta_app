// F088-10 — Coding project grounding panel.
//
// Surfaces a project's corpus binding (which the backend + client already fully
// model but nothing rendered): mode + corpus id + index health, the grounding
// capabilities (honest local-vs-remote), an in-progress build, a retrieval probe
// to confirm the corpus is actually served, and a post-create binding editor.
// Self-fetching so it owns its own load/poll/refresh lifecycle; the api module is
// imported directly and mocked in tests.
import { useCallback, useEffect, useId, useRef, useState } from "react";

import CorpusPicker from "../corpus/CorpusPicker";
import { listCorpora } from "../../lib/api/corpus";
import type { CorpusSummary } from "../../lib/api/corpus";
import * as api from "../../lib/api/coding";
import type {
  GroundingBootstrapJob,
  GroundingRetrieveResult,
  PmWorkingMemoryStatus,
  ProjectCorpusBinding,
  ProjectGroundingCapabilities,
} from "../../lib/api/coding";

const HEALTH_LABEL: Record<string, string> = {
  ready: "Ready",
  indexing: "Indexing",
  stale: "Stale",
  failed: "Failed",
  missing: "Not indexed",
};

const TERMINAL_JOB = new Set(["done", "failed", "interrupted"]);
const BUILD_POLL_MS = 2000;

function formatTime(value: string | null): string {
  if (!value) return "never";
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? new Date(parsed).toLocaleString() : "unknown";
}

function truncate(text: string, max = 240): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

export interface GroundingPanelProps {
  projectId: string;
  repoPath?: string | null;
  /** A live run is reading the current binding — editing is disabled while running. */
  running?: boolean;
  /** Called after a binding change so the container can reload project state. */
  onChanged?: () => void;
}

export default function GroundingPanel({
  projectId,
  running = false,
  onChanged,
}: GroundingPanelProps) {
  const [binding, setBinding] = useState<ProjectCorpusBinding | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState(false);
  const bodyId = useId();

  const loadBinding = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setBinding(await api.getCorpusBinding(projectId));
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load grounding");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void loadBinding();
  }, [loadBinding]);

  if (loading && !binding) {
    return (
      <section className="coding-grounding" aria-label="Grounding">
        <p className="cg-muted">Loading grounding…</p>
      </section>
    );
  }

  if (error && !binding) {
    return (
      <section className="coding-grounding" aria-label="Grounding">
        <p className="cg-error" role="alert">
          {error}
        </p>
        <button type="button" onClick={() => void loadBinding()}>
          Retry
        </button>
      </section>
    );
  }

  const b = binding;
  const bound = b != null && b.mode !== "none" && Boolean(b.corpusId);
  const health = b?.healthState ?? "missing";
  const healthLabel = HEALTH_LABEL[health] ?? health;
  const summary = bound
    ? `${b?.mode} corpus ${b?.corpusId}`
    : "No corpus bound";

  return (
    <section className="coding-grounding" aria-label="Grounding">
      <header className="cg-header">
        <button
          type="button"
          className="cg-toggle"
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={() => setExpanded((v) => !v)}
        >
          <span className={`cg-status-dot cg-status-dot-${health}`} aria-hidden="true" />
          <span className="cg-toggle-copy">
            <span className="cg-toggle-title-row">
              <span className="cg-summary-label">Project grounding</span>
              <span className={`cg-badge cg-badge-${health}`}>
                {healthLabel}
              </span>
            </span>
            <span className="cg-summary-text">{summary}</span>
          </span>
          <span className="cg-toggle-action">
            <span>{expanded ? "Hide" : "Settings"}</span>
            <span className="cg-chevron" aria-hidden="true" />
          </span>
        </button>
      </header>

      {expanded && (
        <div className="cg-body" id={bodyId}>
          {b && (
            <BindingBlock
              binding={b}
              bound={bound}
              running={running}
              projectId={projectId}
              onSaved={async () => {
                await loadBinding();
                onChanged?.();
              }}
            />
          )}
          <PmMemoryStatusBlock projectId={projectId} />
          {b && b.healthState === "indexing" && b.bootstrapJobId && (
            <BuildProgressBlock
              projectId={projectId}
              jobId={b.bootstrapJobId}
              onSettled={() => void loadBinding()}
            />
          )}
          <CapabilitiesBlock projectId={projectId} />
          <RetrievalProbe projectId={projectId} bound={bound} />
        </div>
      )}
    </section>
  );
}

function PmMemoryStatusBlock({ projectId }: { projectId: string }) {
  const [status, setStatus] = useState<PmWorkingMemoryStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .getPmWorkingMemoryStatus(projectId)
      .then((value) => {
        if (alive) setStatus(value);
      })
      .catch((e) => {
        if (alive) setErr(e instanceof Error ? e.message : "unavailable");
      });
    return () => {
      alive = false;
    };
  }, [projectId]);

  if (err) {
    return (
      <div role="group" className="cg-block" aria-label="PM working memory">
        <h4>PM memory</h4>
        <p className="cg-muted">Unavailable: {err}</p>
      </div>
    );
  }
  if (!status) {
    return (
      <div role="group" className="cg-block" aria-label="PM working memory">
        <h4>PM memory</h4>
        <p className="cg-muted">Loading…</p>
      </div>
    );
  }
  return (
    <div role="group" className="cg-block" aria-label="PM working memory">
      <h4>PM memory</h4>
      <p>
        <span className="cg-badge">{status.status}</span>{" "}
        <span className="cg-muted">
          {status.corpusId ? `corpus ${status.corpusId}` : "local ledger memory"}
        </span>
      </p>
      <dl className="cg-kv">
        <dt>Memory ref</dt>
        <dd>{status.memoryRef ?? "none"}</dd>
        <dt>AIAR mirror</dt>
        <dd>{status.aiarMirrorStatus}</dd>
        <dt>AIAR retrieval</dt>
        <dd>{status.aiarRetrievalStatus}</dd>
        <dt>Last generated</dt>
        <dd>{formatTime(status.lastGeneratedAt)}</dd>
      </dl>
      {status.warnings.length > 0 && (
        <ul className="cg-notes">
          {status.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function BindingBlock({
  binding,
  bound,
  running,
  projectId,
  onSaved,
}: {
  binding: ProjectCorpusBinding;
  bound: boolean;
  running: boolean;
  projectId: string;
  onSaved: () => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState<null | "build" | "refresh">(null);
  const [err, setErr] = useState<string | null>(null);
  const projectCorpus = binding.mode === "build_from_project";

  const build = async () => {
    setBusy("build");
    setErr(null);
    try {
      await api.buildCorpusFromProject(projectId);
      await onSaved();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "failed to build corpus");
    } finally {
      setBusy(null);
    }
  };
  const refresh = async () => {
    setBusy("refresh");
    setErr(null);
    try {
      await api.refreshProjectCorpus(projectId);
      await onSaved();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "failed to refresh corpus");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div role="group" className="cg-block" aria-label="Corpus binding">
      <h4>Binding</h4>
      <dl className="cg-kv">
        <dt>Mode</dt>
        <dd>{binding.mode}</dd>
        <dt>Corpus</dt>
        <dd>{binding.corpusId ?? "—"}</dd>
        <dt>Health</dt>
        <dd>
          <span className={`cg-badge cg-badge-${binding.healthState}`}>
            {HEALTH_LABEL[binding.healthState] ?? binding.healthState}
          </span>{" "}
          <span className="cg-muted">{binding.healthReason}</span>
        </dd>
        <dt>Last refresh</dt>
        <dd>{formatTime(binding.lastRefreshAt)}</dd>
        <dt>Index version</dt>
        <dd>{binding.indexVersion}</dd>
      </dl>
      {!editing && (
        <div className="cg-binding-actions">
          {/* The marquee path: index the team's own code so the PM/devs retrieve
              the project itself. No external repo path needed. Also offered when a
              build_from_project corpus is bound but not yet built (health ≠ ready)
              so the user can populate / re-create it — e.g. the remote instance
              404s because the team hasn't merged code yet. */}
          {(!bound || (projectCorpus && binding.healthState !== "ready")) && (
            <button
              type="button"
              disabled={running || busy !== null}
              title={running ? "Stop the run to build the corpus" : undefined}
              onClick={() => void build()}
            >
              {busy === "build" ? "Building…" : "Build a corpus from this project"}
            </button>
          )}
          {projectCorpus && binding.healthState === "ready" && (
            <button
              type="button"
              disabled={running || busy !== null}
              title={running ? "Stop the run to refresh the corpus" : undefined}
              onClick={() => void refresh()}
            >
              {busy === "refresh" ? "Refreshing…" : "Refresh corpus from project"}
            </button>
          )}
          <button
            type="button"
            disabled={running || busy !== null}
            title={running ? "Stop the run to change the corpus binding" : undefined}
            onClick={() => setEditing(true)}
          >
            {bound ? "Edit binding" : "Attach an existing corpus"}
          </button>
        </div>
      )}
      {err && (
        <p className="cg-error" role="alert">
          {err}
        </p>
      )}
      {editing && (
        <BindingEditor
          projectId={projectId}
          running={running}
          current={binding}
          onCancel={() => setEditing(false)}
          onSaved={async () => {
            setEditing(false);
            await onSaved();
          }}
        />
      )}
    </div>
  );
}

function BindingEditor({
  projectId,
  running,
  current,
  onCancel,
  onSaved,
}: {
  projectId: string;
  running: boolean;
  current: ProjectCorpusBinding;
  onCancel: () => void;
  onSaved: () => Promise<void>;
}) {
  const bound = current.mode !== "none" && Boolean(current.corpusId);
  // Two real choices only: make a NEW corpus from this project's own code, or
  // attach an EXISTING corpus. (Legacy/external-repo bindings collapse to "new"
  // so the dropdown can never land on an unrepresentable value -> no 422.)
  type EditMode = "existing" | "build_from_project";
  const [mode, setMode] = useState<EditMode>(
    current.mode === "existing" ? "existing" : "build_from_project",
  );
  // For build_from_project the corpus id defaults to the conventional
  // `project-<slug>` so the user never has to invent one.
  const projectSlug = projectId.toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "");
  const [corpusId, setCorpusId] = useState(
    current.corpusId ?? (current.mode === "existing" ? "" : `project-${projectSlug}`),
  );
  const [corpora, setCorpora] = useState<CorpusSummary[]>([]);
  const [corporaLoading, setCorporaLoading] = useState(false);
  const [saving, setSaving] = useState<null | "save" | "remove">(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setCorporaLoading(true);
    listCorpora()
      .then(setCorpora)
      .catch(() => setCorpora([]))
      .finally(() => setCorporaLoading(false));
  }, []);

  const onModeChange = (next: EditMode) => {
    setMode(next);
    if (next === "build_from_project" && !corpusId.trim()) setCorpusId(`project-${projectSlug}`);
    if (next === "existing" && corpusId === `project-${projectSlug}`) setCorpusId("");
  };

  const invalid: string | null =
    mode === "existing" && !corpusId.trim()
      ? "Pick a corpus."
      : mode === "build_from_project" && !corpusId.trim()
        ? "Enter a corpus id."
        : null;

  const save = async () => {
    setSaving("save");
    setErr(null);
    try {
      await api.putCorpusBinding(projectId, {
        mode,
        corpusId: corpusId.trim() || null,
        sourceRoot: null,
      });
      await onSaved();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "failed to save binding");
    } finally {
      setSaving(null);
    }
  };

  const remove = async () => {
    setSaving("remove");
    setErr(null);
    try {
      await api.putCorpusBinding(projectId, { mode: "none", corpusId: null, sourceRoot: null });
      await onSaved();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "failed to remove corpus");
    } finally {
      setSaving(null);
    }
  };

  return (
    <div role="group" className="cg-editor" aria-label="Edit binding">
      <label>
        Corpus source
        <select value={mode} onChange={(e) => onModeChange(e.target.value as EditMode)}>
          <option value="build_from_project">Create new from this project</option>
          <option value="existing">Use an existing corpus</option>
        </select>
      </label>
      {mode === "build_from_project" && (
        <>
          <label>
            Corpus id
            <input value={corpusId} onChange={(e) => setCorpusId(e.target.value)} />
          </label>
          <p className="cg-muted">
            Indexes this project&apos;s own merged code into the configured AIAR
            (remote example-host over the tunnel, if set). No source path — the
            source is the project itself. Save, then{" "}
            <strong>Build a corpus from this project</strong> populates it once
            the team has merged code.
          </p>
        </>
      )}
      {mode === "existing" && (
        <CorpusPicker
          label="Existing corpus"
          value={corpusId}
          onChange={setCorpusId}
          corpora={corpora}
          loading={corporaLoading}
          allowEmpty
          emptyLabel="— pick a corpus —"
        />
      )}
      {running && (
        <p className="cg-muted" role="status">
          A run is active — stop it to change the binding.
        </p>
      )}
      {err && (
        <p className="cg-error" role="alert">
          {err}
        </p>
      )}
      <div className="cg-editor-actions">
        <button
          type="button"
          onClick={() => void save()}
          disabled={saving !== null || running || invalid !== null}
          title={invalid ?? undefined}
        >
          {saving === "save" ? "Saving…" : "Save binding"}
        </button>
        <button type="button" onClick={onCancel} disabled={saving !== null}>
          Cancel
        </button>
        {bound && (
          <button
            type="button"
            className="cg-danger"
            onClick={() => void remove()}
            disabled={saving !== null || running}
            title="Detach the corpus from this project"
          >
            {saving === "remove" ? "Removing…" : "Remove corpus"}
          </button>
        )}
      </div>
    </div>
  );
}

function CapabilitiesBlock({ projectId }: { projectId: string }) {
  const [caps, setCaps] = useState<ProjectGroundingCapabilities | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .getGroundingCapabilities(projectId)
      .then((c) => alive && setCaps(c))
      .catch((e) => alive && setErr(e instanceof Error ? e.message : "unavailable"));
    return () => {
      alive = false;
    };
  }, [projectId]);

  if (err) {
    return (
      <div role="group" className="cg-block" aria-label="Grounding capabilities">
        <h4>Capabilities</h4>
        <p className="cg-muted">Capabilities unavailable: {err}</p>
      </div>
    );
  }
  if (!caps) {
    return (
      <div role="group" className="cg-block" aria-label="Grounding capabilities">
        <h4>Capabilities</h4>
        <p className="cg-muted">Loading…</p>
      </div>
    );
  }

  const flags: Array<[string, boolean]> = [
    ["corpus ids", caps.supportsCorpusIds],
    ["file ingest", caps.supportsFileIngest],
    ["incremental refresh", caps.supportsIncrementalRefresh],
    ["supersession", caps.supportsSupersession],
    ["export/import", caps.supportsExportImport],
    ["local-only embedding", caps.localOnlyEmbedding],
  ];

  return (
    <div role="group" className="cg-block" aria-label="Grounding capabilities">
      <h4>Capabilities</h4>
      <p>
        {caps.available ? "Available" : "Unavailable"} ·{" "}
        <span className="cg-muted">
          {caps.source || "unknown"}
          {caps.version ? ` ${caps.version}` : ""}
        </span>
      </p>
      <ul className="cg-flags">
        {flags.map(([label, on]) => (
          <li key={label} className={on ? "cg-flag-on" : "cg-flag-off"}>
            {on ? "✓" : "✗"} {label}
          </li>
        ))}
      </ul>
      {caps.notes.length > 0 && (
        <ul className="cg-notes">
          {caps.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function BuildProgressBlock({
  projectId,
  jobId,
  onSettled,
}: {
  projectId: string;
  jobId: string;
  onSettled: () => void;
}) {
  const [job, setJob] = useState<GroundingBootstrapJob | null>(null);
  const [gone, setGone] = useState(false);
  // Keep onSettled in a ref so it never re-triggers the polling effect — the
  // container re-renders ~every 2.5s with a fresh inline callback, which would
  // otherwise tear down and restart the poll (and defeat the settle-once guard).
  const onSettledRef = useRef(onSettled);
  onSettledRef.current = onSettled;

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let settled = false;

    const settle = () => {
      if (!settled) {
        settled = true;
        onSettledRef.current();
      }
    };

    const poll = async () => {
      try {
        const j = await api.getBootstrapJob(projectId, jobId);
        if (!alive) return;
        if (j === null) {
          // The job 404s — deleted/evicted/never persisted. Stop polling and
          // settle once rather than polling forever showing "queued".
          setGone(true);
          settle();
          return;
        }
        setJob(j);
        if (TERMINAL_JOB.has(j.status)) {
          settle();
          return; // stop polling once terminal
        }
      } catch {
        // transient — keep polling
      }
      if (alive) timer = setTimeout(() => void poll(), BUILD_POLL_MS);
    };
    void poll();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [projectId, jobId]);

  return (
    <div role="group" className="cg-block" aria-label="Build progress">
      <h4>Building corpus index</h4>
      <p aria-live="polite">
        {gone ? (
          <>Status: <strong>job not found</strong> — refresh to recheck.</>
        ) : (
          <>
            Status: <strong>{job?.status ?? "queued"}</strong>
            {job ? ` · ${job.documentsIngested} docs · ${job.chunksAdded} chunks` : ""}
          </>
        )}
      </p>
      {job && job.errors.length > 0 && (
        <ul className="cg-errors">
          {job.errors.slice(0, 5).map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function RetrievalProbe({ projectId, bound }: { projectId: string; bound: boolean }) {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<GroundingRetrieveResult | null>(null);
  const [busy, setBusy] = useState(false);

  // Clear stale hits/empty-state when the binding changes (e.g. none -> existing).
  useEffect(() => {
    setResult(null);
  }, [projectId, bound]);

  const run = async () => {
    const q = query.trim();
    if (!q) return;
    setBusy(true);
    try {
      setResult(await api.retrieveProjectCorpus(projectId, q, 6));
    } catch {
      setResult({ status: "unavailable", hits: [] });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div role="group" className="cg-block" aria-label="Retrieval probe">
      <h4>Test retrieval</h4>
      <div className="cg-probe-row">
        <input
          aria-label="Retrieval query"
          value={query}
          placeholder="Ask the bound corpus a question…"
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void run();
          }}
        />
        <button type="button" onClick={() => void run()} disabled={busy || !bound}>
          {busy ? "Retrieving…" : "Retrieve"}
        </button>
      </div>
      {!bound && <p className="cg-muted">Bind a corpus to test retrieval.</p>}
      {result && <RetrievalResult result={result} />}
    </div>
  );
}

function RetrievalResult({ result }: { result: GroundingRetrieveResult }) {
  if (result.status === "no_corpus") {
    return <p className="cg-muted">No corpus is bound to this project.</p>;
  }
  if (result.status === "unavailable") {
    return (
      <p className="cg-muted" role="status">
        Retrieval is unavailable — the corpus index isn’t ready or the grounding
        backend can’t be reached.
      </p>
    );
  }
  if (result.status === "empty" || result.hits.length === 0) {
    return (
      <p className="cg-muted" role="status">
        No matches in the corpus for that query.
      </p>
    );
  }
  return (
    <ul className="cg-hits" aria-label="Retrieval results">
      {result.hits.map((h, i) => (
        <li key={`${h.chunkId}-${i}`} className="cg-hit">
          <div className="cg-hit-meta">
            <code>{h.corpusId}</code>
            <span className="cg-muted">chunk {h.chunkId}</span>
            {h.score != null && (
              <span className="cg-muted">score {h.score.toFixed(3)}</span>
            )}
          </div>
          <p className="cg-hit-content">{truncate(h.content)}</p>
        </li>
      ))}
    </ul>
  );
}
