// F031 Phase 2/5 — Transcript.
// Renders Phase 0/1 event vocabulary. Unknown types fail-closed (invariant 4 + 11).
// Phase 5: context_built rows with a manifest_id get an Inspect button.
// QA 2026-06-12: a Simple / Verbose toggle on top of the existing dense
// event log. Simple view renders the user prompt + a conversation-style
// thread (Council Member N is thinking… / Council Member N: "<response>")
// with thinking-budget exhaustion + dialect downgrade surfaced as inline
// notes instead of raw reasoning traces.
import { useMemo, useState } from "react";
import type { CouncilTranscriptEvent } from "./types";

const KNOWN_TYPES = new Set([
  "run_started",
  "run_status_changed",
  "member_queued",
  "context_build_started",
  "context_built",
  "budget_check_started",
  "budget_blocked",
  "member_call_started",
  "member_message",
  "member_skipped",
  "member_failed",
  "member_cancelled",
  "finalization_started",
  "final_answer",
  "verdict_recorded",
  "grounding_recorded",
  "run_cancel_requested",
  "run_cancelled",
  "run_failed",
  "run_completed",
  "diagnostic_note",
  "local_resource_check_started",
  "local_resource_released",
  "dialect_downgraded",
  "steward_packet_requested",
  "steward_packet_created",
  "steward_packet_failed",
  "steward_packet_used",
  "steward_packet_invalidated",
  "steward_recommendation",
  "callout_requested",
  "callout_approval_required",
  "callout_approved",
  "callout_rejected",
  "callout_started",
  "callout_completed",
  "callout_failed",
  "user_interjection",
  // F039 tool-use audit vocabulary.
  "tool_call_requested",
  "tool_call_blocked",
  "tool_call_approved",
  "tool_call_started",
  "tool_call_completed",
  "tool_call_failed",
  // F078 Credibility-mode audit vocabulary.
  "credibility_research_started",
  "credibility_source_captured",
  "credibility_research_completed",
  "credibility_claim_packet_submitted",
  "credibility_claim_packet_rejected",
  "credibility_credidation_started",
  "credibility_credidation_review_submitted",
  "credibility_credidation_completed",
  "credibility_repair_requested",
  "credibility_repair_submitted",
  "credibility_claim_admitted",
  "credibility_claim_excluded",
  "credibility_finalization_started",
  "credibility_report_created",
  // F080 neutral leader-judge.
  "judge_evaluation_started",
  "judge_verdict",
]);

function isFakeEvent(ev: CouncilTranscriptEvent): boolean {
  const payload = ev.payload as Record<string, unknown> | undefined;
  if (payload?.fake_members === true) return true;
  if (payload?.provider === "fake") return true;
  // member_snapshot.locality === "fake" — exposed via raw envelope.
  const raw = ev.raw as { member_snapshot?: { locality?: string } } | undefined;
  return raw?.member_snapshot?.locality === "fake";
}

// Pull a JSON object out of a model reply that may wrap it in a ```json fence
// or surround it with preamble. Returns the parsed object or null.
function extractJsonObject(s: string): Record<string, unknown> | null {
  let body = s.trim();
  // Strip a leading ```json / ``` fence and a trailing ``` fence.
  const fence = /^```[a-zA-Z0-9]*\s*\n?([\s\S]*?)\n?```$/.exec(body);
  if (fence) body = fence[1].trim();
  // Try a straight parse first.
  try {
    return JSON.parse(body) as Record<string, unknown>;
  } catch {
    /* fall through to substring extraction */
  }
  // Otherwise grab the first {...last } span and try that (handles preamble).
  const start = body.indexOf("{");
  const end = body.lastIndexOf("}");
  if (start >= 0 && end > start) {
    try {
      return JSON.parse(body.slice(start, end + 1)) as Record<string, unknown>;
    } catch {
      return null;
    }
  }
  return null;
}

// When digest_v1 dialect is on, members reply with a JSON envelope (sometimes
// fenced in ```json or with preamble). Surface it as readable prose (its stance
// + answer) instead of dumping raw JSON.
function humanizeDigest(s: string): string {
  if (!s.includes("digest_v1")) return s;
  const obj = extractJsonObject(s);
  if (!obj) return s;
  try {
    if (obj.v !== "digest_v1") return s;
    const parts: string[] = [];
    const answer = (obj.answer_fragment ?? obj.position) as unknown;
    if (typeof answer === "string" && answer.trim()) parts.push(answer.trim());
    const claims = Array.isArray(obj.claims) ? obj.claims : [];
    const claimTexts = claims
      .map((c) => (c && typeof c === "object" ? (c as Record<string, unknown>).text : null))
      .filter((t): t is string => typeof t === "string" && t.trim().length > 0);
    if (claimTexts.length) parts.push("• " + claimTexts.join("\n• "));
    const delta = obj.delta;
    if (typeof delta === "string" && delta.trim() && delta !== "no_changed_views") {
      parts.push(`(changed: ${delta.trim()})`);
    }
    return parts.length ? parts.join("\n") : s;
  } catch {
    return s;
  }
}

// Trim leading/trailing whitespace and collapse 3+ blank lines to one so a
// model's stray trailing newlines / padding don't blow out the transcript.
function collapseWs(s: string): string {
  return s.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
}

// F078: the website a claim cites, as a clean host (drops "www.", skips minted
// source ids like "src_0001"). Returns "" when no URL citation is present.
function citationHost(sourceIds: unknown): string {
  if (!Array.isArray(sourceIds)) return "";
  for (const sid of sourceIds) {
    const raw = String(sid ?? "").trim();
    if (!raw) continue;
    try {
      return new URL(raw).hostname.replace(/^www\./, "");
    } catch {
      // not a URL (e.g. a minted source id) — try the next one.
    }
  }
  return "";
}

// F078 Credibility mode: members reply with a JSON claim packet
// ({answer_fragment, claims:[{text, source_ids}]}) or a review
// ({reviews:[{claim_id,status}]}), sometimes fenced in ```json. Surface them as
// readable prose (with the cited website) instead of raw JSON.
function humanizeCredibility(s: string): string {
  const obj = extractJsonObject(s);
  if (!obj) return s;
  try {
    if (Array.isArray((obj as Record<string, unknown>).claims)) {
      const parts: string[] = [];
      const answer = (obj.answer_fragment ?? obj.position) as unknown;
      if (typeof answer === "string" && answer.trim()) parts.push(answer.trim());
      const texts = ((obj.claims as unknown[]) ?? [])
        .map((c) => {
          if (!c || typeof c !== "object") return null;
          const co = c as Record<string, unknown>;
          const text = typeof co.text === "string" ? co.text.trim() : "";
          if (!text) return null;
          // F078: surface the website the model cited (host only, for a clean
          // Simple-view reply). source_ids may be full URLs or minted ids.
          const cite = citationHost(co.source_ids);
          return cite ? `${text} (${cite})` : text;
        })
        .filter((t): t is string => typeof t === "string" && t.length > 0);
      if (texts.length) parts.push("• " + texts.join("\n• "));
      return parts.length ? parts.join("\n") : s;
    }
    if (Array.isArray((obj as Record<string, unknown>).reviews)) {
      // Prefer the member's own words reacting to the others — that's what makes
      // the simple view read like a real discussion.
      const comment = (obj as Record<string, unknown>).comment;
      if (typeof comment === "string" && comment.trim()) return comment.trim();
      // Fallback (no comment): summarize the structured take in plain English,
      // grouped by stance — never "verified — Claude:c1".
      return humanizeReviewSummary((obj.reviews as unknown[]) ?? []) || s;
    }
  } catch {
    return s;
  }
  return s;
}

// Turn a list of structured reviews into a single human sentence, e.g.
// "I agree with Claude's c1, c2 and GPT's c1; I'm not convinced by GPT's c2."
function humanizeReviewSummary(reviews: unknown[]): string {
  const verb: Record<string, string> = {
    verified: "agree with",
    partially_supported: "partly agree with",
    unsupported: "am not convinced by",
    contradicted: "disagree with",
  };
  const byStance = new Map<string, string[]>();
  for (const r of reviews) {
    const ro = (r ?? {}) as Record<string, unknown>;
    const cid = String(ro.claim_id ?? "").trim();
    if (!cid) continue;
    const status = String(ro.status ?? "").trim();
    const v = verb[status] ?? "note";
    if (!byStance.has(v)) byStance.set(v, []);
    byStance.get(v)!.push(cid);
  }
  const clauses = [...byStance.entries()].map(
    ([v, ids]) => `I ${v} ${ids.join(", ")}`,
  );
  return clauses.join("; ") + (clauses.length ? "." : "");
}

// Simple-view content: humanize digest_v1 + credibility JSON to prose, then tidy.
function cleanContent(s: string): string {
  return collapseWs(humanizeCredibility(humanizeDigest(s)));
}

function formatPayload(ev: CouncilTranscriptEvent): string {
  // Verbose view — show content as-is (raw digest JSON included), whitespace
  // tidied only.
  const payload = ev.payload as Record<string, unknown>;
  if (typeof payload?.content === "string") return collapseWs(payload.content);
  if (typeof payload?.reason === "string") return `reason: ${payload.reason}`;
  if (typeof payload?.terminal_reason === "string")
    return `terminal_reason: ${payload.terminal_reason}`;
  try {
    return JSON.stringify(payload);
  } catch {
    return "[unrenderable]";
  }
}

function renderCitationText(text: string) {
  const parts = text.split(/(\[c:[A-Za-z0-9_-]+\])/g);
  return parts.map((part, idx) => {
    const match = /^\[c:([A-Za-z0-9_-]+)\]$/.exec(part);
    if (!match) return <span key={idx}>{part}</span>;
    return (
      <span
        key={idx}
        className="citation-chip"
        title={`Citation ${match[1]}; content is available only through inspection policy`}
      >
        {part}
      </span>
    );
  });
}

function shortHash(s: string | null | undefined): string {
  if (!s) return "—";
  if (s.length <= 16) return s;
  return `${s.slice(0, 8)}…${s.slice(-4)}`;
}

function DigestCard({ payload }: { payload: Record<string, unknown> }) {
  const digest = payload.digest as Record<string, unknown> | undefined;
  if (!digest) return null;
  const claims = Array.isArray(digest.claims) ? digest.claims : [];
  const disputes = Array.isArray(digest.dispute) ? digest.dispute : [];
  const open = Array.isArray(digest.open) ? digest.open : [];
  return (
    <div className="digest-card" aria-label="Structured digest">
      <div className="digest-position">
        {String(digest.position ?? "")}
      </div>
      {claims.length > 0 && (
        <ul className="digest-claims">
          {claims.map((claim, idx) => {
            const c = claim as Record<string, unknown>;
            const cites = Array.isArray(c.cites) ? c.cites : [];
            return (
              <li key={idx}>
                <code>{String(c.id ?? `k${idx + 1}`)}</code>
                {" "}
                <span className={`confidence confidence-${String(c.confidence ?? "medium")}`}>
                  {String(c.confidence ?? "medium")}
                </span>
                {" "}
                {String(c.text ?? "")}
                {cites.length > 0 && (
                  <span className="digest-cites">
                    {" "}
                    {cites.map((cite) => (
                      <span
                        key={String(cite)}
                        className="citation-chip"
                        title={`Citation ${String(cite)}`}
                      >
                        [c:{String(cite)}]
                      </span>
                    ))}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {disputes.length > 0 && (
        <div className="digest-disputes">
          disputes: {JSON.stringify(disputes)}
        </div>
      )}
      {digest.delta != null && digest.delta !== "" && (
        <div className="digest-delta">delta: {String(digest.delta)}</div>
      )}
      {open.length > 0 && (
        <div className="digest-open">open: {open.map(String).join(" | ")}</div>
      )}
      <details>
        <summary>Raw digest</summary>
        <pre>{JSON.stringify(digest, null, 2)}</pre>
      </details>
    </div>
  );
}

function StewardPacketCard({ ev }: { ev: CouncilTranscriptEvent }) {
  const payload = ev.payload as Record<string, unknown>;
  const coverage = (
    payload.coverage && typeof payload.coverage === "object"
      ? payload.coverage
      : {}
  ) as Record<string, unknown>;
  const sourceIds = Array.isArray(payload.source_event_ids)
    ? payload.source_event_ids
    : Array.isArray(coverage.source_event_ids)
      ? coverage.source_event_ids
      : [];
  const title =
    ev.type === "steward_packet_created"
      ? "Steward packet created"
      : ev.type === "steward_packet_used"
        ? "Steward packet used"
        : ev.type === "steward_packet_failed"
          ? "Steward packet failed"
          : ev.type === "steward_packet_requested"
            ? "Steward packet requested"
            : "Steward packet event";
  return (
    <details className="steward-packet-card">
      <summary>{title}</summary>
      <dl className="cid-kv">
        <dt>Packet</dt>
        <dd>
          <code>{String(payload.packet_id ?? "—")}</code>
        </dd>
        <dt>Packet sha256</dt>
        <dd>
          <code title={String(payload.content_sha256 ?? "")}>
            {shortHash(
              typeof payload.content_sha256 === "string"
                ? payload.content_sha256
                : null,
            )}
          </code>
        </dd>
        <dt>Coverage</dt>
        <dd>
          {String(coverage.from_sequence ?? "—")}–{String(coverage.to_sequence ?? "—")}
        </dd>
        <dt>Mode</dt>
        <dd>{String(payload.mode ?? "—")}</dd>
        <dt>Estimated tokens</dt>
        <dd>{String(payload.estimated_tokens ?? "—")}</dd>
        <dt>Source events</dt>
        <dd>{sourceIds.length}</dd>
        {Boolean(payload.reason) && (
          <>
            <dt>Reason</dt>
            <dd>
              <code>{String(payload.reason)}</code>
            </dd>
          </>
        )}
        {Boolean(payload.recipient_member_id) && (
          <>
            <dt>Recipient</dt>
            <dd>
              <code>{String(payload.recipient_member_id)}</code>
            </dd>
          </>
        )}
      </dl>
    </details>
  );
}

function PayloadBody({ ev }: { ev: CouncilTranscriptEvent }) {
  const payload = ev.payload as Record<string, unknown>;
  if (ev.type.startsWith("steward_packet_") || ev.type === "steward_recommendation") {
    return <StewardPacketCard ev={ev} />;
  }
  if (payload.digest && typeof payload.digest === "object") {
    return <DigestCard payload={payload} />;
  }
  const text = formatPayload(ev);
  return <>{renderCitationText(text)}</>;
}

function manifestIdOf(ev: CouncilTranscriptEvent): string | null {
  if (ev.type !== "context_built") return null;
  const v = (ev.payload as Record<string, unknown>).manifest_id;
  return typeof v === "string" && v.length > 0 ? v : null;
}

function turnIdOf(ev: CouncilTranscriptEvent): string {
  // Adapter synthesizes turn_id = `${member_id}-r${round}`. The router uses
  // that string to key manifests. Reconstruct here so the Inspect button
  // can call the inspection endpoint with the right key.
  if (!ev.memberId || ev.round == null) return "";
  return `${ev.memberId}-r${ev.round}`;
}

interface Props {
  events: CouncilTranscriptEvent[];
  // QA P1 #1: Inspect passes (round, memberId) so the drawer can fetch
  // the round-level inspection (all per-member manifests for the round)
  // and render the compare strip. turnId is retained for back-compat
  // with any caller that needs single-turn drill-down — typically not
  // the demo path.
  onInspect?: (
    args: { round: number; memberId: string | undefined; turnId: string },
  ) => void;
  // QA 2026-06-12: pass the prompt the run was started with so the
  // simple-view conversation can lead with "User Prompt: ..."
  userPrompt?: string;
  // Optional override of member display labels (e.g. "Gemma3 27B"
  // instead of "m-1"). Keyed by member_id.
  memberLabels?: Record<string, string>;
  // F084: member ids configured as designated steelman advocates. Their turns
  // are badged "Steelman — unverified" so a constructed case never reads as
  // verified fact.
  steelmanMemberIds?: string[];
}

// Keep in sync with THINKING_TRACE_MARKER in gateway_local.py.
const THINKING_TRACE_MARKER = "(reasoning trace, no visible answer)";

type ViewMode = "simple" | "verbose";

function isThinkingBurn(payload: Record<string, unknown>): boolean {
  // Prefer the structured flag set by the gateway (is_thinking_burn=True)
  // over string-matching content so a backend wording change doesn't
  // silently regress the simple view.
  if (typeof payload.is_thinking_burn === "boolean") return payload.is_thinking_burn;
  const content = String(payload.content ?? "");
  return content.trim().startsWith(THINKING_TRACE_MARKER);
}

function labelFor(memberId: string | undefined, overrides: Record<string, string> | undefined): string {
  if (!memberId) return "Council Member";
  return overrides?.[memberId] ?? memberId;
}

interface SimpleTurn {
  key: string;
  memberId: string | undefined;
  round: number | null | undefined;
  status: "thinking" | "spoke" | "thinking_burn" | "skipped" | "failed" | "final_answer" | "run_failed" | "run_completed" | "user_interjection" | "judge_verdict";
  content?: string;
  duration_ms?: number;
  downgraded?: boolean;
  reason?: string;
  // F037: an expert callout answer is rendered as a member turn with a badge.
  isCallout?: boolean;
  advisory?: boolean;
  // For the final-answer turn: how the run actually ended, so the UI doesn't
  // imply the members agreed when the run just hit the round/budget limit.
  terminalReason?: string;
  // "consensus" when the final answer was synthesized by a consensus-report
  // finalizer turn, rather than copied verbatim from the last speaker.
  synthesisMode?: string;
  // F064: which members held their position + threshold/round, so the simple
  // view can explain HOW the council leader reached consensus.
  consensus?: ConsensusDetail;
  // F078: the credibility report (verified claims + sources) when the run
  // finalized as a credibility_report.
  credibilityReport?: CredibilityReportView;
  // F080: the neutral judge's verdict for a judge_verdict turn.
  judgeVerdict?: string;
}

interface ConsensusDetail {
  agreedIds: string[];
  threshold: number;
  round: number;
  memberCount: number;
}

interface CredibilityReportView {
  claimsUsed: number;
  sources: { sourceId: string; url: string; title: string; sourceType: string; tier: string; tierLabel: string }[];
  caveats: string[];
  excluded: { claimId: string; reason: string }[];
  confidence: string;
  verificationIncomplete: boolean;
  qualityFlag: string;
  dispositions: { claimId: string; disposition: string; text: string; revisedText: string }[];
  finalizerCitationFailures: { claimId: string; reason: string }[];
  // F084: designated steelman advocates' claims — UNVERIFIED, quarantined.
  steelmanClaims: { claimId: string; memberId: string; topic: string; text: string; cited: string[] }[];
}

// F085: provenance-tier roll-up (mirrors errorta_council.credibility.source_tier).
// Used only as a fallback when a report predates the backend `tier` field.
const _TIER_BY_TYPE: Record<string, string> = {
  official: "primary", primary_document: "primary", peer_reviewed_paper: "primary",
  government: "primary", standards_body: "primary",
  reputable_news: "reporting", trade_publication: "reporting", company_docs: "reporting",
  blog: "opinion", forum: "opinion", unknown: "unknown",
};
const _TIER_LABEL: Record<string, string> = {
  primary: "primary", reporting: "reporting", opinion: "opinion", unknown: "unverified",
};
function tierForSourceType(t: string): string {
  return _TIER_BY_TYPE[t] ?? "unknown";
}
function tierLabelForSourceType(t: string): string {
  return _TIER_LABEL[tierForSourceType(t)] ?? "unverified";
}

function adaptCredibilityReport(raw: unknown): CredibilityReportView | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const r = raw as Record<string, unknown>;
  const sourceMap = Array.isArray(r.source_map) ? r.source_map : [];
  const excluded = Array.isArray(r.excluded_claims) ? r.excluded_claims : [];
  return {
    claimsUsed: Array.isArray(r.claims_used) ? r.claims_used.length : 0,
    sources: sourceMap.map((s) => {
      const so = (s ?? {}) as Record<string, unknown>;
      const sourceType = String(so.source_type ?? "unknown");
      return {
        sourceId: String(so.source_id ?? ""),
        url: String(so.url ?? ""),
        title: String(so.title ?? ""),
        sourceType,
        // F085: tier comes from the backend; fall back to a local roll-up so an
        // older report (no tier field) still tags correctly.
        tier: String(so.tier ?? tierForSourceType(sourceType)),
        tierLabel: String(so.tier_label ?? tierLabelForSourceType(sourceType)),
      };
    }),
    caveats: Array.isArray(r.caveats) ? r.caveats.map(String) : [],
    excluded: excluded.map((e) => {
      const eo = (e ?? {}) as Record<string, unknown>;
      return { claimId: String(eo.claim_id ?? ""), reason: String(eo.reason ?? "") };
    }),
    confidence: String(r.confidence ?? "medium"),
    verificationIncomplete: Boolean(r.verification_incomplete),
    qualityFlag: String(r.quality_flag ?? ""),
    dispositions: (Array.isArray(r.dispositions) ? r.dispositions : []).map((d) => {
      const dd = (d ?? {}) as Record<string, unknown>;
      return {
        claimId: String(dd.claim_id ?? ""),
        disposition: String(dd.disposition ?? ""),
        text: String(dd.text ?? ""),
        revisedText: String(dd.revised_text ?? ""),
      };
    }),
    finalizerCitationFailures: (Array.isArray(r.finalizer_citation_failures)
      ? r.finalizer_citation_failures : []).map((f) => {
      const fo = (f ?? {}) as Record<string, unknown>;
      return { claimId: String(fo.claim_id ?? ""), reason: String(fo.reason ?? "") };
    }),
    steelmanClaims: (Array.isArray(r.steelman_claims) ? r.steelman_claims : []).map((s) => {
      const so = (s ?? {}) as Record<string, unknown>;
      return {
        claimId: String(so.claim_id ?? ""),
        memberId: String(so.member_id ?? ""),
        topic: String(so.topic ?? ""),
        text: String(so.text ?? ""),
        cited: Array.isArray(so.cited) ? so.cited.map(String) : [],
      };
    }),
  };
}

// F081: a human label for the report's debate-quality flag.
function qualityFlagLabel(flag: string): string {
  if (flag === "unchallenged_consensus") {
    return "Unchallenged consensus — no opposing case survived the gate; treat with caution.";
  }
  return flag.replace(/_/g, " ");
}

function CredibilityReportCard({ report }: { report: CredibilityReportView }) {
  return (
    <div className="credibility-report" data-testid="credibility-report">
      {report.verificationIncomplete ? (
        <p className="simple-note simple-no-consensus" data-testid="credibility-incomplete">
          Warning: verification incomplete — required research could not complete.
        </p>
      ) : null}
      {report.qualityFlag ? (
        <p className="simple-note simple-no-consensus" data-testid="credibility-quality-flag">
          {qualityFlagLabel(report.qualityFlag)}
        </p>
      ) : null}
      <p className="credibility-summary">
        {report.claimsUsed} verified claim{report.claimsUsed === 1 ? "" : "s"} ·{" "}
        {report.sources.length} source{report.sources.length === 1 ? "" : "s"} ·
        confidence {report.confidence}
      </p>
      {report.sources.length > 0 ? (
        <>
          <ol className="credibility-sources" data-testid="credibility-sources">
            {report.sources.map((s, i) => (
              <li key={s.sourceId || i}>
                {s.sourceType && s.sourceType !== "unknown" ? (
                  <>
                    <span className="credibility-source-type">[{s.sourceType}]</span>{" "}
                  </>
                ) : null}
                {s.title && s.title !== s.url ? `${s.title} — ` : ""}
                <span className="credibility-source-url">{s.url}</span>
                {/* F085: provenance tag — opinion/unverified sources are flagged
                    so a blog citation never reads as corroborated fact. */}
                <span
                  className={`credibility-tier credibility-tier-${s.tier}`}
                  data-testid={`source-tier-${i}`}
                >
                  {" "}· {s.tierLabel}
                </span>
              </li>
            ))}
          </ol>
          {report.sources.some((s) => s.tier === "opinion" || s.tier === "unknown") ? (
            <p className="credibility-tier-legend" data-testid="credibility-tier-legend">
              opinion = an individual viewpoint, not corroborated reporting — weigh accordingly.
            </p>
          ) : null}
        </>
      ) : null}
      {report.dispositions.some((d) => d.disposition === "revised" || d.disposition === "inference") ? (
        <ul className="credibility-dispositions" data-testid="credibility-dispositions">
          {report.dispositions
            .filter((d) => d.disposition === "revised" || d.disposition === "inference")
            .map((d) => (
              <li key={d.claimId}>
                {d.disposition === "revised"
                  ? `Narrowed to what the source supports: “${d.revisedText || d.text}”`
                  : `Inference (not directly stated by the source): “${d.text}”`}
              </li>
            ))}
        </ul>
      ) : null}
      {report.finalizerCitationFailures.length > 0 ? (
        <p className="simple-note simple-no-consensus" data-testid="credibility-finalizer-failed">
          The council leader mis-cited {report.finalizerCitationFailures.length} of its
          own source{report.finalizerCitationFailures.length === 1 ? "" : "s"} — its
          conclusion is downgraded.
        </p>
      ) : null}
      {report.caveats.length > 0 ? (
        <ul className="credibility-caveats" data-testid="credibility-caveats">
          {report.caveats.map((c, i) => (
            <li key={i}>{c}</li>
          ))}
        </ul>
      ) : null}
      {report.steelmanClaims.length > 0 ? (
        <div className="credibility-steelman" data-testid="credibility-steelman">
          <p className="credibility-steelman-head">
            Steelman arguments — UNVERIFIED (may include constructed evidence)
            {report.steelmanClaims.find((s) => s.topic)
              ? ` · arguing: ${report.steelmanClaims.find((s) => s.topic)!.topic}`
              : ""}
          </p>
          <ul>
            {report.steelmanClaims.map((s, i) => (
              <li key={s.claimId || i}>{s.text}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {report.excluded.length > 0 ? (
        <p className="simple-note" data-testid="credibility-excluded">
          {report.excluded.length} claim{report.excluded.length === 1 ? "" : "s"} excluded
          (unverified or contradicted)
          {report.excluded.some((e) => e.reason === "entailment_contradicted")
            ? `, including ${report.excluded.filter((e) => e.reason === "entailment_contradicted").length} whose cited source argued the opposite`
            : ""}
          .
        </p>
      ) : null}
    </div>
  );
}

function buildSimpleTurns(
  events: CouncilTranscriptEvent[],
): SimpleTurn[] {
  // Group events by (memberId, round). For each group: track latest status.
  // Order: by sequence (events arrive in sequence order already, but be defensive).
  const sorted = [...events].sort((a, b) => a.sequence - b.sequence);
  type Key = string;
  const order: Key[] = [];
  const map = new Map<Key, SimpleTurn>();
  const keyOf = (mid: string | undefined, round: number | null | undefined) =>
    `${mid ?? "?"}-r${round ?? 0}`;

  for (const ev of sorted) {
    const k = keyOf(ev.memberId, ev.round);
    const payload = ev.payload as Record<string, unknown>;
    if (ev.type === "member_call_started") {
      if (!map.has(k)) {
        order.push(k);
        map.set(k, {
          key: k,
          memberId: ev.memberId,
          round: ev.round,
          status: "thinking",
        });
      }
      continue;
    }
    if (ev.type === "dialect_downgraded") {
      if (!map.has(k)) {
        order.push(k);
        map.set(k, {
          key: k, memberId: ev.memberId, round: ev.round, status: "thinking",
        });
      }
      const turn = map.get(k)!;
      turn.downgraded = true;
      continue;
    }
    if (ev.type === "final_answer") {
      const content = cleanContent((payload?.content as string) ?? "");
      const rawConsensus = payload?.consensus as Record<string, unknown> | undefined;
      const consensus: ConsensusDetail | undefined =
        rawConsensus && Array.isArray(rawConsensus.agreed_member_ids)
          ? {
              agreedIds: (rawConsensus.agreed_member_ids as unknown[]).map(String),
              threshold: Number(rawConsensus.threshold ?? 0),
              round: Number(rawConsensus.round ?? 0),
              memberCount: Number(rawConsensus.member_count ?? 0),
            }
          : undefined;
      const turn: SimpleTurn = {
        key: "final-answer",
        memberId: ev.memberId,
        round: ev.round,
        status: "final_answer",
        content,
        synthesisMode: (payload?.synthesis_mode as string) ?? undefined,
        consensus,
        credibilityReport: adaptCredibilityReport(payload?.credibility_report),
      };
      if (!map.has("final-answer")) order.push("final-answer");
      map.set("final-answer", turn);
      continue;
    }
    if (ev.type === "run_completed") {
      // Stamp the terminal reason onto the final-answer turn so the label can
      // distinguish "consensus reached" from "ran out of rounds".
      const reason = (payload?.reason as string) ?? "";
      const fa = map.get("final-answer");
      if (fa) fa.terminalReason = reason;
      continue;
    }
    if (ev.type === "run_failed") {
      const reason =
        (payload?.reason as string) ??
        (payload?.terminal_reason as string) ??
        "run failed";
      const turn: SimpleTurn = {
        key: "terminal-failed",
        memberId: undefined,
        round: null,
        status: "run_failed",
        reason,
      };
      if (!map.has("terminal-failed")) order.push("terminal-failed");
      map.set("terminal-failed", turn);
      continue;
    }
    if (ev.type === "user_interjection") {
      // F049: the user's live message — its own "You" turn, in sequence.
      const ik = `interjection-${ev.sequence}`;
      order.push(ik);
      map.set(ik, {
        key: ik,
        memberId: undefined,
        round: ev.round,
        status: "user_interjection",
        content: collapseWs((payload?.content as string) ?? ""),
      });
      continue;
    }
    if (ev.type === "judge_verdict") {
      // F080: the neutral judge's call for this round — its own compact turn.
      const jk = `judge-${ev.sequence}`;
      order.push(jk);
      map.set(jk, {
        key: jk,
        memberId: ev.memberId,
        round: ev.round,
        status: "judge_verdict",
        judgeVerdict: String(payload?.verdict ?? "continue"),
        reason: String(payload?.reason ?? ""),
      });
      continue;
    }
    if (ev.type === "member_message") {
      const content = cleanContent((payload?.content as string) ?? "");
      const duration_ms = payload?.duration_ms as number | undefined;
      const burn = isThinkingBurn(payload);
      const prev = map.get(k);
      const turn: SimpleTurn = {
        key: k,
        memberId: ev.memberId,
        round: ev.round,
        status: burn ? "thinking_burn" : "spoke",
        content,
        duration_ms,
        downgraded: prev?.downgraded,
        isCallout: payload?.is_callout === true,
        advisory: payload?.advisory === true,
      };
      if (!map.has(k)) order.push(k);
      map.set(k, turn);
      continue;
    }
    if (ev.type === "member_skipped") {
      const turn: SimpleTurn = {
        key: k, memberId: ev.memberId, round: ev.round, status: "skipped",
        reason: (payload?.reason as string) ?? undefined,
      };
      if (!map.has(k)) order.push(k);
      map.set(k, turn);
      continue;
    }
    if (ev.type === "member_failed" || ev.type === "member_cancelled") {
      const turn: SimpleTurn = {
        key: k, memberId: ev.memberId, round: ev.round, status: "failed",
        reason: (payload?.reason as string) ?? ev.type,
      };
      if (!map.has(k)) order.push(k);
      map.set(k, turn);
      continue;
    }
  }
  return order.map((k) => map.get(k)!);
}

function SimpleView({
  events, userPrompt, memberLabels, steelmanMemberIds,
}: { events: CouncilTranscriptEvent[]; userPrompt?: string; memberLabels?: Record<string, string>; steelmanMemberIds?: string[] }) {
  const turns = useMemo(() => buildSimpleTurns(events), [events]);
  const steelmen = useMemo(() => new Set(steelmanMemberIds ?? []), [steelmanMemberIds]);
  // Who took part + how many rounds — used to explain, in plain terms, how the
  // council leader reached consensus.
  const participants = useMemo(() => {
    const names = new Map<string, string>();
    for (const ev of events) {
      if (ev.type === "member_message" && ev.memberId) {
        names.set(ev.memberId, labelFor(ev.memberId, memberLabels));
      }
    }
    return [...names.values()];
  }, [events, memberLabels]);
  const roundCount = useMemo(
    () => events.reduce((m, e) => Math.max(m, e.round ?? 0), 0),
    [events],
  );
  // F064: the structured consensus detail (who held their position, threshold,
  // round) lets us badge the agreeing members + explain how the leader got here.
  const consensusDetail = useMemo(
    () => turns.find((t) => t.status === "final_answer")?.consensus,
    [turns],
  );
  const agreeingIds = useMemo(
    () => new Set(consensusDetail?.agreedIds ?? []),
    [consensusDetail],
  );
  return (
    <div className="council-transcript-simple" role="log" aria-label="Council conversation">
      {userPrompt ? (
        <div className="simple-user-prompt">
          <span className="simple-label">User Prompt:</span>
          <span className="simple-text">{userPrompt}</span>
        </div>
      ) : null}
      {turns.length === 0 ? (
        <p className="council-empty">Run has not produced member turns yet.</p>
      ) : null}
      {turns.map((t) => {
        const label = labelFor(t.memberId, memberLabels);
        const roundSuffix = t.round ? ` (round ${t.round})` : "";
        if (t.status === "user_interjection") {
          return (
            <div key={t.key} className="simple-turn simple-turn-user" data-testid="simple-interjection">
              <span className="simple-label">You:</span>
              <span className="simple-text">{t.content}</span>
            </div>
          );
        }
        if (t.status === "judge_verdict") {
          const v = t.judgeVerdict ?? "continue";
          const headline =
            v === "reached"
              ? "members reached a verdict"
              : v === "decide"
                ? "broke the tie"
                : v === "no_consensus"
                  ? "no consensus"
                  : "keep deliberating";
          return (
            <div
              key={t.key}
              className="simple-turn simple-turn-judge"
              data-testid="simple-judge-verdict"
            >
              <span className="simple-label">⚖️ Judge{roundSuffix}:</span>
              <span className="simple-text">
                {" "}{headline}{t.reason ? ` — ${t.reason}` : ""}
              </span>
            </div>
          );
        }
        if (t.status === "thinking") {
          return (
            <div key={t.key} className="simple-turn simple-turn-thinking">
              <span className="simple-label">{label}{roundSuffix}</span>
              <span className="simple-thinking"> is thinking…</span>
              {t.downgraded ? (
                <span className="simple-note"> (dialect downgraded to prose)</span>
              ) : null}
            </div>
          );
        }
        if (t.status === "spoke") {
          return (
            <div
              key={t.key}
              className={
                "simple-turn simple-turn-spoke" +
                (t.isCallout ? " simple-turn-callout" : "")
              }
            >
              <span className="simple-label">{label}{roundSuffix}:</span>
              {t.memberId && steelmen.has(t.memberId) ? (
                <span
                  className="simple-steelman-badge"
                  data-testid="steelman-badge"
                  title="Designated steelman — argues a position and may construct supporting evidence; treat as unverified."
                >
                  Steelman · unverified
                </span>
              ) : null}
              {t.isCallout ? (
                <span className="simple-callout-badge">
                  Expert callout{t.advisory ? " · advisory" : ""}
                </span>
              ) : null}
              {t.memberId &&
              agreeingIds.has(t.memberId) &&
              t.round === consensusDetail?.round ? (
                <span className="simple-agreed-badge" data-testid="held-position-badge">
                  ✓ held position
                </span>
              ) : null}
              {t.downgraded ? (
                <span className="simple-note"> (dialect downgraded to prose)</span>
              ) : null}
              <div className="simple-content">"{t.content}"</div>
            </div>
          );
        }
        if (t.status === "thinking_burn") {
          return (
            <div key={t.key} className="simple-turn simple-turn-burn">
              <span className="simple-label">{label}{roundSuffix}:</span>
              <span className="simple-note">
                {" "}(no response — reasoning budget exhausted before the model produced a visible answer)
              </span>
            </div>
          );
        }
        if (t.status === "final_answer") {
          const consensus = t.terminalReason === "consensus_reached";
          const synthesized = t.synthesisMode === "consensus";
          const summary = t.synthesisMode === "summary";
          const credibility =
            t.synthesisMode === "credibility" || t.credibilityReport != null;
          const stoppedAtLimit =
            t.terminalReason === "limits_exhausted" ||
            t.terminalReason === "max_rounds_reached";
          const judged = t.synthesisMode === "judge";
          let finalLabel = "Final answer:";
          if (credibility) {
            finalLabel = "Credibility report:";
          } else if (judged) {
            finalLabel = "Judge verdict:";
          } else if (summary) {
            finalLabel = "Summary:";
          } else if (synthesized) {
            finalLabel = "Consensus:";
          } else if (consensus) {
            finalLabel = "Final answer · consensus reached:";
          }
          // Plain-English "how the leader got here" line. Prefer the precise
          // backend consensus detail (who held their position, threshold,
          // round); fall back to participant/round counts for older runs.
          const cd = t.consensus;
          const agreedNames = cd
            ? cd.agreedIds.map((id) => labelFor(id, memberLabels))
            : participants;
          const who =
            agreedNames.length > 0
              ? `${agreedNames.length} member${agreedNames.length === 1 ? "" : "s"} (${agreedNames.join(", ")})`
              : "the members";
          const overRounds = cd
            ? ` in round ${cd.round}`
            : roundCount > 0
              ? ` over ${roundCount} round${roundCount === 1 ? "" : "s"}`
              : "";
          const heldPhrase = cd
            ? `${cd.agreedIds.length} of ${cd.memberCount} members held their position${overRounds}` +
              (agreedNames.length > 0 ? ` (${agreedNames.join(", ")})` : "")
            : `${who} discussed${overRounds} and agreed`;
          return (
            <div
              key={t.key}
              className={`simple-turn simple-turn-final${
                synthesized || consensus ? " simple-turn-consensus" : ""
              }`}
            >
              {consensus ? (
                <p className="simple-consensus-banner" data-testid="consensus-banner">
                  ✓ The council reached consensus
                </p>
              ) : null}
              <span className="simple-label">{finalLabel}</span>
              {t.memberId ? (
                <span className="simple-final-leader" data-testid="council-leader">
                  {" "}Council Leader: {labelFor(t.memberId, memberLabels)}
                </span>
              ) : null}
              {synthesized ? (
                <p className="simple-note simple-consensus-note">
                  How: {heldPhrase}; the council leader then combined their
                  positions into the shared conclusion below — not a verbatim
                  copy of any single member. Switch to Verbose to see each
                  member&apos;s position and the agreement signals.
                </p>
              ) : consensus ? (
                <p className="simple-note simple-consensus-note">
                  {heldPhrase}. Switch to Verbose for each member&apos;s position
                  and the agreement signals.
                </p>
              ) : null}
              {stoppedAtLimit && !credibility ? (
                <p className="simple-note simple-no-consensus">
                  {summary
                    ? (
                        "Warning: The run stopped at the round/budget limit and did not " +
                        "reach consensus. This summary preserves disagreement; it is not " +
                        "an agreed conclusion."
                      )
                    : (
                        "Warning: The run stopped at the round/budget limit — the members " +
                        "did not reach consensus. This is the last message stated, not an " +
                        "agreed conclusion."
                      )}
                </p>
              ) : null}
              <div className="simple-content">{t.content}</div>
              {t.credibilityReport ? (
                <CredibilityReportCard report={t.credibilityReport} />
              ) : null}
            </div>
          );
        }
        if (t.status === "run_failed") {
          return (
            <div key={t.key} className="simple-turn simple-turn-failed simple-turn-terminal">
              <span className="simple-label">Run failed:</span>
              <span className="simple-note"> {t.reason}</span>
            </div>
          );
        }
        if (t.status === "skipped") {
          return (
            <div key={t.key} className="simple-turn simple-turn-skipped">
              <span className="simple-label">{label}{roundSuffix}:</span>
              <span className="simple-note"> (skipped{t.reason ? ` — ${t.reason}` : ""})</span>
            </div>
          );
        }
        return (
          <div key={t.key} className="simple-turn simple-turn-failed">
            <span className="simple-label">{label}{roundSuffix}:</span>
            <span className="simple-note"> (failed{t.reason ? ` — ${t.reason}` : ""})</span>
          </div>
        );
      })}
    </div>
  );
}

export default function CouncilTranscript({ events, onInspect, userPrompt, memberLabels, steelmanMemberIds }: Props) {
  const [mode, setMode] = useState<ViewMode>("simple");
  if (events.length === 0) {
    return <p className="council-empty">Start a run to populate the transcript.</p>;
  }
  return (
    <div className="council-transcript-wrapper">
      <div className="council-transcript-toolbar" role="toolbar" aria-label="Transcript view mode">
        <button
          type="button"
          className={`council-mode-btn${mode === "simple" ? " active" : ""}`}
          aria-pressed={mode === "simple"}
          onClick={() => setMode("simple")}
        >
          Simple
        </button>
        <button
          type="button"
          className={`council-mode-btn${mode === "verbose" ? " active" : ""}`}
          aria-pressed={mode === "verbose"}
          onClick={() => setMode("verbose")}
        >
          Verbose
        </button>
      </div>
      {mode === "simple" ? (
        <SimpleView events={events} userPrompt={userPrompt} memberLabels={memberLabels} steelmanMemberIds={steelmanMemberIds} />
      ) : (
        <VerboseView events={events} onInspect={onInspect} />
      )}
    </div>
  );
}

function VerboseView({ events, onInspect }: { events: CouncilTranscriptEvent[]; onInspect?: Props["onInspect"] }) {
  return (
    <div className="council-transcript" role="log" aria-label="Council transcript">
      {events.map((ev) => {
        const isKnown = KNOWN_TYPES.has(ev.type);
        const isFake = isFakeEvent(ev);
        const manifestId = manifestIdOf(ev);
        const turnId = turnIdOf(ev);
        return (
          <article
            key={ev.id}
            className={`council-event${isKnown ? "" : " unknown"}`}
          >
            <div className="ev-meta">
              #{ev.sequence} · {ev.createdAt}
              {ev.memberId ? ` · ${ev.memberId}` : ""}
              {ev.round ? ` · round ${ev.round}` : ""}
              {ev.usage &&
              (ev.usage.inputTokens != null || ev.usage.outputTokens != null) ? (
                <span
                  className="ev-tokens"
                  title={`${ev.usage.inputTokens ?? 0} input · ${ev.usage.outputTokens ?? 0} output tokens`}
                >
                  {" · "}↓{(ev.usage.inputTokens ?? 0).toLocaleString()} ↑
                  {(ev.usage.outputTokens ?? 0).toLocaleString()} tok
                </span>
              ) : null}
            </div>
            <div className="ev-type">
              {isKnown ? ev.type : `Unknown event (${ev.type})`}
              {isFake && <span className="fake-badge">fake</span>}
              {ev.payload?.dialect_fallback === true && (
                <span className="fake-badge">fallback</span>
              )}
              {" · "}
              <span className="ev-status">{ev.status}</span>
              {manifestId && onInspect && turnId && ev.round != null && (
                <button
                  className="inspect-btn"
                  onClick={() =>
                    onInspect({
                      round: ev.round as number,
                      memberId: ev.memberId,
                      turnId,
                    })
                  }
                  aria-label={`Inspect round ${ev.round}`}
                  type="button"
                >
                  Inspect
                </button>
              )}
            </div>
            <div className="ev-payload"><PayloadBody ev={ev} /></div>
          </article>
        );
      })}
    </div>
  );
}
