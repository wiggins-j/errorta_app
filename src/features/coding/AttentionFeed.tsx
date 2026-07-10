// F117 — the unified "what needs me" feed: Problems (showstoppers) first, then
// Alerts (advisories). Reads GET /attention and resolves via POST /resolve.
import { useCallback, useEffect, useState } from "react";

import * as api from "../../lib/api/coding";

type ResolveExtra = { suggestionId?: string; correctionText?: string };

function CorrectionBox({
  onSubmit,
  disabled,
}: {
  onSubmit: (text: string) => void;
  disabled: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  if (!open) {
    return (
      <button type="button" className="attn-link" onClick={() => setOpen(true)}>
        Provide correction
      </button>
    );
  }
  return (
    <div className="attn-correction">
      <textarea
        aria-label="Correction"
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={2}
      />
      <button
        type="button"
        disabled={disabled || text.trim().length === 0}
        onClick={() => onSubmit(text.trim())}
      >
        Submit correction
      </button>
    </div>
  );
}

// F120: human-readable labels for the member-health failure reasons.
const MEMBER_HEALTH_REASON_LABELS: Record<string, string> = {
  auth_failed: "Not logged in",
  binary_missing: "CLI not found",
  model_rejected: "Model unavailable",
  timeout: "Timed out",
  rate_limited: "Rate-limited",
  unparseable: "No usable output",
  errored: "Provider error",
};

function str(v: unknown): string {
  return v == null ? "" : String(v);
}

// Structured member/task failure detail shared by member-health and F127 worker
// capability Problems.
function MemberHealthDetail({ signal }: { signal: api.AttentionSignal }) {
  const ctx = signal.context ?? {};
  const reason = str(ctx.reason);
  const reasonLabel = MEMBER_HEALTH_REASON_LABELS[reason] ?? reason ?? "Problem";
  const member = str(ctx.member_id);
  const route = str(ctx.gateway_route_id);
  const role = str(ctx.coding_role);
  const remediation = str(ctx.remediation);
  const taskId = str(ctx.task_id);
  return (
    <dl className="attn-member-health">
      <div>
        <dt>Member</dt>
        <dd>
          {member || "unknown"}
          {role ? ` (${role})` : ""}
          {route ? ` — ${route}` : ""}
        </dd>
      </div>
      <div>
        <dt>Reason</dt>
        <dd className="attn-reason">{reasonLabel}</dd>
      </div>
      {taskId ? (
        <div>
          <dt>Task</dt>
          <dd>{taskId}</dd>
        </div>
      ) : null}
      {remediation ? (
        <div>
          <dt>Fix</dt>
          <dd className="attn-remediation">{remediation}</dd>
        </div>
      ) : null}
    </dl>
  );
}

function ProblemCard({
  signal,
  busy,
  onResolve,
  onOpenProviderSettings,
  onOpenRoomSettings,
}: {
  signal: api.AttentionSignal;
  busy: boolean;
  onResolve: (action: api.AttentionAction, extra?: ResolveExtra) => void;
  onOpenProviderSettings?: (signal: api.AttentionSignal) => void;
  onOpenRoomSettings?: (signal: api.AttentionSignal) => void;
}) {
  const isMemberHealth = signal.source === "member_health";
  const isWorkerUnproductive = signal.source === "worker_unproductive";
  const isConfigurationProblem = isMemberHealth || isWorkerUnproductive;
  const visibleSuggestions = signal.suggestions.filter(
    (suggestion) =>
      !(isMemberHealth && suggestion.id === "open_provider_settings") &&
      !(isWorkerUnproductive && suggestion.id === "edit_room"),
  );
  return (
    <article
      className="attn-card attn-problem"
      role={signal.blocking ? "alert" : undefined}
      aria-label={`Problem: ${signal.title}`}
    >
      <header>
        <span className="attn-tag attn-tag-problem">Problem</span>
        {signal.blocking ? <span className="attn-badge">stage paused</span> : null}
        <strong>{signal.title}</strong>
      </header>
      <p className="attn-summary">{signal.summary}</p>
      {isConfigurationProblem ? <MemberHealthDetail signal={signal} /> : null}
      {signal.pmEvaluation ? <p className="attn-eval">{signal.pmEvaluation}</p> : null}
      {isMemberHealth && onOpenProviderSettings ? (
        <div className="attn-actions attn-member-health-actions">
          <button
            type="button"
            disabled={busy}
            onClick={() => onOpenProviderSettings(signal)}
          >
            Open provider settings
          </button>
        </div>
      ) : null}
      {isWorkerUnproductive && onOpenRoomSettings ? (
        <div className="attn-actions attn-member-health-actions">
          <button
            type="button"
            disabled={busy}
            onClick={() => onOpenRoomSettings(signal)}
          >
            Edit room
          </button>
        </div>
      ) : null}
      <ul className="attn-suggestions">
        {visibleSuggestions.map((s) => (
          <li key={s.id}>
            <button
              type="button"
              disabled={busy}
              onClick={() => onResolve("accept", { suggestionId: s.id })}
            >
              Accept: {s.label}
            </button>
            {s.detail ? <span className="attn-detail">{s.detail}</span> : null}
          </li>
        ))}
      </ul>
      <CorrectionBox
        disabled={busy}
        onSubmit={(text) => onResolve("correct", { correctionText: text })}
      />
    </article>
  );
}

// F120-04: the pre-run preflight blocked-start banner. Rendered above the feed
// when a /run start was refused because a provider is logged-out / missing.
export interface PreflightUnhealthy {
  provider: string;
  route: string;
  reason: string;
  detail: string;
  remediation: string;
  memberIds: string[];
}

export function PreflightBlockedBanner({
  unhealthy,
  onOpenProviderSettings,
  onDismiss,
}: {
  unhealthy: PreflightUnhealthy[];
  onOpenProviderSettings?: () => void;
  onDismiss?: () => void;
}) {
  if (unhealthy.length === 0) return null;
  return (
    <section
      className="attn-preflight-banner"
      role="alert"
      aria-label="Can't start: providers not ready"
    >
      <strong>Can't start: one or more providers aren&apos;t ready.</strong>
      <ul>
        {unhealthy.map((u) => (
          <li key={`${u.provider}:${u.route}`}>
            <span className="attn-preflight-provider">{u.route || u.provider}</span>{" "}
            <span className="attn-reason">
              {MEMBER_HEALTH_REASON_LABELS[u.reason] ?? u.reason}
            </span>
            {u.memberIds.length > 0 ? (
              <span className="attn-preflight-members">
                {" "}
                (used by {u.memberIds.join(", ")})
              </span>
            ) : null}
            {u.remediation ? (
              <span className="attn-remediation"> — {u.remediation}</span>
            ) : null}
          </li>
        ))}
      </ul>
      <div className="attn-actions">
        {onOpenProviderSettings ? (
          <button type="button" onClick={onOpenProviderSettings}>
            Open provider settings
          </button>
        ) : null}
        {onDismiss ? (
          <button type="button" onClick={onDismiss}>
            Dismiss
          </button>
        ) : null}
      </div>
    </section>
  );
}

function AlertCard({
  signal,
  busy,
  onResolve,
}: {
  signal: api.AttentionSignal;
  busy: boolean;
  onResolve: (action: api.AttentionAction, extra?: ResolveExtra) => void;
}) {
  return (
    <article className="attn-card attn-alert" aria-label={`Alert: ${signal.title}`}>
      <header>
        <span className="attn-tag attn-tag-alert">Alert</span>
        <strong>{signal.title}</strong>
      </header>
      <p className="attn-summary">{signal.summary}</p>
      <div className="attn-actions">
        <CorrectionBox
          disabled={busy}
          onSubmit={(text) => onResolve("correct", { correctionText: text })}
        />
        <button type="button" disabled={busy} onClick={() => onResolve("accept")}>
          Accept
        </button>
        <button type="button" disabled={busy} onClick={() => onResolve("defer")}>
          Defer to PM
        </button>
        <button type="button" disabled={busy} onClick={() => onResolve("dismiss")}>
          Dismiss
        </button>
      </div>
    </article>
  );
}

export default function AttentionFeed({
  projectId,
  onChange,
  onOpenProviderSettings,
  onOpenRoomSettings,
}: {
  projectId: string;
  onChange?: () => void;
  // F120: invoked when a member-health Problem's "Open provider settings"
  // action is clicked, so the shell can navigate to Settings → Providers.
  onOpenProviderSettings?: (signal: api.AttentionSignal) => void;
  onOpenRoomSettings?: (signal: api.AttentionSignal) => void;
}) {
  const [list, setList] = useState<api.AttentionList | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setList(await api.getAttention(projectId, { state: "open" }));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const resolve = useCallback(
    async (signalId: string, action: api.AttentionAction, extra?: ResolveExtra) => {
      setBusy(signalId);
      try {
        await api.resolveSignal(projectId, signalId, { action, ...extra });
        await load();
        onChange?.();
      } catch (e) {
        // F123: an already-resolved signal (409 "not open") means it was
        // resolved elsewhere — just refresh, don't surface an error. Otherwise
        // show the backend's structured reason, not a bare status code.
        if (e instanceof api.ResolveSignalError && e.status === 409) {
          await load();
        } else {
          setError(e instanceof Error ? e.message : String(e));
        }
      } finally {
        setBusy(null);
      }
    },
    [projectId, load, onChange],
  );

  if (!list) return null;

  // F123: newest first within each group (stable tiebreak on id).
  const byNewest = (a: api.AttentionSignal, b: api.AttentionSignal) => {
    const at = Date.parse(a.createdAt) || 0;
    const bt = Date.parse(b.createdAt) || 0;
    if (bt !== at) return bt - at;
    return a.id < b.id ? 1 : a.id > b.id ? -1 : 0;
  };
  const showstoppers = list.signals
    .filter((s) => s.kind === "problem" && s.blocking)
    .sort(byNewest);
  const alerts = list.signals
    .filter((s) => s.kind === "alert" || !s.blocking)
    .sort(byNewest);
  const total = list.signals.length;
  const hasShowstoppers = showstoppers.length > 0;

  // Bulk actions apply only to ALERT-kind signals: accept/defer/dismiss are the
  // valid alert actions, whereas non-blocking problems mixed into this group
  // only support accept/correct. Resolve each in turn, ignore already-resolved
  // races, then refresh once.
  const bulkAlerts = alerts.filter((s) => s.kind === "alert");
  const resolveAllAlerts = async (action: api.AttentionAction) => {
    if (bulkAlerts.length === 0 || bulkBusy) return;
    setBulkBusy(true);
    setError(null);
    try {
      for (const s of bulkAlerts) {
        try {
          await api.resolveSignal(projectId, s.id, { action });
        } catch (e) {
          if (!(e instanceof api.ResolveSignalError && e.status === 409)) throw e;
        }
      }
      await load();
      onChange?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBulkBusy(false);
    }
  };

  return (
    <section className="attention-feed" aria-label="Attention">
      {error ? <p className="attn-error">{error}</p> : null}
      {total === 0 ? (
        <details className="coding-panel attn-panel">
          <summary>
            <span className="attn-panel-title">Needs attention</span>
            <span className="coding-count">0</span>
          </summary>
          <p className="attn-empty">Nothing needs you right now.</p>
        </details>
      ) : (
        <details className="coding-panel attn-panel" open={hasShowstoppers}>
          <summary>
            <span className="attn-panel-title">Needs attention</span>
            <span className="coding-count">{total}</span>
          </summary>

          {/* Showstoppers — blocking Problems, on top and visible when present. */}
          <details className="attn-subpanel attn-subpanel-problems" open={hasShowstoppers}>
            <summary>
              <span className="attn-subpanel-title">Showstoppers</span>
              <span className="coding-count">{showstoppers.length}</span>
            </summary>
            {showstoppers.length === 0 ? (
              <p className="attn-subpanel-empty">No showstoppers.</p>
            ) : (
              showstoppers.map((s) => (
                <ProblemCard
                  key={s.id}
                  signal={s}
                  busy={busy === s.id}
                  onResolve={(action, extra) => resolve(s.id, action, extra)}
                  onOpenProviderSettings={onOpenProviderSettings}
                  onOpenRoomSettings={onOpenRoomSettings}
                />
              ))
            )}
          </details>

          {/* Alerts — advisory, ignorable (collapsed by default). */}
          <details className="attn-subpanel attn-subpanel-alerts">
            <summary>
              <span className="attn-subpanel-title">Alerts</span>
              <span className="coding-count">{alerts.length}</span>
            </summary>
            {bulkAlerts.length > 0 ? (
              <div className="attn-bulk-actions" role="group" aria-label="Resolve all alerts">
                <button
                  type="button"
                  disabled={bulkBusy}
                  onClick={() => void resolveAllAlerts("accept")}
                >
                  Accept All
                </button>
                <button
                  type="button"
                  disabled={bulkBusy}
                  onClick={() => void resolveAllAlerts("defer")}
                >
                  Defer All
                </button>
                <button
                  type="button"
                  disabled={bulkBusy}
                  onClick={() => void resolveAllAlerts("dismiss")}
                >
                  Dismiss All
                </button>
              </div>
            ) : null}
            {alerts.length === 0 ? (
              <p className="attn-subpanel-empty">No alerts.</p>
            ) : (
              alerts.map((s) => (
                s.kind === "problem" ? (
                  <ProblemCard
                    key={s.id}
                    signal={s}
                    busy={busy === s.id || bulkBusy}
                    onResolve={(action, extra) => resolve(s.id, action, extra)}
                    onOpenProviderSettings={onOpenProviderSettings}
                  />
                ) : (
                  <AlertCard
                    key={s.id}
                    signal={s}
                    busy={busy === s.id || bulkBusy}
                    onResolve={(action, extra) => resolve(s.id, action, extra)}
                  />
                )
              ))
            )}
          </details>
        </details>
      )}
    </section>
  );
}
