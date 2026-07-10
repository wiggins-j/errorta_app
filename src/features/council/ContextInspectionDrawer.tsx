// F031 Phase 5 (Tasks 2-3) — ContextInspectionDrawer.
//
// The F031-08 inspection-drawer feed: shows one card per ContextManifest
// the router wrote for a turn. Invariant 5 (sealed) is honored — manifests
// carry only sha256s + counts + classes; the component never receives raw
// payload text from the endpoint, so it can never leak it. The fail-closed
// banner surfaces blocked_reason in red.
//
// F031-PROVENANCE-VIZ (Task 1) — sub-section renderers now live in
// ContextManifestSections.tsx so the single-card path here and the
// compare-strip path in ContextProvenanceCompare.tsx share one source
// of truth.
import { useCallback, useEffect, useRef, useState } from "react";
import type { CouncilContextManifest, CouncilTurnInspection } from "./types";
import {
  getRoundInspection,
  getStewardPacket,
  getTurnInspection,
} from "../../lib/api/council";
import {
  CitationRefsSection,
  CompactionSection,
  OmittedSection,
  PolicySection,
  shortHash,
  SourceCountsSection,
  SourceRefsSection,
  StewardSection,
  TokenEstimateSection,
  ToolResultsSection,
} from "./ContextManifestSections";
import ContextProvenanceCompare from "./ContextProvenanceCompare";

interface Props {
  runId: string;
  // Either turnId (single-turn drill-down, legacy) OR round (round-level
  // compare view — the QA P1 #1 fix). When both are provided, round wins
  // because the compare view is the marquee surface.
  turnId?: string;
  round?: number;
  memberId?: string;
  onClose: () => void;
}

function sourceIdsOf(item: unknown): string[] {
  if (!item || typeof item !== "object") return [];
  const raw = (item as Record<string, unknown>).source_event_ids;
  return Array.isArray(raw) ? raw.map(String).filter(Boolean) : [];
}

function StewardPacketAudit({ m }: { m: CouncilContextManifest }) {
  const packetId = m.steward?.packetId;
  const [packet, setPacket] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!packetId || m.steward?.fallback) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    getStewardPacket(m.runId, packetId)
      .then((p) => {
        if (!cancelled) setPacket(p);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [m.runId, packetId, m.steward?.fallback]);

  if (!packetId || m.steward?.fallback) return null;
  const goal = (
    packet?.user_goal && typeof packet.user_goal === "object"
      ? packet.user_goal
      : {}
  ) as Record<string, unknown>;
  const consensus = (
    packet?.current_consensus && typeof packet.current_consensus === "object"
      ? packet.current_consensus
      : {}
  ) as Record<string, unknown>;
  const positions = Array.isArray(packet?.member_positions)
    ? packet.member_positions
    : [];
  const disagreements = Array.isArray(packet?.open_disagreements)
    ? packet.open_disagreements
    : [];
  const questions = Array.isArray(packet?.open_questions)
    ? packet.open_questions
    : [];
  return (
    <section className="cid-section" aria-label="Steward packet audit">
      <h4>Steward packet audit</h4>
      {loading && <p className="cid-empty">Loading packet body…</p>}
      {error && (
        <p className="cid-error" role="alert">
          packet_fetch_failed: {error}
        </p>
      )}
      {!loading && !error && packet === null && (
        <p className="cid-empty">Packet body unavailable.</p>
      )}
      {packet && (
        <details>
          <summary>Packet body</summary>
          <dl className="cid-kv">
            <dt>User goal</dt>
            <dd>{String(goal.text ?? "—")}</dd>
            <dt>Goal source events</dt>
            <dd>{sourceIdsOf(goal).join(", ") || "—"}</dd>
            <dt>Current consensus</dt>
            <dd>{String(consensus.text ?? "—")}</dd>
            <dt>Consensus source events</dt>
            <dd>{sourceIdsOf(consensus).join(", ") || "—"}</dd>
            <dt>Member positions</dt>
            <dd>{positions.length}</dd>
            <dt>Open disagreements</dt>
            <dd>{disagreements.length}</dd>
            <dt>Open questions</dt>
            <dd>{questions.length}</dd>
          </dl>
          {positions.length > 0 && (
            <ul className="cid-omitted">
              {positions.map((raw, idx) => {
                const item = raw as Record<string, unknown>;
                return (
                  <li key={idx}>
                    <code>{String(item.member_id ?? `member-${idx + 1}`)}</code>
                    {" · "}
                    <span>{String(item.confidence ?? "medium")}</span>
                    {" · "}
                    {String(item.stance ?? "")}
                    {" · sources "}
                    <code>{sourceIdsOf(item).join(", ") || "—"}</code>
                  </li>
                );
              })}
            </ul>
          )}
        </details>
      )}
    </section>
  );
}

function ManifestCard({ m }: { m: CouncilContextManifest }) {
  const blocked = m.blockedReason !== null && m.blockedReason !== undefined;
  return (
    <article
      className={`cid-manifest${blocked ? " blocked" : ""}`}
      aria-label={`Manifest ${m.manifestId}`}
    >
      <header className="cid-manifest-head">
        <code title={m.manifestId}>{shortHash(m.manifestId)}</code>
        <span className="cid-member">{m.memberId}</span>
      </header>
      {blocked && (
        <div className="cid-blocked-banner" role="alert">
          Blocked: <code>{m.blockedReason}</code>
        </div>
      )}
      <PolicySection m={m} />
      <TokenEstimateSection m={m} />
      <SourceCountsSection m={m} />
      <ToolResultsSection m={m} />
      <SourceRefsSection m={m} />
      <CitationRefsSection m={m} />
      <CompactionSection m={m} />
      <StewardSection m={m} />
      <StewardPacketAudit m={m} />
      <OmittedSection m={m} />
    </article>
  );
}

export default function ContextInspectionDrawer({
  runId,
  turnId,
  round,
  memberId,
  onClose,
}: Props) {
  const [inspection, setInspection] = useState<CouncilTurnInspection | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const drawerRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    // QA P1 #1: prefer round-level fetch (returns all member manifests
    // → compare view reachable). Fall back to per-turn for legacy callers.
    const fetch =
      round !== undefined
        ? getRoundInspection(runId, round)
        : turnId !== undefined
          ? getTurnInspection(runId, turnId)
          : Promise.resolve(null);
    fetch
      .then((r) => {
        if (cancelled) return;
        setInspection(r);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(`inspection_fetch_failed: ${err?.message ?? err}`);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [runId, turnId, round]);

  // F031-DEMO-A11Y-SWEEP Task 3 — capture the originating Inspect
  // button (or whatever else had focus) so we can restore on close.
  // Per the existing `useEffect` cleanup pattern in
  // src/features/corpus/RefreshDiffModal.tsx — this is the project
  // convention; no separate utility module is introduced.
  const openerRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    openerRef.current =
      (typeof document !== "undefined"
        ? (document.activeElement as HTMLElement | null)
        : null) ?? null;
    return () => {
      const opener = openerRef.current;
      if (opener && typeof opener.focus === "function" && document.contains(opener)) {
        try {
          opener.focus();
        } catch {
          // ignore — restoration is best-effort
        }
      }
    };
  }, []);

  // QA P2 #6 (2026-06-12): the keydown effect previously had `[onClose]`
  // in its dep array. CouncilShell passes an inline lambda, so the
  // identity changes every time the parent polls (every 350ms while a
  // run is mid-flight) — re-running this effect, which both re-installs
  // the document keydown listener AND re-focuses the close button.
  // A user mid-Tab inside the drawer would get yanked back to Close.
  //
  // Fix: stash the live `onClose` in a ref so the keydown handler always
  // calls the latest one without depending on its identity. Initial
  // focus runs in its own mount-only effect.
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  // Mount-only: focus Close once when the drawer opens.
  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  // Esc + Tab/Shift+Tab focus trap. Deps are empty so the listener is
  // installed once at mount and removed at unmount — parent re-renders
  // do NOT yank focus.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (e.key === "Tab" && drawerRef.current) {
        const focusables = drawerRef.current.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
        );
        const enabled = Array.from(focusables).filter(
          (el) => !el.hasAttribute("disabled"),
        );
        if (enabled.length === 0) return;
        const first = enabled[0];
        const last = enabled[enabled.length - 1];
        const active = document.activeElement as HTMLElement | null;
        if (e.shiftKey) {
          if (active === first || !drawerRef.current.contains(active)) {
            e.preventDefault();
            last.focus();
          }
        } else {
          if (active === last) {
            e.preventDefault();
            first.focus();
          }
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const handleBackdrop = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      if (e.target === e.currentTarget) onCloseRef.current();
    },
    [],
  );

  return (
    <div
      className="cid-backdrop"
      onClick={handleBackdrop}
      role="presentation"
    >
      <div
        ref={drawerRef}
        className="cid-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="Inspection drawer"
      >
        <header className="cid-head">
          <div>
            <h3>Inspection</h3>
            <div className="cid-subtitle">
              <code>
                {round !== undefined ? `round ${round}` : (turnId ?? "")}
              </code>
              {memberId && <> · {memberId}</>}
            </div>
          </div>
          <button
            ref={closeRef}
            className="cid-close"
            onClick={onClose}
            aria-label="Close inspection drawer"
          >
            ✕
          </button>
        </header>
        {loading && <p className="cid-loading">Loading manifest…</p>}
        {error && (
          <div className="cid-error" role="alert">
            {error}
          </div>
        )}
        {!loading && !error && inspection === null && (
          <p className="cid-empty">
            No manifest yet for this turn. Run not far enough along, or
            context was blocked before write.
          </p>
        )}
        {inspection && inspection.manifests.length === 0 && (
          <p className="cid-empty">
            Inspection returned 0 manifests — should not happen on a
            completed turn. Check sidecar logs.
          </p>
        )}
        {inspection && inspection.manifests.length === 1 && (
          <ManifestCard
            key={inspection.manifests[0].manifestId}
            m={inspection.manifests[0]}
          />
        )}
        {inspection && inspection.manifests.length >= 2 && (
          <ContextProvenanceCompare
            manifests={inspection.manifests}
            focusedMemberId={memberId}
          />
        )}
      </div>
    </div>
  );
}
