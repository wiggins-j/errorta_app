// F031 Phase 5 polish (Task 1) — extracted manifest sub-section renderers.
//
// Single source of truth for the four per-manifest sections so the
// single-card (drawer) and compare paths (F031-PROVENANCE-VIZ) cannot
// drift visually. Moved verbatim from ContextInspectionDrawer.tsx —
// same DOM, same aria-labels, same cid-* class names.
import type { CouncilContextManifest } from "./types";

export function shortHash(s: string | null | undefined): string {
  if (!s) return "—";
  if (s.length <= 16) return s;
  return `${s.slice(0, 8)}…${s.slice(-4)}`;
}

export function PolicySection({ m }: { m: CouncilContextManifest }) {
  return (
    <section className="cid-section" aria-label="Effective policy">
      <h4>Effective policy</h4>
      <dl className="cid-kv">
        <dt>Context access</dt>
        <dd>
          {m.effectiveContextAccess}
          {m.requestedContextAccess !== m.effectiveContextAccess && (
            <span className="cid-narrowed">
              {" "}
              (narrowed from <code>{m.requestedContextAccess}</code>)
            </span>
          )}
        </dd>
        <dt>Transcript access</dt>
        <dd>{m.effectiveTranscriptAccess}</dd>
        <dt>Destination scope</dt>
        <dd>{m.destinationScope}</dd>
        <dt>Egress class</dt>
        <dd>{m.egressClass}</dd>
        <dt>Payload sha256</dt>
        <dd>
          <code title={m.payloadSha256}>{shortHash(m.payloadSha256)}</code>
        </dd>
        <dt>Visibility plan</dt>
        <dd>
          <code>{m.visibilityPlanId ?? "—"}</code>
        </dd>
        <dt>Packing</dt>
        <dd>
          <code>{m.packingContract ?? "v1"}</code>
          {" · "}
          <code>{m.packingOrderVariant ?? "default"}</code>
        </dd>
      </dl>
    </section>
  );
}

export function SourceCountsSection({ m }: { m: CouncilContextManifest }) {
  const entries = Object.entries(m.sourceCounts ?? {});
  if (entries.length === 0) {
    return (
      <section className="cid-section" aria-label="Source counts">
        <h4>Source counts</h4>
        <p className="cid-empty">No sources packed.</p>
      </section>
    );
  }
  return (
    <section className="cid-section" aria-label="Source counts">
      <h4>Source counts</h4>
      <ul className="cid-counts">
        {entries.map(([cls, n]) => (
          <li key={cls}>
            <code>{cls}</code> · <span>{n}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function SourceRefsSection({ m }: { m: CouncilContextManifest }) {
  if (!m.sourceRefs.length) {
    return (
      <section className="cid-section" aria-label="Source refs">
        <h4>Source refs</h4>
        <p className="cid-empty">No source refs.</p>
      </section>
    );
  }
  return (
    <section className="cid-section" aria-label="Source refs">
      <h4>Source refs</h4>
      <table className="cid-refs">
        <thead>
          <tr>
            <th>Class</th>
            <th>Sha256</th>
            <th>Tokens</th>
            <th>Citation</th>
            <th>Packed</th>
          </tr>
        </thead>
        <tbody>
          {m.sourceRefs.map((r, i) => (
            <tr key={`${r.class_}-${i}`}>
              <td>
                <code>{r.class_}</code>
              </td>
              <td>
                <code title={r.contentSha256 ?? ""}>
                  {shortHash(r.contentSha256)}
                </code>
              </td>
              <td>{r.tokens ?? "—"}</td>
              <td>{r.citationId ?? "—"}</td>
              <td>{r.packed ?? "inline"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export function ToolResultsSection({ m }: { m: CouncilContextManifest }) {
  const refs = (m.sourceRefs ?? []).filter((r) => r.class_ === "tool_result");
  if (refs.length === 0) return null;
  return (
    <section className="cid-section" aria-label="Tool results">
      <h4>Tool results</h4>
      <table className="cid-refs">
        <thead>
          <tr>
            <th>Tool</th>
            <th>Call</th>
            <th>Args</th>
            <th>Result</th>
            <th>Egress</th>
          </tr>
        </thead>
        <tbody>
          {refs.map((r, i) => (
            <tr key={`${r.toolCallId ?? r.contentSha256 ?? "tool"}-${i}`}>
              <td>
                <code>{r.toolId ?? "—"}</code>
              </td>
              <td>
                <code title={r.toolCallId ?? ""}>
                  {shortHash(r.toolCallId)}
                </code>
              </td>
              <td>
                <code title={r.argsSha256 ?? ""}>
                  {shortHash(r.argsSha256)}
                </code>
              </td>
              <td>
                <code title={r.contentSha256 ?? ""}>
                  {shortHash(r.contentSha256)}
                </code>
              </td>
              <td>{r.toolEgressClass ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export function TokenEstimateSection({ m }: { m: CouncilContextManifest }) {
  return (
    <section className="cid-section" aria-label="Token estimate">
      <h4>Token estimate</h4>
      <dl className="cid-kv">
        <dt>Input</dt>
        <dd>{String(m.tokenEstimate.input ?? "—")}</dd>
        <dt>Method</dt>
        <dd>{String(m.tokenEstimate.method ?? "—")}</dd>
        <dt>Calibration</dt>
        <dd>{String(m.tokenEstimate.calibration_factor ?? "—")}</dd>
        <dt>Cache hints</dt>
        <dd>{(m.cacheHints ?? []).length}</dd>
      </dl>
    </section>
  );
}

export function CitationRefsSection({ m }: { m: CouncilContextManifest }) {
  const citationRefs = m.citationRefs ?? [];
  if (!citationRefs.length) return null;
  return (
    <section className="cid-section" aria-label="Citation refs">
      <h4>Citations</h4>
      <table className="cid-refs">
        <thead>
          <tr>
            <th>ID</th>
            <th>Sha256</th>
            <th>Packed</th>
          </tr>
        </thead>
        <tbody>
          {citationRefs.map((r, i) => (
            <tr key={`${r.citationId}-${i}`}>
              <td>
                <code>{r.citationId}</code>
              </td>
              <td>
                <code title={r.contentSha256 ?? ""}>
                  {shortHash(r.contentSha256)}
                </code>
              </td>
              <td>{r.packed ?? "inline"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export function CompactionSection({ m }: { m: CouncilContextManifest }) {
  const segments = m.compaction?.segments ?? [];
  if (segments.length === 0) return null;
  return (
    <section className="cid-section" aria-label="Compaction">
      <h4>Compaction</h4>
      <ul className="cid-omitted">
        {segments.map((s) => (
          <li key={s.segmentIndex}>
            <code>segment {s.segmentIndex}</code>
            {" · rounds "}
            {s.roundRange.join("-")}
            {" · "}
            {s.mode ?? "structural"}
            {" · "}
            <code title={s.artifactSha256 ?? ""}>
              {shortHash(s.artifactSha256)}
            </code>
            {" · "}
            {s.eventIds.length} events
          </li>
        ))}
      </ul>
    </section>
  );
}

export function StewardSection({ m }: { m: CouncilContextManifest }) {
  const steward = m.steward;
  if (!steward || (!steward.enabled && !steward.packetId && !steward.fallback)) {
    return null;
  }
  const coverage = steward.coverage ?? {};
  return (
    <section className="cid-section" aria-label="Council Steward">
      <h4>Council Steward</h4>
      <dl className="cid-kv">
        <dt>Status</dt>
        <dd>{steward.fallback ? "fallback" : "packet used"}</dd>
        {steward.reason && (
          <>
            <dt>Reason</dt>
            <dd>
              <code>{steward.reason}</code>
            </dd>
          </>
        )}
        <dt>Packet</dt>
        <dd>
          <code title={steward.packetId ?? ""}>
            {steward.packetId ?? "—"}
          </code>
        </dd>
        <dt>Packet sha256</dt>
        <dd>
          <code title={steward.contentSha256 ?? ""}>
            {shortHash(steward.contentSha256)}
          </code>
        </dd>
        <dt>Coverage</dt>
        <dd>
          {coverage.fromSequence ?? "—"}–{coverage.toSequence ?? "—"}
        </dd>
        <dt>Creator mode</dt>
        <dd>{steward.mode ?? "—"}</dd>
        <dt>Recent full messages</dt>
        <dd>{steward.recentFullMessageCount ?? "—"}</dd>
        <dt>Replaced events</dt>
        <dd>{steward.omittedTranscriptEventCount ?? "—"}</dd>
      </dl>
    </section>
  );
}

export function OmittedSection({ m }: { m: CouncilContextManifest }) {
  if (!m.omitted.length) {
    return (
      <section className="cid-section" aria-label="Omitted">
        <h4>Omitted</h4>
        <p className="cid-empty">Nothing omitted.</p>
      </section>
    );
  }
  return (
    <section className="cid-section" aria-label="Omitted">
      <h4>Omitted</h4>
      <ul className="cid-omitted">
        {m.omitted.map((o, i) => {
          const reason = String((o as Record<string, unknown>).reason ?? "unknown");
          const cls = (o as Record<string, unknown>).class_ as string | undefined;
          const evt = (o as Record<string, unknown>).event_id as string | undefined;
          return (
            <li key={i}>
              <code>{reason}</code>
              {cls && (
                <>
                  {" · "}
                  <code>{cls}</code>
                </>
              )}
              {evt && (
                <>
                  {" · "}
                  <code>{evt}</code>
                </>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
