// F033 — In-app Council room member editor.
//
// Mounted from CouncilShell when the user clicks "Edit room" on the
// selected room. Lets the operator add/edit/delete/enable/disable
// members + reorder for speaker_order, then PUT the updated room.
//
// Schema-aware fields the editor edits explicitly:
// - members[].id, name, enabled, gateway_route_id, provider_kind,
//   context_access, transcript_access, system_prompt
// - topology.speaker_order (matches members[].id order)
//
// Everything else (budget, finalization, ui, metadata, _extras…) is
// round-tripped verbatim.
import { useCallback, useEffect, useMemo, useState } from "react";
import CouncilQuickStartGuide from "./CouncilQuickStartGuide";
import { sidecarHealth } from "../../lib/api";
import type {
  ProviderListItem,
  RouteAvailabilityItem,
  RouteListItem,
} from "../../lib/api/providerKeys";
import {
  listGatewayProviders,
  listGatewayRoutes,
  listModelAvailability,
} from "../../lib/api/providerKeys";
import type { RoomValidation } from "../../lib/api/councilRoom";
import { exportRoomProfile } from "../../lib/api/councilProfile";
import {
  getRoomFull,
  putRoom,
} from "../../lib/api/councilRoom";
import { listCorpora } from "../../lib/api/corpus";
import type { CorpusSummary } from "../../lib/api/corpus";
import InfoBubble from "../../components/InfoBubble";
import CorpusPicker from "../corpus/CorpusPicker";

interface Props {
  roomId: string;
  onClose: () => void;
  onSaved: () => void;
}

// Plain-language explanations surfaced via the ⓘ bubble next to each setting.
// Kept here so the wording is in one place and matches docs/COUNCIL_ROOM_SETTINGS.md.
const TIP: Record<string, string> = {
  provider:
    "Which service runs this member's model — Local (Ollama) runs on your machine; Anthropic/OpenAI/Google call that provider's API (needs a key in Settings).",
  route:
    "The specific model this member uses, e.g. llama3.2:3b or claude-sonnet-4-6. Only models for the chosen provider are listed.",
  cliPool:
    "Pick a connected subscription CLI (Claude, Codex, Cursor) to add all of its available models to this member's pool at once. You can then uncheck any you don't want below.",
  contextAccess:
    "How much this member is allowed to see. prompt_only = just the question; retrieved_snippets = relevant corpus passages; redacted_summary = a sanitized summary; full_context = everything. Less = more private and cheaper.",
  transcriptAccess:
    "How much of the other members' conversation this member sees. none = nobody else's messages; own_messages = only its own; all_messages = the whole discussion. Needed for members to react to each other.",
  maxOutput:
    "The per-turn answer budget (Ollama num_predict). Reasoning models (Qwen, DeepSeek-R1) spend this on hidden thinking before answering — set it high (4096–8192) or they may produce no visible answer. Blank uses the default (2048).",
  systemPrompt:
    "Sets this member's persona and behavior. Leave blank for a neutral 'answer directly, don't role-play' default, or write your own — e.g. make one member a stubborn skeptic the others must win over.",
  topologyKind:
    "How members take turns. Round robin: each answers once per round in order. Consensus deliberation: round 1 is blind, then members refine each round until they agree (or rounds run out). Credibility: members surface and verify cited claims. These are the topologies the engine runs.",
  maxRounds:
    "The maximum number of rounds (full passes through the members). Consensus needs room to converge — use 3–4. The run always stops here even if no consensus.",
  maxMessages:
    "The most messages a single member may post in the whole run. Usually 1 per round.",
  maxTotalTurns:
    "A hard ceiling on total turns across all members, regardless of rounds — a safety stop for long runs.",
  consensusThreshold:
    "How many members must signal 'no change' for the run to stop early as agreed. 0 means all enabled members must agree. Only used by the consensus topology.",
  maxModelCalls:
    "Hard cap on total model calls for the whole run. Must be at least one per enabled member. The run stops cleanly when it's hit.",
  maxOutputPerTurn:
    "Room-wide output-token cap. NOT enforced yet — to limit a member's output today, use that member's 'Max output tokens' field above.",
  maxInputPerTurn:
    "Room-wide input-token cap. Not enforced yet (recorded for a future release).",
  finalizationMode:
    "How the run's final answer is chosen. transcript_only = the last message is the answer; single_finalizer = a named member writes it; consensus_report = a synthesizer writes the agreed answer; summary = a rapporteur writes an abstractive summary that preserves disagreement (runs on any ending, not just consensus); credibility_report = a verified-citation report. Vote summary and judge verdict are not implemented yet (shown disabled).",
  finalizerMember:
    "The member whose last message becomes the final answer. Only used when mode is 'single finalizer'.",
  judgeMember:
    "The member that acts as the neutral judge. It never takes a deliberation turn and holds no opinion of its own — it only reads each round and decides whether the others have reached a verdict (and can break a tie at the round limit). Pick a strong, impartial model.",
  deliberationStyle:
    "Telegraphic asks members to be terse (saves tokens) during deliberation; the final answer is never abbreviated. Natural is normal prose.",
  intermediateCap:
    "Caps output tokens on intermediate (non-final) messages when telegraphic style or digest_v1 dialect is on. Blank = no extra cap.",
  deliberationDialect:
    "digest_v1 asks members to emit a small structured JSON position (needed for the consensus topology to detect agreement). Prose is normal text.",
  citationReferences:
    "Replaces repeated source passages with short [c:1] markers plus a lookup table, so the same corpus text isn't re-sent in full each turn.",
  compaction:
    "Summarizes older rounds while keeping the most recent ones verbatim, to keep long deliberations within the token budget.",
  fullRoundsWindow:
    "How many of the most recent rounds stay verbatim (uncompacted) before older rounds get summarized.",
  segmentSize:
    "How many older rounds are grouped together into each summary segment.",
  onSummaryUnavailable:
    "If a summary can't be produced: 'structural' drops the text and keeps only metadata; 'verbatim' keeps the original rounds unchanged.",
  promptCacheHints:
    "Marks the stable part of the context so providers that support prompt caching (e.g. Anthropic) can reuse it and bill less.",
  steward:
    "Builds a compact, inspectable Steward Packet from older council messages so members can receive the important state without re-sending the full transcript. The user-facing transcript still stays complete.",
  stewardAssignment:
    "External uses a separate model only for packet maintenance. Existing member reuses one enabled council member as the steward, which costs less but can bias that member's ordinary council role.",
  stewardRoute:
    "The model used when the steward is external. Local routes keep packet maintenance on your machine. Remote routes require explicit remote-steward approval and remote budget.",
  stewardPacketMode:
    "Hybrid keeps structured facts plus short natural summaries. Structured is compact and machine-oriented. Narrative is easier to read but usually larger.",
  stewardCadence:
    "When new packets are produced. after_each_round is implemented now; on_demand records intent for later runtime triggers.",
  stewardRecent:
    "How many of the latest member messages still go to each member verbatim. Older messages covered by the latest packet are replaced by the packet.",
  stewardPacketTokens:
    "Approximate maximum Steward Packet size. Values below 128 are rejected because the packet cannot carry enough source-linked state.",
  stewardCalls:
    "Maximum external steward model calls reserved for a run. With after_each_round, set this at least to max rounds when using an external steward.",
  stewardFallback:
    "What happens if packet creation is unavailable. full_transcript preserves current behavior; stop is strict and can block future runs.",
};

const CONTEXT_ACCESS_OPTIONS = [
  "prompt_only",
  "task_instructions",
  "retrieved_snippets",
  "redacted_summary",
  "full_context",
];

const TRANSCRIPT_ACCESS_OPTIONS = [
  "none",
  "own_messages",
  "all_messages",
];

type MemberPrivacyPreset = "private" | "grounded" | "full" | "custom";

const MEMBER_PRIVACY_PRESETS: {
  value: MemberPrivacyPreset;
  label: string;
}[] = [
  { value: "private", label: "Private prompt only" },
  { value: "grounded", label: "Sources + discussion" },
  { value: "full", label: "Full workspace context" },
  { value: "custom", label: "Custom advanced settings" },
];

function memberPrivacyPreset(m: MemberDraft): MemberPrivacyPreset {
  if (
    m.context_access === "prompt_only" &&
    m.transcript_access === "own_messages"
  ) {
    return "private";
  }
  if (
    m.context_access === "retrieved_snippets" &&
    m.transcript_access === "all_messages"
  ) {
    return "grounded";
  }
  if (
    m.context_access === "full_context" &&
    m.transcript_access === "all_messages"
  ) {
    return "full";
  }
  return "custom";
}

function memberPrivacyPatch(
  preset: MemberPrivacyPreset,
): Pick<MemberDraft, "context_access" | "transcript_access"> | null {
  if (preset === "private") {
    return {
      context_access: "prompt_only",
      transcript_access: "own_messages",
    };
  }
  if (preset === "grounded") {
    return {
      context_access: "retrieved_snippets",
      transcript_access: "all_messages",
    };
  }
  if (preset === "full") {
    return {
      context_access: "full_context",
      transcript_access: "all_messages",
    };
  }
  return null;
}

// F087: Coding Mode role for a member. Stored in metadata.coding_role.
const CODING_ROLE_OPTIONS = [
  { value: "", label: "None" },
  { value: "pm", label: "PM (directs the team)" },
  { value: "dev", label: "Developer (writes code + tests)" },
  { value: "reviewer", label: "Reviewer" },
  { value: "tester", label: "Tester (runs/validates)" },
];

interface MemberDraft {
  id: string;
  name: string;
  enabled: boolean;
  provider_kind: string;
  gateway_route_id: string;
  model_mode: "single" | "multi";
  model_pool: string[];
  context_access: string;
  transcript_access: string;
  system_prompt: string;
  // turn_limits.max_output_tokens — the per-turn output (and reasoning) budget
  // sent to the model. "" means "use the room/engine default". Reasoning
  // models need a high value or they burn the budget on thinking with no
  // visible answer.
  max_output_tokens: string;
  // F084: designated steelman advocate. When on, this member argues
  // `steelman_topic` as forcefully as possible and may construct supporting
  // evidence/citations; its claims are labeled unverified + quarantined
  // (never source-supported, never promoted to the corpus). Stored in
  // metadata.steelman / metadata.steelman_topic.
  steelman: boolean;
  steelman_topic: string;
  // F087: this member's role when the room is run in Coding Mode — "" (none) |
  // pm | dev | reviewer | tester. Stored in metadata.coding_role.
  coding_role: string;
  // Round-trip-only fields (not edited but preserved).
  _extra: Record<string, unknown>;
  // Other turn_limits subfields the editor doesn't surface, preserved on save.
  _turn_limits_extra: Record<string, unknown>;
  // Other metadata keys the editor doesn't surface, preserved on save.
  _metadata_extra: Record<string, unknown>;
}

// QA 2026-06-12: surface topology / budget / finalization controls.
// Everything below was previously only settable via PUT /council/rooms;
// now it's editable in the room editor.
interface TopologyDraft {
  kind: string;
  max_rounds: number;
  max_messages_per_member: number;
  max_total_turns: number;
  allow_user_interjection: boolean;
  // Only consulted by ConsensusDeliberationTopology; 0 = "all enabled members"
  consensus_threshold: number;
}

interface BudgetDraft {
  max_total_model_calls: number;
  max_output_tokens_per_turn: number;
  max_input_tokens_per_turn: number;
  max_remote_calls_per_run: number;
  max_steward_calls_per_run: number;
  max_remote_steward_calls_per_run: number;
}

interface BudgetFloorInput {
  enabledCount: number;
  maxRounds: number;
  remoteCount: number;
  maxStewardCallsPerRun?: number;
  stewardIsRemote?: boolean;
  maxCalloutsPerRun?: number;
  remoteCalloutTargetCount?: number;
}

// A provider is "remote" (counts against the remote-call budget) when it
// isn't the local Ollama path or the test-only fake path. Defined as a
// denylist so any non-local provider — the F034 API providers
// (anthropic/openai/google/custom) AND the F040 subscription CLIs
// (claude_cli/codex_cli/cursor_cli) AND anything added later — is counted as remote
// without needing to extend an allowlist. This mirrors the backend gateway
// classifier, which treats every route that isn't local./fake. as remote.
const LOCAL_PROVIDER_KINDS = new Set(["local", "fake"]);

export function isRemoteProviderKind(kind: string): boolean {
  return kind !== "" && !LOCAL_PROVIDER_KINDS.has(kind);
}

// F040-01 — subscription CLI providers gate on `connected` (a verified login),
// not merely `configured` (the binary is installed). An installed-but-logged-out
// CLI must NOT be blindly offered as assignable.
export function isCliProviderClass(cls: string): boolean {
  return cls.endsWith("_cli");
}

// Whether a provider option should be selectable in the room editor:
// - CLI providers: only when the cached probe reports `connected === true`.
//   `configured` (installed) alone is not enough.
// - everything else: when `configured`.
export function isProviderSelectable(p: ProviderListItem): boolean {
  if (isCliProviderClass(p.provider_class)) return p.connected === true;
  return p.configured;
}

// "Installed but not connected" — the case that gets a "Set up →" affordance
// instead of a blind enable or a dead grey option.
export function isCliNeedsSetup(p: ProviderListItem): boolean {
  return (
    isCliProviderClass(p.provider_class) &&
    p.configured &&
    p.connected !== true
  );
}

// F135 — deep-link to the Settings tab (provider-keys / CLI setup). Reused by
// both Single-mode ("Set up subscription CLIs →") and the Multi CLI picker.
function navigateToSettings(): void {
  window.dispatchEvent(
    new CustomEvent("errorta:navigate", { detail: { view: "settings" } }),
  );
}

// F135 — the provider_class prefix of a gateway route_id (e.g. "claude_cli" from
// "claude_cli.opus"). Robust to routes that are no longer in the live catalog.
export function routeProviderClass(routeId: string): string {
  return routeId.split(".", 1)[0] ?? "unknown";
}

// F135 — map gateway model-availability reason codes to human-readable text for
// the Multi "Allowed models" pool. Unknown codes pass through verbatim.
const POOL_REASON_LABELS: Record<string, string> = {
  no_api_key: "needs an API key",
  family_disabled: "family disabled in Settings",
  cli_not_connected: "CLI not connected",
  model_not_installed: "not installed in Ollama",
  route_unavailable: "unavailable",
};
export function reasonLabel(reason: string | null | undefined): string {
  if (!reason) return "unavailable";
  return POOL_REASON_LABELS[reason] ?? reason;
}

// F135 — group ordering for the Multi pool: subscription CLIs first (they are
// the point of Multi model mode), then API providers, then local/fake last.
const POOL_GROUP_LAST = new Set(["local", "fake"]);
export function poolGroupRank(providerClass: string): number {
  if (isCliProviderClass(providerClass)) return 0;
  if (POOL_GROUP_LAST.has(providerClass)) return 2;
  return 1;
}

interface PooledRoute {
  route_id: string;
  label: string;
  family: string | null;
  providerClass: string;
}

// F135 — group pooled routes by provider_class, ordered CLIs → API → local/fake,
// then alphabetically within a rank. Pure; unit-tested.
export function groupPooledRoutes(
  routes: PooledRoute[],
): { providerClass: string; routes: PooledRoute[] }[] {
  const byProvider = new Map<string, PooledRoute[]>();
  for (const r of routes) {
    const arr = byProvider.get(r.providerClass) ?? [];
    arr.push(r);
    byProvider.set(r.providerClass, arr);
  }
  return [...byProvider.entries()]
    .map(([providerClass, rs]) => ({ providerClass, routes: rs }))
    .sort(
      (a, b) =>
        poolGroupRank(a.providerClass) - poolGroupRank(b.providerClass) ||
        a.providerClass.localeCompare(b.providerClass),
    );
}

export function computeBudgetFloor({
  enabledCount,
  maxRounds,
  remoteCount,
  maxStewardCallsPerRun = 0,
  stewardIsRemote = false,
  maxCalloutsPerRun = 0,
  remoteCalloutTargetCount = 0,
}: BudgetFloorInput): {
  maxTotalModelCallsFloor: number;
  maxRemoteCallsPerRunFloor: number;
} {
  const ordinaryCalls = Math.max(0, enabledCount) * Math.max(1, maxRounds);
  const stewardCalls = Math.max(0, maxStewardCallsPerRun);
  const calloutCalls = Math.max(0, maxCalloutsPerRun);
  // Each remote member is called once per round, so the per-run remote cap
  // must cover remoteCount * rounds — otherwise a multi-round room with
  // remote/CLI members stops early at limits_exhausted mid-run.
  const remoteCalls =
    Math.max(0, remoteCount) * Math.max(1, maxRounds) +
    (stewardIsRemote ? stewardCalls : 0) +
    Math.max(0, remoteCalloutTargetCount);
  return {
    maxTotalModelCallsFloor: ordinaryCalls + stewardCalls + calloutCalls,
    maxRemoteCallsPerRunFloor: remoteCalls,
  };
}

interface FinalizationDraft {
  mode: string;
  finalizer_member_id: string;
}

function topologyFromRaw(raw: unknown): TopologyDraft {
  const r = (raw ?? {}) as Record<string, unknown>;
  return {
    kind: typeof r.kind === "string" && r.kind ? r.kind : "round_robin",
    max_rounds: Number(r.max_rounds ?? 1),
    max_messages_per_member: Number(r.max_messages_per_member ?? 1),
    max_total_turns: Number(r.max_total_turns ?? 6),
    allow_user_interjection: Boolean(r.allow_user_interjection),
    consensus_threshold: Number(r.consensus_threshold ?? 0),
  };
}

function topologyToRaw(d: TopologyDraft): Record<string, unknown> {
  const out: Record<string, unknown> = {
    kind: d.kind,
    max_rounds: d.max_rounds,
    max_messages_per_member: d.max_messages_per_member,
    max_total_turns: d.max_total_turns,
    allow_user_interjection: d.allow_user_interjection,
  };
  if (d.consensus_threshold > 0) {
    out.consensus_threshold = d.consensus_threshold;
  }
  return out;
}

function budgetFromRaw(raw: unknown): BudgetDraft {
  const r = (raw ?? {}) as Record<string, unknown>;
  return {
    max_total_model_calls: Number(r.max_total_model_calls ?? 0),
    max_output_tokens_per_turn: Number(r.max_output_tokens_per_turn ?? 512),
    max_input_tokens_per_turn: Number(r.max_input_tokens_per_turn ?? 4096),
    max_remote_calls_per_run: Number(r.max_remote_calls_per_run ?? 0),
    max_steward_calls_per_run: Number(r.max_steward_calls_per_run ?? 0),
    max_remote_steward_calls_per_run: Number(
      r.max_remote_steward_calls_per_run ?? 0,
    ),
  };
}

function budgetToRaw(d: BudgetDraft): Record<string, unknown> {
  return {
    max_total_model_calls: d.max_total_model_calls,
    max_output_tokens_per_turn: d.max_output_tokens_per_turn,
    max_input_tokens_per_turn: d.max_input_tokens_per_turn,
    max_remote_calls_per_run: d.max_remote_calls_per_run,
    max_steward_calls_per_run: d.max_steward_calls_per_run,
    max_remote_steward_calls_per_run: d.max_remote_steward_calls_per_run,
  };
}

function finalizationFromRaw(raw: unknown): FinalizationDraft {
  const r = (raw ?? {}) as Record<string, unknown>;
  return {
    mode: typeof r.mode === "string" && r.mode ? r.mode : "transcript_only",
    finalizer_member_id:
      typeof r.finalizer_member_id === "string"
        ? r.finalizer_member_id
        : "",
  };
}

function finalizationToRaw(d: FinalizationDraft): Record<string, unknown> {
  return {
    mode: d.mode,
    finalizer_member_id: d.finalizer_member_id || null,
  };
}

interface ContextEfficiencyDraft {
  deliberation_style: "natural" | "telegraphic";
  intermediate_max_output_tokens: string;
  deliberation_dialect: "prose" | "digest_v1";
  citation_references: boolean;
  compaction_enabled: boolean;
  compaction_full_rounds_window: string;
  compaction_segment_size_rounds: string;
  on_summary_unavailable: "structural" | "verbatim";
  prompt_cache_hints: boolean;
  // Round-trip-only: top-level and transcript_compaction subfields the editor
  // doesn't know about are preserved here and spread back on save.
  _extra: Record<string, unknown>;
  _compaction_extra: Record<string, unknown>;
}

function efficiencyFromRaw(raw: unknown): ContextEfficiencyDraft {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const c = (
    r.transcript_compaction && typeof r.transcript_compaction === "object"
      ? r.transcript_compaction
      : {}
  ) as Record<string, unknown>;
  // Capture any subfields the editor doesn't render so they round-trip on save.
  const {
    deliberation_style: _ds,
    intermediate_max_output_tokens: _imot,
    deliberation_dialect: _dd,
    citation_references: _cr,
    transcript_compaction: _tc,
    prompt_cache_hints: _pch,
    ..._extra
  } = r;
  const {
    enabled: _ce,
    full_rounds_window: _frw,
    segment_size_rounds: _ssr,
    on_summary_unavailable: _osu,
    ..._compaction_extra
  } = c;
  return {
    deliberation_style:
      r.deliberation_style === "telegraphic" ? "telegraphic" : "natural",
    intermediate_max_output_tokens:
      r.intermediate_max_output_tokens == null
        ? ""
        : String(r.intermediate_max_output_tokens),
    deliberation_dialect:
      r.deliberation_dialect === "digest_v1" ? "digest_v1" : "prose",
    citation_references: Boolean(r.citation_references),
    compaction_enabled: Boolean(c.enabled),
    compaction_full_rounds_window: String(c.full_rounds_window ?? 2),
    compaction_segment_size_rounds: String(c.segment_size_rounds ?? 4),
    on_summary_unavailable:
      c.on_summary_unavailable === "verbatim" ? "verbatim" : "structural",
    prompt_cache_hints: Boolean(r.prompt_cache_hints),
    _extra,
    _compaction_extra,
  };
}

function efficiencyToRaw(d: ContextEfficiencyDraft): Record<string, unknown> {
  const cap = Number.parseInt(d.intermediate_max_output_tokens, 10);
  const window = Number.parseInt(d.compaction_full_rounds_window, 10);
  const segment = Number.parseInt(d.compaction_segment_size_rounds, 10);
  return {
    ...d._extra,
    deliberation_style: d.deliberation_style,
    intermediate_max_output_tokens: Number.isFinite(cap) && cap > 0 ? cap : null,
    deliberation_dialect: d.deliberation_dialect,
    citation_references: d.citation_references,
    transcript_compaction: {
      ...d._compaction_extra,
      enabled: d.compaction_enabled,
      full_rounds_window: Number.isFinite(window) && window > 0 ? window : 2,
      segment_size_rounds: Number.isFinite(segment) && segment > 0 ? segment : 4,
      on_summary_unavailable: d.on_summary_unavailable,
    },
    prompt_cache_hints: d.prompt_cache_hints,
  };
}

interface StewardDraft {
  enabled: boolean;
  assignment_mode: "external" | "member" | string;
  assignment_member_id: string;
  assignment_provider_kind: string;
  assignment_gateway_route_id: string;
  assignment_name: string;
  packet_mode: string;
  recipient_mode: string;
  cadence: string;
  recent_full_messages: number;
  max_packet_tokens: number;
  include_member_positions: boolean;
  include_open_disagreements: boolean;
  include_risk_flags: boolean;
  include_callout_recommendation: boolean;
  allow_raw_expansion: boolean;
  show_packet_audit_to_user: boolean;
  fallback_on_failure: string;
  remote_steward_allowed: boolean;
  _extra: Record<string, unknown>;
  _assignment_extra: Record<string, unknown>;
}

function stewardFromRaw(raw: unknown): StewardDraft {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const assignment = (
    r.assignment && typeof r.assignment === "object" ? r.assignment : {}
  ) as Record<string, unknown>;
  const {
    enabled: _enabled,
    assignment: _assignment,
    packet_mode: _packetMode,
    recipient_mode: _recipientMode,
    cadence: _cadence,
    recent_full_messages: _recent,
    max_packet_tokens: _maxPacket,
    include_member_positions: _includePositions,
    include_open_disagreements: _includeDisagreements,
    include_risk_flags: _includeRisks,
    include_callout_recommendation: _includeCallout,
    allow_raw_expansion: _allowRaw,
    show_packet_audit_to_user: _showAudit,
    fallback_on_failure: _fallback,
    remote_steward_allowed: _remoteAllowed,
    ..._extra
  } = r;
  const {
    mode: _mode,
    member_id: _memberId,
    gateway_route_id: _gatewayRouteId,
    provider_kind: _providerKind,
    name: _name,
    ..._assignment_extra
  } = assignment;
  return {
    enabled: Boolean(r.enabled),
    assignment_mode:
      typeof assignment.mode === "string" && assignment.mode
        ? assignment.mode
        : "external",
    assignment_member_id:
      typeof assignment.member_id === "string" ? assignment.member_id : "",
    assignment_provider_kind:
      typeof assignment.provider_kind === "string" && assignment.provider_kind
        ? assignment.provider_kind
        : "local",
    assignment_gateway_route_id:
      typeof assignment.gateway_route_id === "string" &&
      assignment.gateway_route_id
        ? assignment.gateway_route_id
        : "local.summary-model",
    assignment_name:
      typeof assignment.name === "string" && assignment.name
        ? assignment.name
        : "Council Steward",
    packet_mode:
      typeof r.packet_mode === "string" && r.packet_mode ? r.packet_mode : "hybrid",
    recipient_mode:
      typeof r.recipient_mode === "string" && r.recipient_mode
        ? r.recipient_mode
        : "shared",
    cadence:
      typeof r.cadence === "string" && r.cadence
        ? r.cadence
        : "after_each_round",
    recent_full_messages: Number(r.recent_full_messages ?? 2),
    max_packet_tokens: Number(r.max_packet_tokens ?? 1200),
    include_member_positions: r.include_member_positions !== false,
    include_open_disagreements: r.include_open_disagreements !== false,
    include_risk_flags: r.include_risk_flags !== false,
    include_callout_recommendation: r.include_callout_recommendation !== false,
    allow_raw_expansion: r.allow_raw_expansion !== false,
    show_packet_audit_to_user: r.show_packet_audit_to_user !== false,
    fallback_on_failure:
      typeof r.fallback_on_failure === "string" && r.fallback_on_failure
        ? r.fallback_on_failure
        : "full_transcript",
    remote_steward_allowed: Boolean(r.remote_steward_allowed),
    _extra,
    _assignment_extra,
  };
}

function stewardToRaw(d: StewardDraft): Record<string, unknown> {
  return {
    ...d._extra,
    enabled: d.enabled,
    assignment: {
      ...d._assignment_extra,
      mode: d.assignment_mode,
      member_id:
        d.assignment_mode === "member" ? d.assignment_member_id || null : null,
      gateway_route_id:
        d.assignment_mode === "external"
          ? d.assignment_gateway_route_id || null
          : null,
      provider_kind: d.assignment_provider_kind,
      name: d.assignment_name || "Council Steward",
    },
    packet_mode: d.packet_mode,
    recipient_mode: d.recipient_mode,
    cadence: d.cadence,
    recent_full_messages: d.recent_full_messages,
    max_packet_tokens: d.max_packet_tokens,
    include_member_positions: d.include_member_positions,
    include_open_disagreements: d.include_open_disagreements,
    include_risk_flags: d.include_risk_flags,
    include_callout_recommendation: d.include_callout_recommendation,
    allow_raw_expansion: d.allow_raw_expansion,
    show_packet_audit_to_user: d.show_packet_audit_to_user,
    fallback_on_failure: d.fallback_on_failure,
    remote_steward_allowed: d.remote_steward_allowed,
  };
}

// F039 tool policy (default-off). Round-trips unknown sub-fields via _raw.
interface ToolsDraft {
  web_fetch_enabled: boolean;
  web_fetch_allowed_domains: string; // comma-separated
  web_search_enabled: boolean;
  web_search_searxng_url: string;
  code_read_enabled: boolean;
  code_read_workspace_path: string;
  code_write_enabled: boolean;
  code_write_mode: string; // propose_only | auto_apply
  code_exec_enabled: boolean;
  code_exec_network: boolean;
  code_exec_sandbox: string; // none | seatbelt | docker
  require_first_use_consent: boolean;
  _raw: Record<string, unknown>;
}

function toolsFromRaw(raw: unknown): ToolsDraft {
  const tp = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const sub = (k: string) =>
    (tp[k] && typeof tp[k] === "object" ? tp[k] : {}) as Record<string, unknown>;
  const wf = sub("web_fetch"), ws = sub("web_search");
  const cr = sub("code_read"), cw = sub("code_write"), ce = sub("code_exec");
  const ex = sub("execution");
  const domains = Array.isArray(wf.allowed_domains)
    ? (wf.allowed_domains as unknown[]).map(String).join(", ")
    : "";
  return {
    web_fetch_enabled: Boolean(wf.enabled),
    web_fetch_allowed_domains: domains,
    web_search_enabled: Boolean(ws.enabled),
    web_search_searxng_url: String(ws.searxng_url ?? ""),
    code_read_enabled: Boolean(cr.enabled),
    code_read_workspace_path: String(cr.workspace_path ?? ""),
    code_write_enabled: Boolean(cw.enabled),
    code_write_mode: String(cw.mode ?? "propose_only"),
    code_exec_enabled: Boolean(ce.enabled),
    code_exec_network: Boolean(ce.network),
    code_exec_sandbox: String(ex.sandbox ?? "none"),
    require_first_use_consent: tp.require_first_use_consent !== false,
    _raw: tp,
  };
}

function toolsToRaw(d: ToolsDraft): Record<string, unknown> {
  const base = { ...d._raw } as Record<string, unknown>;
  const sub = (k: string) =>
    (base[k] && typeof base[k] === "object" ? base[k] : {}) as Record<string, unknown>;
  const domains = d.web_fetch_allowed_domains
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return {
    ...base,
    web_fetch: { ...sub("web_fetch"), enabled: d.web_fetch_enabled, allowed_domains: domains },
    web_search: {
      ...sub("web_search"),
      enabled: d.web_search_enabled,
      ...(d.web_search_searxng_url ? { searxng_url: d.web_search_searxng_url } : {}),
    },
    code_read: {
      ...sub("code_read"),
      enabled: d.code_read_enabled,
      ...(d.code_read_workspace_path ? { workspace_path: d.code_read_workspace_path } : {}),
    },
    code_write: { ...sub("code_write"), enabled: d.code_write_enabled, mode: d.code_write_mode },
    code_exec: { ...sub("code_exec"), enabled: d.code_exec_enabled, network: d.code_exec_network },
    execution: { ...sub("execution"), sandbox: d.code_exec_sandbox },
    require_first_use_consent: d.require_first_use_consent,
  };
}

function memberFromRaw(raw: Record<string, unknown>): MemberDraft {
  const {
    id, name, enabled, provider_kind, gateway_route_id, model_mode, model_pool,
    context_access, transcript_access, system_prompt,
    turn_limits,
    ...rest
  } = raw;
  const tl = (turn_limits && typeof turn_limits === "object"
    ? turn_limits
    : {}) as Record<string, unknown>;
  const { max_output_tokens, ...tlExtra } = tl;
  // F084: pull steelman flags out of metadata into typed fields; keep any other
  // metadata keys in _metadata_extra so they round-trip untouched.
  const { metadata, ...restNoMeta } = rest as { metadata?: unknown } & Record<string, unknown>;
  const md = (metadata && typeof metadata === "object"
    ? metadata
    : {}) as Record<string, unknown>;
  const { steelman, steelman_topic, coding_role, ...mdExtra } = md;
  return {
    id: String(id ?? ""),
    name: String(name ?? ""),
    enabled: Boolean(enabled),
    provider_kind: String(provider_kind ?? "local"),
    gateway_route_id: String(gateway_route_id ?? ""),
    model_mode: model_mode === "multi" ? "multi" : "single",
    model_pool: Array.isArray(model_pool)
      ? model_pool.filter((route): route is string => typeof route === "string")
      : [],
    context_access: String(context_access ?? "prompt_only"),
    transcript_access: String(transcript_access ?? "own_messages"),
    system_prompt: String(system_prompt ?? ""),
    max_output_tokens: max_output_tokens == null ? "" : String(max_output_tokens),
    steelman: Boolean(steelman),
    steelman_topic: typeof steelman_topic === "string" ? steelman_topic : "",
    coding_role: typeof coding_role === "string" ? coding_role : "",
    _extra: restNoMeta,
    _turn_limits_extra: tlExtra,
    _metadata_extra: mdExtra,
  };
}

function memberToRaw(m: MemberDraft): Record<string, unknown> {
  const out: Record<string, unknown> = {
    ...m._extra,
    id: m.id,
    name: m.name,
    enabled: m.enabled,
    provider_kind: m.provider_kind,
    gateway_route_id: m.gateway_route_id,
    context_access: m.context_access,
    transcript_access: m.transcript_access,
    system_prompt: m.system_prompt,
  };
  if (m.model_mode === "multi") out.model_mode = "multi";
  if (m.model_pool.length > 0) out.model_pool = [...m.model_pool];
  const parsed = Number.parseInt(m.max_output_tokens, 10);
  const tl: Record<string, unknown> = { ...m._turn_limits_extra };
  if (Number.isFinite(parsed) && parsed > 0) {
    tl.max_output_tokens = parsed;
  }
  // Only attach turn_limits when there's something in it, so we don't write an
  // empty object onto members that never had one.
  if (Object.keys(tl).length > 0) {
    out.turn_limits = tl;
  }
  // F084: rebuild metadata from preserved keys + the typed steelman flags.
  // Toggling steelman off drops the keys (mdExtra never held them).
  const md: Record<string, unknown> = { ...m._metadata_extra };
  if (m.steelman) {
    md.steelman = true;
    md.steelman_topic = m.steelman_topic.trim();
  }
  // F087: persist the coding-mode role when set (dropped when "" / none).
  if (m.coding_role) {
    md.coding_role = m.coding_role;
  }
  if (Object.keys(md).length > 0) {
    out.metadata = md;
  }
  return out;
}

// Curated pool of distinctive member names. A freshly-added member gets one of
// these (skipping names already in the room) so new members don't all read as
// "Member N". Indexing starts at the add position so successive adds vary.
const MEMBER_NAME_POOL = [
  "Atlas",
  "Beacon",
  "Cipher",
  "Delta",
  "Echo",
  "Forge",
  "Gauge",
  "Harbor",
  "Indigo",
  "Juno",
  "Keystone",
  "Lumen",
  "Mercury",
  "Nova",
  "Onyx",
  "Pilot",
  "Quill",
  "Rook",
  "Sage",
  "Tempo",
  "Umbra",
  "Vertex",
  "Willow",
  "Xenon",
  "Yonder",
  "Zephyr",
] as const;

// Pick a name not already used in the room. Start scanning from `idx` into the
// pool so consecutive adds land on different names; fall back to "Member <idx>"
// only if every pool name is taken.
function pickMemberName(idx: number, existingNames: Iterable<string>): string {
  const taken = new Set<string>();
  for (const n of existingNames) {
    taken.add(n.trim().toLowerCase());
  }
  for (let i = 0; i < MEMBER_NAME_POOL.length; i++) {
    const name = MEMBER_NAME_POOL[(idx + i) % MEMBER_NAME_POOL.length];
    if (!taken.has(name.toLowerCase())) {
      return name;
    }
  }
  return `Member ${idx}`;
}

function pickRandomMemberNames(count: number, existingNames: Iterable<string>): string[] {
  const taken = new Set<string>();
  for (const n of existingNames) {
    taken.add(n.trim().toLowerCase());
  }
  const available = MEMBER_NAME_POOL.filter((name) => !taken.has(name.toLowerCase()));
  const shuffled = available
    .map((name) => ({ name, sort: Math.random() }))
    .sort((a, b) => a.sort - b.sort)
    .map((entry) => entry.name);
  const names: string[] = shuffled.slice(0, count);
  for (let i = names.length; i < count; i++) {
    names.push(`Member ${i + 1}`);
  }
  return names;
}

function newMemberDraft(idx: number, existing: MemberDraft[] = []): MemberDraft {
  return {
    id: `m-${idx}`,
    name: pickMemberName(idx, existing.map((m) => m.name)),
    enabled: true,
    provider_kind: "local",
    gateway_route_id: "local.ollama.llama3.2:3b",
    model_mode: "single",
    model_pool: [],
    context_access: "full_context",
    transcript_access: "all_messages",
    system_prompt: "",
    max_output_tokens: "",
    steelman: false,
    steelman_topic: "",
    coding_role: "",
    _extra: {},
    _turn_limits_extra: {},
    _metadata_extra: {},
  };
}

type CodingMemberSlot = {
  id: string;
  role: "pm" | "dev" | "reviewer" | "tester";
  provider: string;
  preferredRoutes: string[];
  maxOutputTokens: string;
  systemPrompt: string;
};

// Cursor members in the Coding preset run on Composer (Cursor's own coding
// model). Prefer the current Composer, then its fast variant, then the
// always-valid account default — `preferredRoute` resolves the first that
// exists in the LIVE catalog, so a Composer version bump can't strand the slot.
const CURSOR_CODING_ROUTES = [
  "cursor_cli.composer-2.5",
  "cursor_cli.composer-2.5-fast",
  "cursor_cli.default",
];

const CODING_MEMBER_SLOTS: CodingMemberSlot[] = [
  {
    id: "m-pm",
    role: "pm",
    provider: "claude_cli",
    preferredRoutes: ["claude_cli.opus", "claude_cli.claude-opus-4-8"],
    maxOutputTokens: "8192",
    systemPrompt:
      "You are the coding PM. Break the project into small tasks, assign dev, " +
      "reviewer, and tester work, enforce the definition of done, and stop the " +
      "team only when the product is ready for human review.",
  },
  {
    id: "m-dev-1",
    role: "dev",
    provider: "cursor_cli",
    preferredRoutes: CURSOR_CODING_ROUTES,
    maxOutputTokens: "8192",
    systemPrompt:
      "You are a coding developer. Implement small, testable changes, explain " +
      "file-level intent, and hand off with exact tests. Do not leave TODOs.",
  },
  {
    id: "m-dev-2",
    role: "dev",
    provider: "codex_cli",
    preferredRoutes: ["codex_cli.default"],
    maxOutputTokens: "8192",
    systemPrompt:
      "You are a coding developer. Focus on integration details, edge cases, " +
      "and keeping changes compatible with the existing codebase.",
  },
  {
    id: "m-dev-3",
    role: "dev",
    provider: "claude_cli",
    preferredRoutes: ["claude_cli.opus", "claude_cli.claude-opus-4-8"],
    maxOutputTokens: "8192",
    systemPrompt:
      "You are a coding developer. Take the hardest implementation slice, keep " +
      "the design simple, and produce reviewable, well-tested changes.",
  },
  {
    id: "m-review-1",
    role: "reviewer",
    provider: "claude_cli",
    preferredRoutes: ["claude_cli.opus", "claude_cli.claude-opus-4-8"],
    maxOutputTokens: "6144",
    systemPrompt:
      "You are a code reviewer. Look for correctness bugs, missed requirements, " +
      "regressions, security/privacy issues, and missing tests. Block unsafe work.",
  },
  {
    id: "m-review-2",
    role: "reviewer",
    provider: "cursor_cli",
    preferredRoutes: CURSOR_CODING_ROUTES,
    maxOutputTokens: "6144",
    systemPrompt:
      "You are a code reviewer. Focus on repository fit, maintainability, " +
      "frontend polish, and whether the implementation matches the user's intent.",
  },
  {
    id: "m-test-1",
    role: "tester",
    provider: "codex_cli",
    preferredRoutes: ["codex_cli.default"],
    maxOutputTokens: "6144",
    systemPrompt:
      "You are a tester. Turn requirements into executable checks, run and " +
      "interpret tests when tools allow it, and report the smallest failing case.",
  },
  {
    id: "m-test-2",
    role: "tester",
    provider: "cursor_cli",
    preferredRoutes: CURSOR_CODING_ROUTES,
    maxOutputTokens: "6144",
    systemPrompt:
      "You are a tester. Verify fixes end to end, watch for UI regressions, " +
      "and require evidence before accepting a completed task.",
  },
];

// Route-dropdown grouping. A live provider catalog can be long (Cursor exposes
// ~130 models), so group the <select> by model family into <optgroup>s with the
// account default surfaced as a top-level option. Short catalogs (most
// providers) stay flat — optgroups-of-one read worse than a plain list.
const ROUTE_GROUP_THRESHOLD = 10;
const FAMILY_GROUP_LABELS: Record<string, string> = {
  gpt: "GPT / Codex",
  codex: "GPT / Codex",
  claude: "Claude",
  opus: "Claude",
  sonnet: "Claude",
  haiku: "Claude",
  gemini: "Gemini",
  grok: "Grok",
};
// Preferred display order; any other group falls in afterwards, "Other" last.
const FAMILY_GROUP_ORDER = ["GPT / Codex", "Claude", "Gemini", "Grok"];

function routeModelId(routeId: string): string {
  const i = routeId.indexOf(".");
  return (i >= 0 ? routeId.slice(i + 1) : routeId).toLowerCase();
}

// Providers whose route catalog is authoritative (live-discovered or a fixed
// vendor alias set), so a saved route_id absent from the catalog means the
// model was DEPRECATED — not a deliberate free-form id on an HTTP provider.
const CATALOG_AUTHORITATIVE_PROVIDERS = new Set([
  "claude_cli",
  "codex_cli",
  "cursor_cli",
]);

// A saved route points at a model the provider no longer offers. Only flagged
// for authoritative-catalog providers with a non-empty live list (an empty list
// means discovery failed — we can't conclude staleness). `default`/`auto` are
// always-valid sentinels and never stale.
export function isRouteStale(
  routeId: string,
  provider: string,
  routesByProvider: Record<string, RouteListItem[]>,
): boolean {
  if (!routeId || !CATALOG_AUTHORITATIVE_PROVIDERS.has(provider)) return false;
  const model = routeModelId(routeId);
  if (model === "default" || model === "auto") return false;
  const routes = routesByProvider[provider] ?? [];
  if (routes.length === 0) return false;
  return !routes.some((r) => r.route_id === routeId);
}

// The valid route to fall back to when healing a stale pick: the provider's
// first catalog entry (account default for the CLIs), else `<provider>.default`.
export function fallbackRouteId(
  provider: string,
  routesByProvider: Record<string, RouteListItem[]>,
): string {
  return routesByProvider[provider]?.[0]?.route_id ?? `${provider}.default`;
}

export function groupRoutesByFamily(routes: RouteListItem[]): {
  leading: RouteListItem[];
  groups: { label: string; routes: RouteListItem[] }[];
} {
  const leading: RouteListItem[] = [];
  const byLabel = new Map<string, RouteListItem[]>();
  for (const r of routes) {
    const model = routeModelId(r.route_id);
    if (model === "default" || model === "auto") {
      leading.push(r);
      continue;
    }
    const label = FAMILY_GROUP_LABELS[(r.family ?? "").toLowerCase()] ?? "Other";
    const bucket = byLabel.get(label);
    if (bucket) bucket.push(r);
    else byLabel.set(label, [r]);
  }
  const labels = [...byLabel.keys()].sort((a, b) => {
    const ia = FAMILY_GROUP_ORDER.indexOf(a);
    const ib = FAMILY_GROUP_ORDER.indexOf(b);
    const ra = ia === -1 ? (a === "Other" ? Infinity : 500) : ia;
    const rb = ib === -1 ? (b === "Other" ? Infinity : 500) : ib;
    return ra !== rb ? ra - rb : a.localeCompare(b);
  });
  return { leading, groups: labels.map((label) => ({ label, routes: byLabel.get(label)! })) };
}

function RouteOption({ route }: { route: RouteListItem }) {
  return <option value={route.route_id}>{route.label}</option>;
}

// Renders the <option>/<optgroup> children for a route <select>. Flat below the
// grouping threshold; grouped by family above it.
function RouteOptions({ routes }: { routes: RouteListItem[] }) {
  if (routes.length <= ROUTE_GROUP_THRESHOLD) {
    return (
      <>
        {routes.map((r) => (
          <RouteOption key={r.route_id} route={r} />
        ))}
      </>
    );
  }
  const { leading, groups } = groupRoutesByFamily(routes);
  return (
    <>
      {leading.map((r) => (
        <RouteOption key={r.route_id} route={r} />
      ))}
      {groups.map((g) => (
        <optgroup key={g.label} label={g.label}>
          {g.routes.map((r) => (
            <RouteOption key={r.route_id} route={r} />
          ))}
        </optgroup>
      ))}
    </>
  );
}

function preferredRoute(
  routesByProvider: Record<string, RouteListItem[]>,
  provider: string,
  preferredRoutes: string[],
): string {
  const routes = routesByProvider[provider] ?? [];
  for (const routeId of preferredRoutes) {
    if (routes.some((route) => route.route_id === routeId)) return routeId;
  }
  for (const routeId of preferredRoutes) {
    const needle = routeId.toLowerCase().replace(`${provider}.`, "");
    const match = routes.find((route) => route.route_id.toLowerCase().includes(needle));
    if (match) return match.route_id;
  }
  return routes[0]?.route_id ?? preferredRoutes[0] ?? `${provider}.default`;
}

function buildCodingMembers(
  routesByProvider: Record<string, RouteListItem[]>,
  existingMembers: MemberDraft[],
): MemberDraft[] {
  const names = pickRandomMemberNames(
    CODING_MEMBER_SLOTS.length,
    existingMembers.map((m) => m.name),
  );
  return CODING_MEMBER_SLOTS.map((slot, idx) => ({
    id: slot.id,
    name: names[idx],
    enabled: true,
    provider_kind: slot.provider,
    gateway_route_id: preferredRoute(routesByProvider, slot.provider, slot.preferredRoutes),
    model_mode: "single",
    model_pool: [],
    context_access: "full_context",
    transcript_access: "all_messages",
    system_prompt: slot.systemPrompt,
    max_output_tokens: slot.maxOutputTokens,
    steelman: false,
    steelman_topic: "",
    coding_role: slot.role,
    _extra: {},
    _turn_limits_extra: {},
    _metadata_extra: {},
  }));
}

// Quick-setup presets: each applies a coherent bundle of draft settings. Most
// presets leave the members list untouched; Coding intentionally replaces it
// with a role-based PM/dev/reviewer/tester roster.
type PresetKey =
  | "quick"
  | "consensus"
  | "coding"
  | "saver"
  | "marathon"
  | "credibility";

// Marathon: a very high but finite round ceiling — the run still terminates
// early the moment the members converge ("decide they're done"). Truly
// unbounded is disallowed by the runnable-config rule (max_rounds must be a
// positive int); this is "open-ended for practical purposes".
const MARATHON_MAX_ROUNDS = 100;

const PRESETS: {
  key: PresetKey;
  label: string;
  shortLabel: string;
  blurb: string;
}[] = [
  {
    key: "quick",
    label: "Quick answers",
    shortLabel: "Quick answers",
    blurb:
      "One round, every member answers in plain prose. Fastest and cheapest.",
  },
  {
    key: "consensus",
    label: "Debate to consensus",
    shortLabel: "Debate",
    blurb:
      "Members deliberate over several rounds and converge, using the structured digest dialect.",
  },
  {
    key: "coding",
    label: "Coding",
    shortLabel: "Coding",
    blurb:
      "Seeds a resilient Coding Team: a strong Claude Opus PM frames small tasks for mixed Cursor, Claude, and Codex workers; malformed worker turns retry, reassign upward, then return to the PM for re-scoping.",
  },
  {
    key: "saver",
    label: "Token-saver",
    shortLabel: "Token-saver",
    blurb:
      "Telegraphic + digest + transcript compaction + cache hints + steward. Minimizes inter-member tokens.",
  },
  {
    key: "marathon",
    label: "Marathon",
    shortLabel: "Marathon",
    blurb:
      "Open-ended deliberation — members keep going up to 100 rounds and stop only when they agree they're done. A council leader (steward) plus compaction keep it sustainable. Give a big task and let them take it from there.",
  },
  {
    key: "credibility",
    label: "Credibility",
    shortLabel: "Credibility",
    blurb:
      "Source-backed factual answers: members research the web, post claims, peer-review each other's citations, and the leader writes a report that cites only verified, actually-fetched sources. Turns on web search + fetch.",
  },
];

function formatTopology(kind: string): string {
  if (kind === "consensus_deliberation") return "Consensus";
  if (kind === "round_robin") return "Round robin";
  if (kind === "credibility") return "Credibility";
  return kind.replaceAll("_", " ");
}

function formatFinalization(mode: string): string {
  if (mode === "transcript_only") return "Transcript";
  if (mode === "single_finalizer") return "Single finalizer";
  if (mode === "consensus_report") return "Consensus report";
  if (mode === "vote_summary") return "Vote summary";
  if (mode === "judged_final_answer") return "Judge verdict";
  return mode.replaceAll("_", " ");
}

function contextEfficiencySummary(e: ContextEfficiencyDraft): string {
  const parts: string[] = [];
  if (e.deliberation_style === "telegraphic") parts.push("telegraphic");
  if (e.deliberation_dialect === "digest_v1") parts.push("digest");
  if (e.citation_references) parts.push("citations");
  if (e.compaction_enabled) parts.push("compaction");
  if (e.prompt_cache_hints) parts.push("cache hints");
  if (e.intermediate_max_output_tokens.trim()) parts.push("output cap");
  return parts.length > 0 ? parts.join(", ") : "off";
}

function tokenSavingEnabled(e: ContextEfficiencyDraft): boolean {
  return (
    e.deliberation_style === "telegraphic" ||
    e.deliberation_dialect === "digest_v1" ||
    e.citation_references ||
    e.compaction_enabled ||
    e.prompt_cache_hints
  );
}

function stewardSummary(s: StewardDraft): string {
  if (!s.enabled) return "off";
  if (s.assignment_mode === "member") {
    return s.assignment_member_id
      ? `member ${s.assignment_member_id}`
      : "member steward";
  }
  return s.assignment_gateway_route_id
    ? `external ${s.assignment_gateway_route_id}`
    : "external steward";
}

function toolsSummary(t: ToolsDraft): string {
  const parts: string[] = [];
  if (t.web_fetch_enabled) parts.push("web fetch");
  if (t.web_search_enabled) parts.push("web search");
  if (t.code_read_enabled) parts.push("code read");
  if (t.code_write_enabled) parts.push("code write");
  if (t.code_exec_enabled) parts.push("code exec");
  return parts.length > 0 ? parts.join(", ") : "none granted";
}

export default function CouncilRoomEditor({ roomId, onClose, onSaved }: Props) {
  const [room, setRoom] = useState<Record<string, unknown> | null>(null);
  const [quickStartOpen, setQuickStartOpen] = useState(false);
  const [roomName, setRoomName] = useState("");
  const [members, setMembers] = useState<MemberDraft[]>([]);
  const [expectedRevision, setExpectedRevision] = useState(0);
  const [providers, setProviders] = useState<ProviderListItem[]>([]);
  const [efficiency, setEfficiency] = useState<ContextEfficiencyDraft>(
    efficiencyFromRaw({}),
  );
  const [steward, setSteward] = useState<StewardDraft>(
    stewardFromRaw({}),
  );
  const [tools, setTools] = useState<ToolsDraft>(toolsFromRaw({}));
  const [topology, setTopology] = useState<TopologyDraft>(topologyFromRaw({}));
  const [budget, setBudget] = useState<BudgetDraft>(budgetFromRaw({}));
  const [finalization, setFinalization] = useState<FinalizationDraft>(
    finalizationFromRaw({}),
  );
  const [corpusIds, setCorpusIds] = useState<string[]>([]);
  const [corpora, setCorpora] = useState<CorpusSummary[]>([]);
  const [corporaLoading, setCorporaLoading] = useState(false);
  const [routesByProvider, setRoutesByProvider] = useState<
    Record<string, RouteListItem[]>
  >({});
  const [routeAvailability, setRouteAvailability] = useState<
    Record<string, RouteAvailabilityItem>
  >({});
  const [validation, setValidation] = useState<RoomValidation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [activePreset, setActivePreset] = useState<PresetKey | null>(null);
  const [previewPreset, setPreviewPreset] = useState<PresetKey | null>(null);
  // F129 Slice 4/7: gate the "Multi" model-mode option behind the sidecar's
  // model_assignment_ready capability. Backend flips it true only when the
  // bound-route-before-policy invariant is proven safe (Slice 7). Default
  // undefined (loading) → treated as disabled until the flag arrives.
  const [modelAssignmentReady, setModelAssignmentReady] = useState<boolean | undefined>(undefined);
  useEffect(() => {
    let cancelled = false;
    sidecarHealth().then((h) => {
      if (!cancelled) setModelAssignmentReady(h?.features?.model_assignment_ready === true);
    }).catch(() => {
      if (!cancelled) setModelAssignmentReady(false);
    });
    return () => { cancelled = true; };
  }, []);

  // Initial load: room + providers + routes for every provider.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const roomResp = await getRoomFull(roomId);
        if (cancelled) return;
        const raw = roomResp.room;
        setRoom(raw);
        setRoomName(String(raw.name ?? ""));
        setValidation(roomResp.validation);
        setExpectedRevision(Number(raw.revision ?? 0));
        const rawMembers = Array.isArray(raw.members) ? raw.members : [];
        setMembers(
          rawMembers.map((m) => memberFromRaw(m as Record<string, unknown>)),
        );
        setEfficiency(efficiencyFromRaw(raw.context_efficiency));
        setSteward(stewardFromRaw(raw.steward_policy));
        setTools(toolsFromRaw(raw.tool_policy));
        setTopology(topologyFromRaw(raw.topology));
        setBudget(budgetFromRaw(raw.budget_policy));
        setFinalization(finalizationFromRaw(raw.finalization_policy));
        setCorpusIds(
          Array.isArray(raw.corpus_ids)
            ? raw.corpus_ids.filter((v): v is string => typeof v === "string")
            : [],
        );

        const provList = await listGatewayProviders();
        if (cancelled) return;
        setProviders(provList.providers);

        const routeMap: Record<string, RouteListItem[]> = {};
        for (const p of provList.providers) {
          try {
            const r = await listGatewayRoutes(p.provider_class);
            if (cancelled) return;
            routeMap[p.provider_class] = r.routes;
          } catch {
            routeMap[p.provider_class] = [];
          }
        }
        setRoutesByProvider(routeMap);
        try {
          const availability = await listModelAvailability();
          if (cancelled) return;
          setRouteAvailability(
            Object.fromEntries(availability.routes.map((item) => [item.route_id, item])),
          );
        } catch {
          setRouteAvailability({});
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [roomId]);

  useEffect(() => {
    let cancelled = false;
    setCorporaLoading(true);
    listCorpora()
      .then((items) => {
        if (!cancelled) setCorpora(items);
      })
      .catch(() => {
        if (!cancelled) setCorpora([]);
      })
      .finally(() => {
        if (!cancelled) setCorporaLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const markDirty = useCallback((updater: (m: MemberDraft[]) => MemberDraft[]) => {
    setMembers(updater);
    setDirty(true);
  }, []);

  const applyPreset = useCallback((key: PresetKey) => {
    if (key === "quick") {
      setTopology((t) => ({ ...t, kind: "round_robin", max_rounds: 1 }));
      setEfficiency((e) => ({
        ...e,
        deliberation_style: "natural",
        deliberation_dialect: "prose",
        compaction_enabled: false,
        prompt_cache_hints: false,
      }));
      setFinalization((f) => ({ ...f, mode: "transcript_only" }));
      setSteward((s) => ({ ...s, enabled: false }));
    } else if (key === "consensus") {
      setTopology((t) => ({ ...t, kind: "consensus_deliberation", max_rounds: 3 }));
      setEfficiency((e) => ({
        ...e,
        deliberation_style: "natural",
        deliberation_dialect: "digest_v1",
        citation_references: true,
        compaction_enabled: false,
      }));
      // consensus_report: after the members converge, one synthesizer turn
      // writes a single "Consensus" answer in the council's shared voice.
      setFinalization((f) => ({ ...f, mode: "consensus_report" }));
      setSteward((s) => ({ ...s, enabled: false }));
    } else if (key === "coding") {
      setMembers(buildCodingMembers(routesByProvider, members));
      setTopology((t) => ({
        ...t,
        kind: "round_robin",
        max_rounds: 8,
        max_messages_per_member: 8,
        max_total_turns: Math.max(t.max_total_turns, 64),
        allow_user_interjection: true,
        consensus_threshold: 0,
      }));
      setEfficiency((e) => ({
        ...e,
        deliberation_style: "natural",
        deliberation_dialect: "prose",
        citation_references: true,
        compaction_enabled: true,
        compaction_full_rounds_window: "3",
        compaction_segment_size_rounds: "4",
        on_summary_unavailable: "verbatim",
        prompt_cache_hints: true,
      }));
      setFinalization((f) => ({
        ...f,
        mode: "single_finalizer",
        finalizer_member_id: "m-pm",
      }));
      setSteward((s) => ({
        ...s,
        enabled: true,
        assignment_mode: "member",
        assignment_member_id: "m-pm",
        recent_full_messages: 4,
        max_packet_tokens: 2400,
        include_member_positions: true,
        include_open_disagreements: true,
        include_risk_flags: true,
        fallback_on_failure: "full_transcript",
      }));
      setTools((t) => ({
        ...t,
        code_read_enabled: true,
        code_write_enabled: true,
        code_write_mode: "propose_only",
        code_exec_enabled: true,
        code_exec_network: false,
        code_exec_sandbox: "seatbelt",
        require_first_use_consent: true,
      }));
      setBudget((b) => ({
        ...b,
        max_total_model_calls: Math.max(b.max_total_model_calls, 96),
        max_remote_calls_per_run: Math.max(b.max_remote_calls_per_run, 96),
        max_output_tokens_per_turn: Math.max(b.max_output_tokens_per_turn, 8192),
        max_input_tokens_per_turn: Math.max(b.max_input_tokens_per_turn, 32768),
        max_steward_calls_per_run: 0,
        max_remote_steward_calls_per_run: 0,
      }));
      setRoom((r) =>
        r
          ? {
              ...r,
              preset_id: "coding",
              credibility_policy:
                typeof r.credibility_policy === "object" && r.credibility_policy
                  ? {
                      ...(r.credibility_policy as Record<string, unknown>),
                      enabled: false,
                    }
                  : { enabled: false },
              judge_policy:
                typeof r.judge_policy === "object" && r.judge_policy
                  ? { ...(r.judge_policy as Record<string, unknown>), enabled: false }
                  : { enabled: false },
            }
          : r,
      );
    } else if (key === "saver") {
      setTopology((t) => ({ ...t, kind: "consensus_deliberation", max_rounds: 3 }));
      setEfficiency((e) => ({
        ...e,
        deliberation_style: "telegraphic",
        deliberation_dialect: "digest_v1",
        citation_references: true,
        compaction_enabled: true,
        prompt_cache_hints: true,
      }));
      setFinalization((f) => ({ ...f, mode: "consensus_report" }));
      setSteward((s) => ({ ...s, enabled: true }));
    } else if (key === "marathon") {
      // Open-ended: consensus topology stops the run the moment the members
      // agree, but the round ceiling is set very high so they can keep going
      // on a big task. The save path auto-bumps the budget floor from
      // max_rounds, so a long run is allowed without manual budget editing.
      setTopology((t) => ({
        ...t,
        kind: "consensus_deliberation",
        max_rounds: MARATHON_MAX_ROUNDS,
        max_messages_per_member: MARATHON_MAX_ROUNDS,
        max_total_turns: Math.max(t.max_total_turns, MARATHON_MAX_ROUNDS * 8),
      }));
      // Compaction is mandatory here — over dozens of rounds an uncompacted
      // transcript would blow the context window. digest_v1 keeps each
      // round's decisions structured; cache hints cut repeated-prefix cost.
      setEfficiency((e) => ({
        ...e,
        deliberation_style: "natural",
        deliberation_dialect: "digest_v1",
        citation_references: true,
        compaction_enabled: true,
        prompt_cache_hints: true,
      }));
      setFinalization((f) => ({ ...f, mode: "consensus_report" }));
      // The council leader: a steward curates a per-round packet so members
      // see a tight summary instead of the full ever-growing transcript.
      // Default to an existing member as leader; the user can reassign below.
      const leaderId =
        members.find((m) => m.enabled)?.id ?? members[0]?.id ?? "";
      setSteward((s) => ({
        ...s,
        enabled: true,
        assignment_mode: "member",
        assignment_member_id: leaderId,
      }));
    } else if (key === "credibility") {
      // Source-backed factual answers. Credibility topology + report
      // finalization, web tools ON (required), and the credibility policy
      // enabled on the room (preserved through the `...room` save spread).
      setTopology((t) => ({
        ...t,
        kind: "credibility",
        max_rounds: 4,
        max_messages_per_member: 4,
        max_total_turns: Math.max(t.max_total_turns, 24),
      }));
      setFinalization((f) => ({ ...f, mode: "credibility_report" }));
      setTools((t) => ({ ...t, web_search_enabled: true, web_fetch_enabled: true }));
      setSteward((s) => ({ ...s, enabled: false }));
      setRoom((r) =>
        r
          ? {
              ...r,
              credibility_policy: {
                ...(typeof r.credibility_policy === "object" && r.credibility_policy
                  ? (r.credibility_policy as Record<string, unknown>)
                  : {}),
                enabled: true,
                strictness: "normal",
                require_search: true,
                require_fetch: true,
                // F081: full debate rigor — the entailment gate (a claim must
                // be source-entailed to be admitted), novelty-exhaustion
                // termination (agreement never ends the run), and an
                // auto-assigned opponent who steelmans the opposing case.
                rigor: "adversarial",
                require_entailment: true,
                auto_assign_opponent: true,
                route_inference_to_validity: true,
              },
            }
          : r,
      );
    }
    setActivePreset(key);
    setDirty(true);
  }, [members, routesByProvider]);

  const updateMember = useCallback(
    (idx: number, patch: Partial<MemberDraft>) => {
      markDirty((m) =>
        m.map((row, i) => (i === idx ? { ...row, ...patch } : row)),
      );
    },
    [markDirty],
  );

  const handleProviderChange = useCallback(
    (idx: number, provider: string) => {
      // When provider changes, default to that provider's first route.
      const firstRoute = routesByProvider[provider]?.[0]?.route_id ?? "";
      updateMember(idx, {
        provider_kind: provider,
        gateway_route_id: firstRoute,
      });
    },
    [routesByProvider, updateMember],
  );

  const updateMemberPrivacy = useCallback(
    (idx: number, preset: MemberPrivacyPreset) => {
      const patch = memberPrivacyPatch(preset);
      if (patch) {
        updateMember(idx, patch);
      }
    },
    [updateMember],
  );

  const updateTokenSaving = useCallback((enabled: boolean) => {
    setEfficiency((cur) => ({
      ...cur,
      deliberation_style: enabled ? "telegraphic" : "natural",
      deliberation_dialect: enabled ? "digest_v1" : "prose",
      citation_references: enabled,
      compaction_enabled: enabled,
      prompt_cache_hints: enabled,
    }));
    setDirty(true);
  }, []);

  const updateTools = useCallback((patch: Partial<ToolsDraft>) => {
    setTools((cur) => ({ ...cur, ...patch }));
    setDirty(true);
  }, []);

  const updateSteward = useCallback((patch: Partial<StewardDraft>) => {
    setSteward((cur) => ({ ...cur, ...patch }));
    setDirty(true);
  }, []);

  const handleAdd = useCallback(() => {
    markDirty((m) => [...m, newMemberDraft(m.length + 1, m)]);
  }, [markDirty]);

  const handleDelete = useCallback(
    (idx: number) => {
      markDirty((m) => m.filter((_, i) => i !== idx));
    },
    [markDirty],
  );

  const handleMove = useCallback(
    (idx: number, direction: -1 | 1) => {
      markDirty((m) => {
        const target = idx + direction;
        if (target < 0 || target >= m.length) return m;
        const next = m.slice();
        [next[idx], next[target]] = [next[target], next[idx]];
        return next;
      });
    },
    [markDirty],
  );

  const handleSave = useCallback(async () => {
    if (!room) return;
    setBusy(true);
    setError(null);
    try {
      const speakerOrder = members.map((m) => m.id);
      const rawTopology = (room.topology as Record<string, unknown>) ?? {};
      const updatedTopology = {
        ...rawTopology,
        ...topologyToRaw(topology),
        speaker_order: speakerOrder,
      };
      // Auto-bump budget caps so adding members never fails validation on
      // a stale max_total_model_calls / max_remote_calls_per_run from
      // the seed. The runner enforces caps per-turn; raising the room
      // ceiling here just keeps the room loadable.
      const rawBudget = (room.budget_policy as Record<string, unknown>) ?? {};
      const enabledCount = members.filter((m) => m.enabled).length;
      const remoteCount = members.filter(
        (m) =>
          m.enabled &&
          isRemoteProviderKind(m.provider_kind),
      ).length;
      const stewardIsExternal = steward.enabled && steward.assignment_mode === "external";
      const stewardIsRemote =
        stewardIsExternal &&
        isRemoteProviderKind(steward.assignment_provider_kind);
      const stewardCallsPerRun = stewardIsExternal
        ? Math.max(1, budget.max_steward_calls_per_run || topology.max_rounds || 1)
        : 0;
      // F037: the editor has no callout config section, but escalation_policy /
      // escalation_roster round-trip via the `...room` spread. Their model-call
      // headroom must still be reflected in the budget floor, or a callout-
      // enabled room fails its own `callout_total_budget_impossible` check on
      // save. Mirror validation._validate_escalation.
      const escPolicy = (room.escalation_policy as Record<string, unknown>) ?? {};
      const escRoster =
        (room.escalation_roster as Array<Record<string, unknown>>) ?? [];
      const calloutsEnabled = escPolicy.enabled === true;
      const maxCalloutsPerRun = calloutsEnabled
        ? Number(escPolicy.max_callouts_per_run ?? 1)
        : 0;
      const remoteCalloutTargetCount = calloutsEnabled
        ? escRoster.filter((t) =>
            isRemoteProviderKind(String(t.provider_kind)),
          ).length
        : 0;
      const floor = computeBudgetFloor({
        enabledCount,
        maxRounds: topology.max_rounds,
        remoteCount,
        maxStewardCallsPerRun: stewardCallsPerRun,
        stewardIsRemote,
        maxCalloutsPerRun,
        remoteCalloutTargetCount,
      });
      const updatedBudget = {
        ...rawBudget,
        ...budgetToRaw(budget),
        max_total_model_calls: Math.max(
          floor.maxTotalModelCallsFloor,
          budget.max_total_model_calls,
          Number((rawBudget.max_total_model_calls as number) ?? 0),
        ),
        max_steward_calls_per_run: Math.max(
          stewardCallsPerRun,
          budget.max_steward_calls_per_run,
          Number((rawBudget.max_steward_calls_per_run as number) ?? 0),
        ),
        max_remote_steward_calls_per_run: Math.max(
          stewardIsRemote ? stewardCallsPerRun : 0,
          budget.max_remote_steward_calls_per_run,
          Number((rawBudget.max_remote_steward_calls_per_run as number) ?? 0),
        ),
        // If any remote members exist, lift the per-run remote cap to at
        // least cover them. Daily cap is left untouched.
        max_remote_calls_per_run: Math.max(
          floor.maxRemoteCallsPerRunFloor,
          budget.max_remote_calls_per_run,
          Number((rawBudget.max_remote_calls_per_run as number) ?? 0),
        ),
      };
      const rawFinalization =
        (room.finalization_policy as Record<string, unknown>) ?? {};
      const updatedFinalization = {
        ...rawFinalization,
        ...finalizationToRaw(finalization),
      };
      const updatedRoom = {
        ...room,
        name: roomName.trim() || "New room",
        members: members.map(memberToRaw),
        topology: updatedTopology,
        budget_policy: updatedBudget,
        finalization_policy: updatedFinalization,
        corpus_ids: corpusIds,
        context_efficiency: efficiencyToRaw(efficiency),
        steward_policy: stewardToRaw(steward),
        tool_policy: toolsToRaw(tools),
      };
      const resp = await putRoom(roomId, expectedRevision, updatedRoom);
      setRoom(resp.room);
      setRoomName(String(resp.room.name ?? ""));
      setValidation(resp.validation);
      setExpectedRevision(Number(resp.room.revision ?? expectedRevision + 1));
      setDirty(false);
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [room, roomName, members, efficiency, steward, tools, topology, budget, finalization, corpusIds, roomId, expectedRevision, onSaved]);

  const handleCancel = useCallback(() => {
    if (
      dirty &&
      !window.confirm("Discard your changes to this room?")
    ) {
      return;
    }
    onClose();
  }, [dirty, onClose]);

  const handleExportProfile = useCallback(async () => {
    try {
      const { yaml } = await exportRoomProfile(roomId);
      const blob = new Blob([yaml], { type: "text/yaml" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const slug = String(room?.name ?? roomId)
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "");
      a.download = `${slug || "council"}.council.yaml`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [roomId, room]);

  const errorMessages = useMemo(() => {
    if (!validation) return [];
    return validation.errors.map((e) => {
      const path = e.path ? `${e.path}: ` : "";
      const code = e.code ?? "error";
      return `${path}${code}`;
    });
  }, [validation]);

  const enabledMembers = useMemo(
    () => members.filter((m) => m.enabled),
    [members],
  );
  const remoteMemberCount = useMemo(
    () =>
      members.filter(
        (m) => m.enabled && isRemoteProviderKind(m.provider_kind),
      ).length,
    [members],
  );
  const calloutSummary = useMemo(() => {
    const escPolicy =
      room && typeof room.escalation_policy === "object"
        ? (room.escalation_policy as Record<string, unknown>)
        : {};
    const escRoster = Array.isArray(room?.escalation_roster)
      ? (room?.escalation_roster as Array<Record<string, unknown>>)
      : [];
    const enabled = escPolicy.enabled === true;
    const maxCallouts = enabled ? Number(escPolicy.max_callouts_per_run ?? 1) : 0;
    const remoteTargets = enabled
      ? escRoster.filter((t) =>
          isRemoteProviderKind(String(t.provider_kind ?? "")),
        ).length
      : 0;
    return {
      enabled,
      maxCallouts,
      targetCount: enabled ? escRoster.length : 0,
      remoteTargets,
    };
  }, [room]);
  const stewardIsExternal =
    steward.enabled && steward.assignment_mode === "external";
  const stewardCallsPerRun = stewardIsExternal
    ? Math.max(1, budget.max_steward_calls_per_run || topology.max_rounds || 1)
    : 0;
  const stewardIsRemote =
    stewardIsExternal && isRemoteProviderKind(steward.assignment_provider_kind);
  const budgetFloor = useMemo(
    () =>
      computeBudgetFloor({
        enabledCount: enabledMembers.length,
        maxRounds: topology.max_rounds,
        remoteCount: remoteMemberCount,
        maxStewardCallsPerRun: stewardCallsPerRun,
        stewardIsRemote,
        maxCalloutsPerRun: calloutSummary.maxCallouts,
        remoteCalloutTargetCount: calloutSummary.remoteTargets,
      }),
    [
      enabledMembers.length,
      topology.max_rounds,
      remoteMemberCount,
      stewardCallsPerRun,
      stewardIsRemote,
      calloutSummary.maxCallouts,
      calloutSummary.remoteTargets,
    ],
  );
  const contextSummary = contextEfficiencySummary(efficiency);
  const stewardStatusSummary = stewardSummary(steward);
  const toolStatusSummary = toolsSummary(tools);
  const saveTokensOnLongRuns = tokenSavingEnabled(efficiency);
  const showCodingRoles =
    activePreset === "coding" ||
    String(room?.preset_id ?? "") === "coding" ||
    members.some((m) => m.coding_role);
  const activeAdvanced = [
    contextSummary !== "off" ? "context" : null,
    steward.enabled ? "steward" : null,
    toolStatusSummary !== "none granted" ? "tools" : null,
    calloutSummary.enabled ? "callouts" : null,
  ].filter(Boolean);
  const selectedPreset = PRESETS.find((p) => p.key === (previewPreset ?? activePreset));

  if (error && !room) {
    return (
      <div className="council-room-editor" role="alert">
        Failed to load room: {error}
        <button onClick={onClose} type="button">Close</button>
      </div>
    );
  }
  if (!room) {
    return <div className="council-room-editor">Loading…</div>;
  }

  return (
    <section
      className="council-room-editor"
      aria-label="Council room editor"
      data-testid="council-room-editor"
    >
      <header className="cre-head">
        <div className="cre-title-block">
          <h3>Edit room</h3>
          <label className="cre-room-name-field">
            <span>Room name</span>
            <input
              type="text"
              value={roomName}
              onChange={(e) => {
                setRoomName(e.target.value);
                setDirty(true);
              }}
              placeholder="New room"
              data-testid="room-name-input"
            />
          </label>
          <p>{roomId}</p>
        </div>
        <div className="cre-status-strip" aria-label="Room editor status">
          <span className={`cre-status-pill ${dirty ? "is-dirty" : "is-clean"}`}>
            {dirty ? "Unsaved changes" : "Saved"}
          </span>
          <span className={`cre-status-pill cre-status-${validation?.status ?? "loading"}`}>
            {validation?.status ?? "loading"}
          </span>
          {errorMessages.length > 0 && (
            <span className="cre-status-pill is-error">
              {errorMessages.length} issue{errorMessages.length === 1 ? "" : "s"}
            </span>
          )}
        </div>
        <div className="cre-head-actions">
          <button
            onClick={() => setQuickStartOpen(true)}
            type="button"
            className="cqs-open-button"
            title="What each starting point and room mode does"
          >
            ? Quick Start
          </button>
          <button
            onClick={handleExportProfile}
            type="button"
            className="cre-export-profile"
            title="Download this room as a portable, secret-free profile"
          >
            Export profile
          </button>
          <button
            onClick={handleSave}
            disabled={busy || !dirty}
            type="button"
            data-testid="save-room"
          >
            {busy ? "Saving..." : "Save"}
          </button>
          <button onClick={handleCancel} type="button">
            {dirty ? "Cancel" : "Close"}
          </button>
        </div>
      </header>
      <section className="cre-summary-row" aria-label="Room summary">
        <div className="cre-summary-item">
          <span>Members</span>
          <strong>{enabledMembers.length}/{members.length} enabled</strong>
        </div>
        <div className="cre-summary-item">
          <span>Remote</span>
          <strong>{remoteMemberCount}</strong>
        </div>
        <div className="cre-summary-item">
          <span>Flow</span>
          <strong>
            {formatTopology(topology.kind)}, {topology.max_rounds} round
            {topology.max_rounds === 1 ? "" : "s"}
          </strong>
        </div>
        <div className="cre-summary-item">
          <span>Final</span>
          <strong>{formatFinalization(finalization.mode)}</strong>
        </div>
        <div className="cre-summary-item">
          <span>Corpora</span>
          <strong>{corpusIds.length > 0 ? corpusIds.length : "none"}</strong>
        </div>
        <div className="cre-summary-item">
          <span>Advanced</span>
          <strong>
            {activeAdvanced.length > 0 ? activeAdvanced.join(", ") : "off"}
          </strong>
        </div>
        <div className="cre-summary-item">
          <span>Budget floor</span>
          <strong>{budgetFloor.maxTotalModelCallsFloor} calls</strong>
        </div>
      </section>
      <p className="cre-section-hint cre-doc-pointer">
        Configure the room's members, flow, and final answer first. Advanced
        tuning stays collapsed below with active-state summaries. New to rooms?{" "}
        <button
          type="button"
          className="cqs-inline-link"
          onClick={() => setQuickStartOpen(true)}
        >
          Open the Quick Start guide
        </button>
        .
      </p>

      <section className="cre-presets" aria-label="Quick setup presets">
        <div className="cre-presets-head">
          <span className="cre-presets-title">Starting point</span>
          <span className="cre-presets-sub">
            Applies a draft preset; nothing saves until you click Save.
          </span>
        </div>
        <div className="cre-presets-row" role="group">
          {PRESETS.map((p) => (
            <button
              key={p.key}
              type="button"
              className={`cre-preset-btn${
                activePreset === p.key ? " is-active" : ""
              }`}
              onClick={() => applyPreset(p.key)}
              onFocus={() => setPreviewPreset(p.key)}
              onBlur={() => setPreviewPreset(null)}
              onMouseEnter={() => setPreviewPreset(p.key)}
              onMouseLeave={() => setPreviewPreset(null)}
              data-testid={`preset-${p.key}`}
              title={p.blurb}
              aria-pressed={activePreset === p.key}
            >
              <span className="cre-preset-label">{p.shortLabel}</span>
            </button>
          ))}
        </div>
        <p className="cre-preset-description">
          {selectedPreset
            ? selectedPreset.blurb
            : "Choose a preset to seed flow, final-answer, context-saving, and steward settings."}
        </p>
        {activePreset && (
          <p className="cre-preset-applied" role="status">
            Applied:{" "}
            <strong>
              {PRESETS.find((p) => p.key === activePreset)?.label}
            </strong>{" "}
            . Review changes before saving.
          </p>
        )}
      </section>

      {validation && (
        <div
          className={`cre-validation cre-validation-${validation.status}`}
          role="status"
        >
          status: <code>{validation.status}</code>
          {errorMessages.length > 0 && (
            <ul>
              {errorMessages.map((m, i) => (
                <li key={i}>{m}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {calloutSummary.enabled && (
        <p className="cre-section-hint cre-callout-preserved">
          This room has {calloutSummary.targetCount} expert callout target
          {calloutSummary.targetCount === 1 ? "" : "s"} configured. F075 keeps
          that policy intact and includes its headroom in budget summaries.
        </p>
      )}

      <section className="cre-corpora" aria-label="Council corpora">
        <h4>Corpora</h4>
        <CorpusPicker
          label="Room corpora"
          multiple
          value={corpusIds}
          onChange={(next) => {
            setCorpusIds(next);
            setDirty(true);
          }}
          corpora={corpora}
          loading={corporaLoading}
          noCorporaLabel="No corpora available for this room"
        />
      </section>

      <div className="cre-section-title-row">
        <div>
          <h4 className="cre-members-heading">Members</h4>
          <p className="cre-section-hint">
            One model per row. Pick provider/route first, then tune context,
            transcript, output budget, and persona only where needed.
          </p>
        </div>
        <button
          onClick={handleAdd}
          type="button"
          data-testid="add-member"
        >
          + Add member
        </button>
      </div>
      <ol className="cre-members" aria-label="Members">
        {members.map((m, idx) => {
          const privacyPreset = memberPrivacyPreset(m);
          const offeredRoutes = Object.entries(routesByProvider).flatMap(
            ([providerClass, routes]) => routes.map((route) => ({ ...route, providerClass })),
          );
          const offeredIds = new Set(offeredRoutes.map((route) => route.route_id));
          const pooledRoutes = [
            ...offeredRoutes,
            ...m.model_pool
              .filter((routeId) => !offeredIds.has(routeId))
              .map((routeId) => ({
                route_id: routeId,
                label: `${routeId} (saved, unavailable)`,
                family: null,
                providerClass: routeProviderClass(routeId),
              })),
          ];
          // F135 — CLI picker: connected subscription CLIs and the models each
          // would contribute to the pool (available routes only).
          const connectedClis = providers.filter(
            (p) => isCliProviderClass(p.provider_class) && p.connected === true,
          );
          const availableRoutesForCli = (cls: string) =>
            (routesByProvider[cls] ?? []).filter(
              (r) => routeAvailability[r.route_id]?.available === true,
            );
          // A CLI is "selected" iff any of its routes are already in the pool.
          // Derived from the pool — no separate persisted field.
          const selectedClis = connectedClis.filter((p) =>
            m.model_pool.some(
              (id) => routeProviderClass(id) === p.provider_class,
            ),
          );
          const poolGroups = groupPooledRoutes(pooledRoutes);
          const providerDisplayName = (cls: string) =>
            providers.find((p) => p.provider_class === cls)?.display_name ?? cls;
          return (
          <li
            key={`${m.id}-${idx}`}
            className="cre-member-row"
            data-testid={`member-row-${idx}`}
          >
            <div className="cre-row-top">
              <span className="cre-member-index">#{idx + 1}</span>
              <label>
                <input
                  type="checkbox"
                  checked={m.enabled}
                  onChange={(e) =>
                    updateMember(idx, { enabled: e.target.checked })
                  }
                  data-testid={`enable-${idx}`}
                />
                enabled
              </label>
              <div className="cre-member-route-summary">
                <strong>{m.name || m.id || `Member ${idx + 1}`}</strong>
                <span>
                  {m.model_mode === "multi"
                    ? `Multi / ${m.model_pool.length} models`
                    : `${m.provider_kind || "provider"} / ${m.gateway_route_id || "route"}`}
                </span>
              </div>
              <div className="cre-order-buttons">
                <button
                  type="button"
                  onClick={() => handleMove(idx, -1)}
                  disabled={idx === 0}
                  aria-label="Move up"
                >
                  Up
                </button>
                <button
                  type="button"
                  onClick={() => handleMove(idx, 1)}
                  disabled={idx === members.length - 1}
                  aria-label="Move down"
                >
                  Down
                </button>
                <button
                  type="button"
                  onClick={() => handleDelete(idx)}
                  data-testid={`delete-${idx}`}
                >
                  Delete
                </button>
              </div>
            </div>

            <div className="cre-member-primary">
              <input
                aria-label="Member name"
                value={m.name}
                onChange={(e) =>
                  updateMember(idx, { name: e.target.value })
                }
                placeholder="Member 1"
                className="cre-name-input"
              />
              <label>
                <span className="cre-field-label">Model mode</span>
                <select
                  aria-label={`Model mode for ${m.name || m.id}`}
                  value={m.model_mode}
                  disabled={m.coding_role === "pm"}
                  onChange={(event) => updateMember(idx, {
                    model_mode: event.target.value === "multi" ? "multi" : "single",
                  })}
                >
                  <option value="single">Single</option>
                  <option
                    value="multi"
                    disabled={modelAssignmentReady === false}
                  >
                    {modelAssignmentReady === false
                      ? "Multi (unavailable)"
                      : "Multi"}
                  </option>
                </select>
                {modelAssignmentReady === false && m.model_mode !== "multi" ? (
                  <small className="cre-field-hint">
                    Multi-model assignment isn&apos;t available in this build yet.
                  </small>
                ) : null}
              </label>
              {m.model_mode === "single" ? <>
              <label>
                <span className="cre-field-label">
                  Provider <InfoBubble label="Provider" text={TIP.provider} />
                </span>
                <select
                  value={m.provider_kind}
                  onChange={(e) => handleProviderChange(idx, e.target.value)}
                  data-testid={`provider-${idx}`}
                >
                  {providers.map((p) => {
                    const selectable = isProviderSelectable(p);
                    const needsSetup = isCliNeedsSetup(p);
                    let suffix = "";
                    if (needsSetup) {
                      suffix = " — Set up →";
                    } else if (!selectable) {
                      suffix = isCliProviderClass(p.provider_class)
                        ? " (CLI not installed)"
                        : p.provider_class === "custom"
                          ? " (none added)"
                          : " (no key)";
                    }
                    return (
                      <option
                        key={p.provider_class}
                        value={p.provider_class}
                        disabled={!selectable}
                      >
                        {p.display_name}
                        {suffix}
                      </option>
                    );
                  })}
                </select>
              </label>
              {providers.some((p) => isCliNeedsSetup(p)) ? (
                <button
                  type="button"
                  className="cre-cli-setup-link"
                  data-testid={`provider-setup-link-${idx}`}
                  onClick={navigateToSettings}
                >
                  Set up subscription CLIs →
                </button>
              ) : null}
              <label>
                <span className="cre-field-label">
                  Route <InfoBubble label="Route" text={TIP.route} />
                </span>
                <select
                  value={m.gateway_route_id}
                  onChange={(e) =>
                    updateMember(idx, { gateway_route_id: e.target.value })
                  }
                  data-testid={`route-${idx}`}
                >
                  <RouteOptions routes={routesByProvider[m.provider_kind] ?? []} />
                  {/* Allow free-form route_ids by including the current
                     value even if not in the catalog. */}
                  {m.gateway_route_id &&
                    !(routesByProvider[m.provider_kind] ?? []).some(
                      (r) => r.route_id === m.gateway_route_id,
                    ) && (
                      <option value={m.gateway_route_id}>
                        {m.gateway_route_id} (custom)
                      </option>
                  )}
                </select>
                {isRouteStale(m.gateway_route_id, m.provider_kind, routesByProvider) ? (
                  <span className="cre-route-stale" role="status">
                    ⚠ <strong>{m.gateway_route_id}</strong> is no longer offered by
                    this provider.{" "}
                    <button
                      type="button"
                      className="cre-route-stale-fix"
                      data-testid={`route-stale-fix-${idx}`}
                      onClick={() =>
                        updateMember(idx, {
                          gateway_route_id: fallbackRouteId(
                            m.provider_kind,
                            routesByProvider,
                          ),
                        })
                      }
                    >
                      Use {fallbackRouteId(m.provider_kind, routesByProvider)}
                    </button>
                  </span>
                ) : null}
              </label>
              </> : (
              <>
                {/* F135 — CLI picker: bulk-add a connected CLI's available
                   models to the pool; selected CLIs shown as removable chips. */}
                <div className="cre-cli-picker">
                  <span className="cre-field-label">
                    Add models from a CLI{" "}
                    <InfoBubble label="Add models from a CLI" text={TIP.cliPool} />
                  </span>
                  {connectedClis.length > 0 ? (
                    <div className="cre-cli-picker-row">
                      <select
                        aria-label={`Add models from a CLI for ${m.name || m.id}`}
                        value=""
                        data-testid={`cli-add-${idx}`}
                        onChange={(event) => {
                          const cls = event.target.value;
                          if (!cls) return;
                          const ids = availableRoutesForCli(cls).map((r) => r.route_id);
                          updateMember(idx, {
                            model_pool: [...new Set([...m.model_pool, ...ids])],
                          });
                        }}
                      >
                        <option value="">Add a CLI…</option>
                        {connectedClis.map((p) => {
                          const count = availableRoutesForCli(p.provider_class).length;
                          return (
                            <option
                              key={p.provider_class}
                              value={p.provider_class}
                              disabled={count === 0}
                            >
                              {p.display_name} ({count} model{count === 1 ? "" : "s"})
                            </option>
                          );
                        })}
                      </select>
                      {selectedClis.length > 0 ? (
                        <div className="cre-cli-chips">
                          {selectedClis.map((p) => (
                            <span key={p.provider_class} className="cre-cli-chip">
                              {p.display_name}
                              <button
                                type="button"
                                aria-label={`Remove ${p.display_name} models`}
                                data-testid={`cli-chip-remove-${idx}-${p.provider_class}`}
                                onClick={() =>
                                  updateMember(idx, {
                                    model_pool: m.model_pool.filter(
                                      (id) => routeProviderClass(id) !== p.provider_class,
                                    ),
                                  })
                                }
                              >
                                ×
                              </button>
                            </span>
                          ))}
                        </div>
                      ) : null}
                    </div>
                  ) : (
                    <small className="cre-field-hint">
                      No subscription CLI is connected.{" "}
                      <button
                        type="button"
                        className="cre-cli-setup-link"
                        onClick={navigateToSettings}
                      >
                        Set up subscription CLIs →
                      </button>
                    </small>
                  )}
                  {connectedClis.length > 0 && providers.some((p) => isCliNeedsSetup(p)) ? (
                    <button
                      type="button"
                      className="cre-cli-setup-link"
                      onClick={navigateToSettings}
                    >
                      Set up more CLIs →
                    </button>
                  ) : null}
                </div>

                <fieldset className="cre-model-pool">
                  <legend>Allowed models</legend>
                  <div className="cre-pool-summary">
                    <span>{m.model_pool.length} selected</span>
                    {m.model_pool.length > 0 ? (
                      <button
                        type="button"
                        className="cre-pool-clear"
                        data-testid={`pool-clear-${idx}`}
                        onClick={() => updateMember(idx, { model_pool: [] })}
                      >
                        Clear all
                      </button>
                    ) : null}
                  </div>
                  {poolGroups.map((group) => {
                    const rows = group.routes.map((route) => {
                      const availability = routeAvailability[route.route_id];
                      return {
                        route,
                        available: availability?.available === true,
                        saved: m.model_pool.includes(route.route_id),
                        reason: availability?.reason,
                      };
                    });
                    const shown = rows.filter((r) => r.available || r.saved);
                    const hidden = rows.filter((r) => !r.available && !r.saved);
                    const availableIds = rows
                      .filter((r) => r.available)
                      .map((r) => r.route.route_id);
                    const renderRow = (r: (typeof rows)[number]) => (
                      <label
                        key={r.route.route_id}
                        className="cre-model-pool-option"
                        data-available={r.available ? "yes" : "no"}
                      >
                        <input
                          type="checkbox"
                          checked={r.saved}
                          disabled={!r.available && !r.saved}
                          onChange={(event) => {
                            const next = new Set(m.model_pool);
                            if (event.target.checked) next.add(r.route.route_id);
                            else next.delete(r.route.route_id);
                            updateMember(idx, { model_pool: [...next] });
                          }}
                        />
                        <span className="cre-pool-model">
                          {r.route.providerClass} / {r.route.label}
                        </span>
                        {!r.available ? (
                          <small className="cre-pool-reason">
                            {r.saved ? `saved · ${reasonLabel(r.reason)}` : reasonLabel(r.reason)}
                          </small>
                        ) : null}
                      </label>
                    );
                    return (
                      <div key={group.providerClass} className="cre-pool-group">
                        <div className="cre-pool-group-head">
                          <span className="cre-pool-group-name">
                            {providerDisplayName(group.providerClass)}
                          </span>
                          {availableIds.length > 0 ? (
                            <span className="cre-pool-group-actions">
                              <button
                                type="button"
                                aria-label={`Select all ${providerDisplayName(group.providerClass)} models`}
                                onClick={() =>
                                  updateMember(idx, {
                                    model_pool: [
                                      ...new Set([...m.model_pool, ...availableIds]),
                                    ],
                                  })
                                }
                              >
                                Select all
                              </button>
                              <button
                                type="button"
                                aria-label={`Clear ${providerDisplayName(group.providerClass)} models`}
                                onClick={() => {
                                  const rm = new Set(availableIds);
                                  updateMember(idx, {
                                    model_pool: m.model_pool.filter((id) => !rm.has(id)),
                                  });
                                }}
                              >
                                Clear
                              </button>
                            </span>
                          ) : null}
                        </div>
                        {shown.map(renderRow)}
                        {hidden.length > 0 ? (
                          <details className="cre-pool-unavailable">
                            <summary>Show {hidden.length} unavailable</summary>
                            {hidden.map(renderRow)}
                          </details>
                        ) : null}
                      </div>
                    );
                  })}
                  {pooledRoutes.length === 0 ? (
                    <p className="cre-pool-empty">
                      No models are available yet. Enable a model family or connect
                      a CLI in{" "}
                      <button
                        type="button"
                        className="cre-cli-setup-link"
                        onClick={navigateToSettings}
                      >
                        Settings
                      </button>
                      .
                    </p>
                  ) : null}
                </fieldset>
              </>
              )}
            </div>

            <div className="cre-row-bottom">
              <label>
                <span className="cre-field-label">
                  Privacy{" "}
                  <InfoBubble
                    label="Member privacy"
                    text="Plain-language presets for how much context this member sees. Use Advanced when you need the raw context/transcript controls."
                  />
                </span>
                <select
                  value={privacyPreset}
                  onChange={(e) =>
                    updateMemberPrivacy(
                      idx,
                      e.target.value as MemberPrivacyPreset,
                    )
                  }
                  aria-label="Member privacy"
                >
                  {MEMBER_PRIVACY_PRESETS.map((o) => (
                    <option
                      key={o.value}
                      value={o.value}
                      disabled={o.value === "custom" && privacyPreset !== "custom"}
                    >
                      {o.label}
                    </option>
                  ))}
                </select>
              </label>
              {showCodingRoles ? (
                <label>
                  <span className="cre-field-label">
                    Coding role{" "}
                    <InfoBubble
                      label="Coding role"
                      text="This member's job when the room runs as a Coding Team: PM directs, dev writes code + tests, reviewer reviews, tester runs/validates."
                    />
                  </span>
                  <select
                    value={m.coding_role}
                    onChange={(e) =>
                      updateMember(idx, { coding_role: e.target.value })
                    }
                    aria-label="Coding role"
                  >
                    {CODING_ROLE_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </label>
              ) : null}
              <label>
                <span className="cre-field-label">
                  Reasoning/output budget{" "}
                  <InfoBubble label="Reasoning/output budget" text={TIP.maxOutput} />
                </span>
                <input
                  type="number"
                  min={1}
                  step={256}
                  value={m.max_output_tokens}
                  onChange={(e) =>
                    updateMember(idx, { max_output_tokens: e.target.value })
                  }
                  placeholder="2048 (default)"
                  data-testid={`max-output-${idx}`}
                  title="Per-turn output budget (Ollama num_predict). Reasoning models (Qwen, DeepSeek-R1) need a high value — they spend this on hidden thinking before the visible answer. Blank = engine default (2048)."
                />
              </label>
            </div>
            <details
              className="cre-member-advanced"
              {...(privacyPreset === "custom" ? { open: true } : {})}
            >
              <summary>Advanced member settings</summary>
              <div className="cre-row-bottom">
                <label>
                  <span className="cre-field-label">Member id</span>
                  <input
                    aria-label="Member id"
                    value={m.id}
                    onChange={(e) =>
                      updateMember(idx, { id: e.target.value })
                    }
                    placeholder="m-1"
                    className="cre-id-input"
                  />
                </label>
                <label>
                  <span className="cre-field-label">
                    Context access{" "}
                    <InfoBubble label="Context access" text={TIP.contextAccess} />
                  </span>
                  <select
                    value={m.context_access}
                    onChange={(e) =>
                      updateMember(idx, { context_access: e.target.value })
                    }
                  >
                    {CONTEXT_ACCESS_OPTIONS.map((o) => (
                      <option key={o} value={o}>
                        {o}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  <span className="cre-field-label">
                    Transcript access{" "}
                    <InfoBubble
                      label="Transcript access"
                      text={TIP.transcriptAccess}
                    />
                  </span>
                  <select
                    value={m.transcript_access}
                    onChange={(e) =>
                      updateMember(idx, { transcript_access: e.target.value })
                    }
                  >
                    {TRANSCRIPT_ACCESS_OPTIONS.map((o) => (
                      <option key={o} value={o}>
                        {o}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            </details>
            <details
              className="cre-system-prompt"
              {...(m.system_prompt.trim().length > 0 ? { open: true } : {})}
            >
              <summary className="cre-system-prompt-label">
                <span className="cre-field-label">
                  System prompt — persona &amp; behavior{" "}
                  <InfoBubble label="System prompt" text={TIP.systemPrompt} />
                </span>
                <span className="cre-prompt-state">
                  {m.system_prompt.trim() ? "Custom" : "Default"}
                </span>
              </summary>
              <div className="cre-system-prompt-body">
                <button
                  type="button"
                  className="cre-prompt-example-btn"
                  data-testid={`insert-example-${idx}`}
                  onClick={() =>
                    updateMember(idx, {
                      system_prompt:
                        "You are a stubborn, irritable skeptic. You start out " +
                        "disagreeing and are hard to convince. Only concede a " +
                        "point when another member gives a genuinely airtight, " +
                        "evidence-backed argument — and say so explicitly when " +
                        "you do. Stay terse and a little grumpy. Never invent " +
                        "facts to win.",
                    })
                  }
                  title="Fill with an example 'angry skeptic' persona you can edit"
                >
                  Insert example
                </button>
                <textarea
                  value={m.system_prompt}
                  onChange={(e) =>
                    updateMember(idx, { system_prompt: e.target.value })
                  }
                  rows={3}
                  placeholder="Blank uses a neutral default. Example: 'You are deeply skeptical and hard to convince — make the others earn your agreement.'"
                  data-testid={`system-prompt-${idx}`}
                />
              </div>
            </details>
            <div className="cre-steelman">
              <label className="cre-steelman-toggle">
                <input
                  type="checkbox"
                  checked={m.steelman}
                  data-testid={`steelman-${idx}`}
                  onChange={(e) =>
                    updateMember(idx, { steelman: e.target.checked })
                  }
                />
                <span className="cre-field-label">
                  Steelman{" "}
                  <InfoBubble
                    label="Steelman"
                    text={
                      "This member argues FOR the topic below as forcefully as " +
                      "possible and may construct supporting evidence and " +
                      "citations even when the fetched sources don't back it. " +
                      "Its claims are labeled UNVERIFIED and quarantined — never " +
                      "counted as source-supported, never added to your corpus, " +
                      "never used by the council leader as real evidence."
                    }
                  />
                </span>
              </label>
              <input
                type="text"
                className="cre-steelman-topic"
                value={m.steelman_topic}
                disabled={!m.steelman}
                data-testid={`steelman-topic-${idx}`}
                placeholder="What to steelman, e.g. Existence of Santa"
                onChange={(e) =>
                  updateMember(idx, { steelman_topic: e.target.value })
                }
              />
            </div>
          </li>
          );
        })}
      </ol>

      <section className="cre-topology" aria-label="Topology and rounds">
        <h4>Conversation flow</h4>
        <p className="cre-section-hint">
          How members take turns. Round robin and consensus deliberation are
          live today; rounds and turn caps are enforced for every run.
        </p>
        <div className="cre-row-bottom">
          <label>
            <span className="cre-field-label">
              Topology kind{" "}
              <InfoBubble label="Topology kind" text={TIP.topologyKind} />
            </span>
            <select
              value={topology.kind}
              onChange={(e) => {
                setTopology((cur) => ({ ...cur, kind: e.target.value }));
                setDirty(true);
              }}
            >
              <option value="round_robin">Round robin (each member takes turns)</option>
              <option value="consensus_deliberation">
                Deliberate until consensus (round 1 blind, then refine)
              </option>
              <option value="credibility">
                Credibility (research → claims → peer credidation → verified report)
              </option>
            </select>
          </label>
          <label>
            <span className="cre-field-label">
              Max rounds <InfoBubble label="Max rounds" text={TIP.maxRounds} />
            </span>
            <input
              type="number"
              min={1}
              max={20}
              value={topology.max_rounds}
              onChange={(e) => {
                setTopology((cur) => ({
                  ...cur,
                  max_rounds: Math.max(1, Number(e.target.value) || 1),
                }));
                setDirty(true);
              }}
            />
          </label>
          <label>
            <span className="cre-field-label">
              Max messages per member{" "}
              <InfoBubble label="Max messages per member" text={TIP.maxMessages} />
            </span>
            <input
              type="number"
              min={1}
              max={20}
              value={topology.max_messages_per_member}
              onChange={(e) => {
                setTopology((cur) => ({
                  ...cur,
                  max_messages_per_member: Math.max(
                    1, Number(e.target.value) || 1,
                  ),
                }));
                setDirty(true);
              }}
            />
          </label>
          <label>
            <span className="cre-field-label">
              Max total turns{" "}
              <InfoBubble label="Max total turns" text={TIP.maxTotalTurns} />
            </span>
            <input
              type="number"
              min={1}
              max={200}
              value={topology.max_total_turns}
              onChange={(e) => {
                setTopology((cur) => ({
                  ...cur,
                  max_total_turns: Math.max(1, Number(e.target.value) || 1),
                }));
                setDirty(true);
              }}
            />
          </label>
          {topology.kind === "consensus_deliberation" && (
            <label>
              <span className="cre-field-label">
                Consensus threshold{" "}
                <InfoBubble
                  label="Consensus threshold"
                  text={TIP.consensusThreshold}
                />
              </span>
              <input
                type="number"
                min={0}
                max={20}
                value={topology.consensus_threshold}
                onChange={(e) => {
                  setTopology((cur) => ({
                    ...cur,
                    consensus_threshold: Math.max(0, Number(e.target.value) || 0),
                  }));
                  setDirty(true);
                }}
                placeholder="0 = all enabled"
              />
            </label>
          )}
        </div>
        {topology.kind === "consensus_deliberation" && (
          <p className="cre-section-hint">
            Consensus topology: round 1 each member answers blind; rounds 2+
            they see prior round &amp; refine. Stops when ≥ <strong>threshold</strong>{" "}
            members signal no-changed-views in their digest_v1 ("delta": null
            or "no_changed_views"). 0 means all enabled members must agree.
            Works best with deliberation_dialect = digest_v1 below.
          </p>
        )}
      </section>

      <section className="cre-finalization" aria-label="Finalization">
        <h4>Final answer</h4>
        <p className="cre-section-hint">
          How the run chooses or records the answer-of-record. Pick a finalizer
          only when the selected mode uses one.
        </p>
        <div className="cre-row-bottom">
          <label>
            <span className="cre-field-label">
              Mode <InfoBubble label="Finalization mode" text={TIP.finalizationMode} />
            </span>
            <select
              value={finalization.mode}
              onChange={(e) => {
                setFinalization((cur) => ({ ...cur, mode: e.target.value }));
                setDirty(true);
              }}
            >
              <option value="transcript_only">
                Transcript only (last message is the answer)
              </option>
              <option value="single_finalizer">
                Single finalizer (named member writes the final answer)
              </option>
              <option value="consensus_report">Consensus report</option>
              <option value="summary">
                Summary (abstractive synthesis — preserves disagreement)
              </option>
              <option value="credibility_report">Credibility report (verified citations)</option>
              {/* F111: these modes are recorded but have no executed path yet —
                  shown disabled so the editor can't promise behavior the engine
                  won't deliver (it would silently run as transcript_only). */}
              <option value="vote_summary" disabled>
                Vote summary (majority) — not implemented yet
              </option>
              <option value="judged_final_answer" disabled>
                Judge verdict — not implemented yet
              </option>
            </select>
          </label>
          <label>
            <span className="cre-field-label">
              {finalization.mode === "consensus_report"
                ? "Consensus writer"
                : finalization.mode === "summary"
                  ? "Summary writer"
                  : "Finalizer member"}{" "}
              <InfoBubble
                label={
                  finalization.mode === "consensus_report"
                    ? "Consensus writer"
                    : finalization.mode === "summary"
                      ? "Summary writer"
                      : "Finalizer member"
                }
                text={
                  finalization.mode === "consensus_report"
                    ? "Which member writes the synthesized Consensus answer once the council converges. Leave blank to auto-pick (the steward leader, else the last speaker). Pick a strong model here (e.g. Claude) to always author the consensus."
                    : finalization.mode === "summary"
                      ? "Which member writes the abstractive summary. Leave blank to auto-pick (the steward leader, else the last speaker). Pick a strong model here to author the summary."
                      : TIP.finalizerMember
                }
              />
            </span>
            <select
              value={finalization.finalizer_member_id}
              disabled={
                finalization.mode !== "single_finalizer" &&
                finalization.mode !== "consensus_report" &&
                finalization.mode !== "summary"
              }
              onChange={(e) => {
                setFinalization((cur) => ({
                  ...cur,
                  finalizer_member_id: e.target.value,
                }));
                setDirty(true);
              }}
            >
              <option value="">— pick a member —</option>
              {members.filter((m) => m.enabled).map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name || m.id}
                </option>
              ))}
            </select>
          </label>
        </div>
      </section>

      {(() => {
        const jp = (room?.judge_policy as Record<string, unknown>) ?? {};
        const judgeEnabled = jp.enabled === true;
        const judgeMemberId = String(jp.judge_member_id ?? "");
        const setJudge = (patch: Record<string, unknown>) => {
          setRoom((r) => ({
            ...(r ?? {}),
            judge_policy: {
              ...((r?.judge_policy as Record<string, unknown>) ?? {}),
              ...patch,
            },
          }));
          setDirty(true);
        };
        return (
          <section className="cre-finalization" aria-label="Neutral judge">
            <h4>Neutral judge</h4>
            <p className="cre-section-hint">
              A judge watches each round, holds no opinion of its own, and ends
              the run early when the members reach a verdict (it can also break a
              tie at the round limit). It never takes a deliberation turn. Works
              with any topology.
            </p>
            <div className="cre-row-bottom">
              <label className="cre-checkbox">
                <input
                  type="checkbox"
                  checked={judgeEnabled}
                  onChange={(e) => setJudge({ enabled: e.target.checked })}
                />
                <span>Enable neutral judge</span>
              </label>
              <label>
                <span className="cre-field-label">
                  Judge member{" "}
                  <InfoBubble label="Judge member" text={TIP.judgeMember} />
                </span>
                <select
                  value={judgeMemberId}
                  disabled={!judgeEnabled}
                  onChange={(e) =>
                    setJudge({ judge_member_id: e.target.value || null })
                  }
                >
                  <option value="">— pick a member —</option>
                  {members
                    .filter((m) => m.enabled)
                    .map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.name || m.id}
                      </option>
                    ))}
                </select>
              </label>
            </div>
          </section>
        );
      })()}

      <p className="cre-advanced-divider">Advanced — expand only what you need</p>

      <details className="cre-group cre-budget">
        <summary className="cre-group-summary">
          <span className="cre-group-title">Budget &amp; limits</span>
          <span className="cre-group-badge">
            floor {budgetFloor.maxTotalModelCallsFloor} calls
          </span>
          <span className="cre-group-sub">
            cap {budget.max_total_model_calls || "auto"} · remote floor{" "}
            {budgetFloor.maxRemoteCallsPerRunFloor}
          </span>
        </summary>
        <p className="cre-section-hint">
          <strong>Max total model calls</strong> is enforced: it caps how many
          times the council can hit a model across the whole run (must be at
          least one per enabled member); the run stops cleanly when it's reached.
          Room-wide per-turn token caps are preserved for existing rooms but are
          hidden here until the runner enforces them. Use each member's{" "}
          <strong>Reasoning/output budget</strong> field for the live per-turn
          budget today.
        </p>
        <div className="cre-row-bottom">
          <label>
            <span className="cre-field-label">
              Max total model calls{" "}
              <InfoBubble label="Max total model calls" text={TIP.maxModelCalls} />
            </span>
            <input
              type="number"
              min={1}
              max={1000}
              value={budget.max_total_model_calls}
              onChange={(e) => {
                setBudget((cur) => ({
                  ...cur,
                  max_total_model_calls: Math.max(
                    1, Number(e.target.value) || 1,
                  ),
                }));
                setDirty(true);
              }}
            />
          </label>
        </div>
      </details>

      <details className="cre-group cre-context-efficiency">
        <summary className="cre-group-summary">
          <span className="cre-group-title">Context efficiency</span>
          <span className={`cre-group-badge ${contextSummary === "off" ? "" : "is-active"}`}>
            {contextSummary}
          </span>
          <span className="cre-group-sub">
            token-saving for member-to-member context
          </span>
        </summary>
        <p className="cre-section-hint">
          Optional token-saving settings (F036). All default off and apply
          only to the back-and-forth between members — the final answer is
          never abbreviated. <strong>Telegraphic</strong> style asks members
          to be terse; <strong>digest_v1</strong> dialect asks them to emit a
          structured JSON position (needed for consensus stop);{" "}
          <strong>citation references</strong> replaces repeated source text
          with short <code>[c:1]</code> markers; <strong>compaction</strong>{" "}
          summarises old rounds while keeping the most recent ones verbatim;{" "}
          <strong>prompt cache hints</strong> mark the stable prefix so
          supported providers can cache it. See the Council room settings doc
          for details.
        </p>
        <label className="cre-checkbox cre-primary-toggle">
          <input
            type="checkbox"
            checked={saveTokensOnLongRuns}
            onChange={(e) => updateTokenSaving(e.target.checked)}
            data-testid="context-save-tokens"
          />
          Save tokens on long runs{" "}
          <InfoBubble
            label="Save tokens on long runs"
            text="Turns on the recommended context-saving bundle: terse member deliberation, structured digest positions, citation references, transcript compaction, and provider cache hints."
          />
        </label>
        <div className="cre-row-bottom">
          <label>
            <span className="cre-field-label">
              Deliberation style{" "}
              <InfoBubble label="Deliberation style" text={TIP.deliberationStyle} />
            </span>
            <select
              value={efficiency.deliberation_style}
              onChange={(e) => {
                setEfficiency((cur) => ({
                  ...cur,
                  deliberation_style: e.target.value as "natural" | "telegraphic",
                }));
                setDirty(true);
              }}
            >
              <option value="natural">Natural</option>
              <option value="telegraphic">Telegraphic</option>
            </select>
          </label>
          <label>
            <span className="cre-field-label">
              Intermediate output cap{" "}
              <InfoBubble label="Intermediate output cap" text={TIP.intermediateCap} />
            </span>
            <input
              type="number"
              min="1"
              value={efficiency.intermediate_max_output_tokens}
              onChange={(e) => {
                setEfficiency((cur) => ({
                  ...cur,
                  intermediate_max_output_tokens: e.target.value,
                }));
                setDirty(true);
              }}
              placeholder="member default"
            />
          </label>
          <label>
            <span className="cre-field-label">
              Deliberation dialect{" "}
              <InfoBubble label="Deliberation dialect" text={TIP.deliberationDialect} />
            </span>
            <select
              value={efficiency.deliberation_dialect}
              onChange={(e) => {
                setEfficiency((cur) => ({
                  ...cur,
                  deliberation_dialect: e.target.value as "prose" | "digest_v1",
                }));
                setDirty(true);
              }}
            >
              <option value="prose">Prose</option>
              <option value="digest_v1">digest_v1</option>
            </select>
          </label>
          <label className="cre-checkbox">
            <input
              type="checkbox"
              checked={efficiency.citation_references}
              onChange={(e) => {
                setEfficiency((cur) => ({
                  ...cur,
                  citation_references: e.target.checked,
                }));
                setDirty(true);
              }}
            />
            Citation references{" "}
            <InfoBubble label="Citation references" text={TIP.citationReferences} />
          </label>
          <label className="cre-checkbox">
            <input
              type="checkbox"
              checked={efficiency.compaction_enabled}
              onChange={(e) => {
                setEfficiency((cur) => ({
                  ...cur,
                  compaction_enabled: e.target.checked,
                }));
                setDirty(true);
              }}
            />
            Transcript compaction{" "}
            <InfoBubble label="Transcript compaction" text={TIP.compaction} />
          </label>
          <label>
            <span className="cre-field-label">
              Full rounds window{" "}
              <InfoBubble label="Full rounds window" text={TIP.fullRoundsWindow} />
            </span>
            <input
              type="number"
              min="1"
              value={efficiency.compaction_full_rounds_window}
              onChange={(e) => {
                setEfficiency((cur) => ({
                  ...cur,
                  compaction_full_rounds_window: e.target.value,
                }));
                setDirty(true);
              }}
            />
          </label>
          <label>
            <span className="cre-field-label">
              Segment size <InfoBubble label="Segment size" text={TIP.segmentSize} />
            </span>
            <input
              type="number"
              min="1"
              value={efficiency.compaction_segment_size_rounds}
              onChange={(e) => {
                setEfficiency((cur) => ({
                  ...cur,
                  compaction_segment_size_rounds: e.target.value,
                }));
                setDirty(true);
              }}
            />
          </label>
          <label>
            <span className="cre-field-label">
              When summary unavailable{" "}
              <InfoBubble
                label="When summary unavailable"
                text={TIP.onSummaryUnavailable}
              />
            </span>
            <select
              value={efficiency.on_summary_unavailable}
              onChange={(e) => {
                setEfficiency((cur) => ({
                  ...cur,
                  on_summary_unavailable: e.target.value as
                    | "structural"
                    | "verbatim",
                }));
                setDirty(true);
              }}
            >
              <option value="structural">
                Structural (drop text, keep metadata)
              </option>
              <option value="verbatim">Verbatim (keep original rounds)</option>
            </select>
          </label>
          <label className="cre-checkbox">
            <input
              type="checkbox"
              checked={efficiency.prompt_cache_hints}
              onChange={(e) => {
                setEfficiency((cur) => ({
                  ...cur,
                  prompt_cache_hints: e.target.checked,
                }));
                setDirty(true);
              }}
            />
            Prompt cache hints{" "}
            <InfoBubble label="Prompt cache hints" text={TIP.promptCacheHints} />
          </label>
        </div>
      </details>

      <details className="cre-group cre-steward">
        <summary className="cre-group-summary">
          <span className="cre-group-title">Council Steward</span>
          <span className={`cre-group-badge ${steward.enabled ? "is-active" : ""}`}>
            {stewardStatusSummary}
          </span>
          <span className="cre-group-sub">
            compact packet maintenance for older transcript context
          </span>
        </summary>
        <p className="cre-section-hint">
          <strong>Council Steward</strong> keeps the full conversation visible
          to the user while compacting older member-to-member context into an
          inspectable packet. Default is off. When enabled, the packet replaces
          older transcript messages in model context and cites the source
          events it summarizes.
        </p>
        <div className="cre-row-bottom">
          <label className="cre-checkbox">
            <input
              type="checkbox"
              checked={steward.enabled}
              onChange={(e) => updateSteward({ enabled: e.target.checked })}
              data-testid="steward-enabled"
            />
            Enable steward{" "}
            <InfoBubble label="Council Steward" text={TIP.steward} />
          </label>
          <label>
            <span className="cre-field-label">
              Assignment{" "}
              <InfoBubble label="Steward assignment" text={TIP.stewardAssignment} />
            </span>
            <select
              value={steward.assignment_mode}
              onChange={(e) =>
                updateSteward({ assignment_mode: e.target.value })
              }
              data-testid="steward-assignment"
            >
              <option value="external">External steward model</option>
              <option value="member">Existing council member</option>
            </select>
          </label>
          {steward.assignment_mode === "member" ? (
            <label>
              <span className="cre-field-label">
                Steward member{" "}
                <InfoBubble label="Steward assignment" text={TIP.stewardAssignment} />
              </span>
              <select
                value={steward.assignment_member_id}
                onChange={(e) =>
                  updateSteward({ assignment_member_id: e.target.value })
                }
                data-testid="steward-member"
              >
                <option value="">— pick a member —</option>
                {members.filter((m) => m.enabled).map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name || m.id}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <p className="cre-field-note cre-wide-note">
              External model-backed Steward routes are not active yet. Saved
              external assignment values are preserved, but packet maintenance
              runs through the deterministic Steward path today.
            </p>
          )}
          <label>
            <span className="cre-field-label">
              Packet mode{" "}
              <InfoBubble label="Packet mode" text={TIP.stewardPacketMode} />
            </span>
            <select
              value={steward.packet_mode}
              onChange={(e) => updateSteward({ packet_mode: e.target.value })}
              data-testid="steward-packet-mode"
            >
              <option value="hybrid">Hybrid</option>
              <option value="structured">Structured</option>
              <option value="narrative">Narrative</option>
            </select>
          </label>
          <label>
            <span className="cre-field-label">
              Cadence <InfoBubble label="Cadence" text={TIP.stewardCadence} />
            </span>
            <select
              value={steward.cadence}
              onChange={(e) => updateSteward({ cadence: e.target.value })}
              data-testid="steward-cadence"
            >
              <option value="after_each_round">After each round</option>
              <option value="on_demand">On demand</option>
            </select>
          </label>
          <label>
            <span className="cre-field-label">
              Recent full messages{" "}
              <InfoBubble label="Recent full messages" text={TIP.stewardRecent} />
            </span>
            <input
              type="number"
              min={0}
              max={20}
              value={steward.recent_full_messages}
              onChange={(e) =>
                updateSteward({
                  recent_full_messages: Math.max(0, Number(e.target.value) || 0),
                })
              }
              data-testid="steward-recent"
            />
          </label>
          <label>
            <span className="cre-field-label">
              Max packet tokens{" "}
              <InfoBubble label="Max packet tokens" text={TIP.stewardPacketTokens} />
            </span>
            <input
              type="number"
              min={128}
              max={32768}
              step={64}
              value={steward.max_packet_tokens}
              onChange={(e) =>
                updateSteward({
                  max_packet_tokens: Math.max(128, Number(e.target.value) || 128),
                })
              }
              data-testid="steward-max-packet"
            />
          </label>
          <label>
            <span className="cre-field-label">
              Max steward calls/run{" "}
              <InfoBubble label="Max steward calls/run" text={TIP.stewardCalls} />
            </span>
            <input
              type="number"
              min={0}
              max={100}
              value={budget.max_steward_calls_per_run}
              onChange={(e) => {
                setBudget((cur) => ({
                  ...cur,
                  max_steward_calls_per_run: Math.max(
                    0,
                    Number(e.target.value) || 0,
                  ),
                }));
                setDirty(true);
              }}
              data-testid="steward-max-calls"
            />
          </label>
          <label>
            <span className="cre-field-label">
              Fallback{" "}
              <InfoBubble label="Steward fallback" text={TIP.stewardFallback} />
            </span>
            <select
              value={steward.fallback_on_failure}
              onChange={(e) =>
                updateSteward({ fallback_on_failure: e.target.value })
              }
              data-testid="steward-fallback"
            >
              <option value="full_transcript">Full transcript</option>
              <option value="stop">Stop run</option>
            </select>
          </label>
          <label className="cre-checkbox">
            <input
              type="checkbox"
              checked={steward.allow_raw_expansion}
              onChange={(e) =>
                updateSteward({ allow_raw_expansion: e.target.checked })
              }
            />
            Allow raw expansion
          </label>
          <label className="cre-checkbox">
            <input
              type="checkbox"
              checked={steward.show_packet_audit_to_user}
              onChange={(e) =>
                updateSteward({ show_packet_audit_to_user: e.target.checked })
              }
            />
            Show packet audit
          </label>
          <label className="cre-checkbox">
            <input
              type="checkbox"
              checked={steward.remote_steward_allowed}
              onChange={(e) =>
                updateSteward({ remote_steward_allowed: e.target.checked })
              }
              data-testid="steward-remote-allowed"
            />
            Allow remote steward
          </label>
        </div>
        {steward.enabled &&
          steward.assignment_mode === "external" &&
          isRemoteProviderKind(steward.assignment_provider_kind) &&
          !steward.remote_steward_allowed && (
            <p className="cre-section-hint" role="alert">
              Remote steward route selected. Enable remote steward and reserve
              remote budget before saving this as a ready room.
            </p>
          )}
        {steward.enabled && steward.assignment_mode === "member" && (
          <p className="cre-section-hint">
            Existing-member steward mode reuses a council member for packet
            maintenance. Use it when cost matters more than strict separation
            between stewardship and ordinary member opinions.
          </p>
        )}
      </details>

      <details className="cre-group cre-tools">
        <summary className="cre-group-summary">
          <span className="cre-group-title">Tools</span>
          <span className={`cre-group-badge ${toolStatusSummary === "none granted" ? "" : "is-active"}`}>
            {toolStatusSummary}
          </span>
          <span className="cre-group-sub">
            internet + code tools; each call still asks first
          </span>
        </summary>
        <p className="cre-section-hint">
          Grant capabilities to the council. Everything here is default-off and
          fail-closed; the first use of a granted tool asks for your approval
          (unless you turn that off). Tool output is treated as untrusted data,
          never instructions.
        </p>
        <div className="cre-row-bottom">
          <label className="cre-checkbox">
            <input
              type="checkbox"
              checked={tools.web_fetch_enabled}
              onChange={(e) => updateTools({ web_fetch_enabled: e.target.checked })}
              data-testid="tool-web-fetch"
            />
            Web fetch (SSRF-guarded)
          </label>
          <label>
            <span className="cre-field-label">Allowed domains (comma-sep, blank = any public)</span>
            <input
              type="text"
              value={tools.web_fetch_allowed_domains}
              disabled={!tools.web_fetch_enabled}
              onChange={(e) => updateTools({ web_fetch_allowed_domains: e.target.value })}
              data-testid="tool-web-fetch-domains"
              placeholder="example.com, docs.python.org"
            />
          </label>
        </div>
        <div className="cre-row-bottom">
          <label className="cre-checkbox">
            <input
              type="checkbox"
              checked={tools.web_search_enabled}
              onChange={(e) => updateTools({ web_search_enabled: e.target.checked })}
              data-testid="tool-web-search"
            />
            Web search (SearXNG)
          </label>
          <p className="cre-field-note cre-wide-note">
            SearXNG endpoint is configured in Settings → Tools. This room only
            grants web-search permission.
          </p>
        </div>
        <div className="cre-row-bottom">
          <label className="cre-checkbox">
            <input
              type="checkbox"
              checked={tools.code_read_enabled}
              onChange={(e) => updateTools({ code_read_enabled: e.target.checked })}
              data-testid="tool-code-read"
            />
            Code read
          </label>
          <label>
            <span className="cre-field-label">Workspace path (granted folder)</span>
            <input
              type="text"
              value={tools.code_read_workspace_path}
              disabled={!tools.code_read_enabled && !tools.code_write_enabled && !tools.code_exec_enabled}
              onChange={(e) => updateTools({ code_read_workspace_path: e.target.value })}
              data-testid="tool-workspace-path"
              placeholder="/Users/you/project"
            />
          </label>
        </div>
        <div className="cre-row-bottom">
          <label className="cre-checkbox">
            <input
              type="checkbox"
              checked={tools.code_write_enabled}
              onChange={(e) => updateTools({ code_write_enabled: e.target.checked })}
              data-testid="tool-code-write"
            />
            Code write
          </label>
          <label>
            <span className="cre-field-label">Write mode</span>
            <select
              value={tools.code_write_mode}
              disabled={!tools.code_write_enabled}
              onChange={(e) => updateTools({ code_write_mode: e.target.value })}
              data-testid="tool-code-write-mode"
            >
              <option value="propose_only">propose_only (diff, never writes)</option>
              <option value="auto_apply">auto_apply (isolated git copy)</option>
            </select>
          </label>
        </div>
        <div className="cre-row-bottom">
          <label className="cre-checkbox">
            <input
              type="checkbox"
              checked={tools.code_exec_enabled}
              onChange={(e) => updateTools({ code_exec_enabled: e.target.checked })}
              data-testid="tool-code-exec"
            />
            Code exec (sandboxed)
          </label>
          <label className="cre-field">
            <span className="cre-field-label">Sandbox</span>
            <select
              value={tools.code_exec_sandbox}
              disabled={!tools.code_exec_enabled}
              onChange={(e) => updateTools({ code_exec_sandbox: e.target.value })}
              data-testid="tool-code-exec-sandbox"
            >
              <option value="none">none (constrained subprocess)</option>
              <option value="seatbelt">seatbelt (macOS — no network, writes confined)</option>
              <option value="bwrap">bubblewrap (Linux — no network, writes confined)</option>
              <option value="docker">docker (container — no network)</option>
            </select>
          </label>
          <label className="cre-checkbox">
            <input
              type="checkbox"
              checked={tools.code_exec_network}
              disabled={!tools.code_exec_enabled || tools.code_exec_sandbox === "none"}
              onChange={(e) => updateTools({ code_exec_network: e.target.checked })}
              data-testid="tool-code-exec-network"
            />
            Allow network (requires a sandbox; fails closed otherwise)
          </label>
        </div>
        <div className="cre-row-bottom">
          <label className="cre-checkbox">
            <input
              type="checkbox"
              checked={tools.require_first_use_consent}
              onChange={(e) => updateTools({ require_first_use_consent: e.target.checked })}
              data-testid="tool-consent"
            />
            Ask before first use of each tool
          </label>
        </div>
      </details>

      <footer className="cre-actions">
        <button
          onClick={handleSave}
          disabled={busy || !dirty}
          type="button"
        >
          {busy ? "Saving…" : "Save"}
        </button>
        <button onClick={handleCancel} disabled={busy} type="button">
          {dirty ? "Cancel" : "Close"}
        </button>
      </footer>
      {error && (
        <div className="cre-error" role="alert">
          {error}
        </div>
      )}
      <CouncilQuickStartGuide
        open={quickStartOpen}
        onClose={() => setQuickStartOpen(false)}
      />
    </section>
  );
}
