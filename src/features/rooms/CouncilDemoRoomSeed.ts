// F031-DEMO-CORPUS — Council demo room seed.
//
// Phase 5 originally posted a 2-fake-member round-robin room with
// `corpus_ids=[]`. This slice extends the flow:
//
// 1. Ensure the F007 welcome corpus is on disk via `ensureDemoCorpus()`
//    (POST /welcome/install — Task 2 decision: reuse the existing route).
// 2. Post the seeded room with `corpus_ids=["welcome"]` and a stable
//    `metadata.demo_marker` so `CouncilShell` can recognise the demo
//    room without name-matching.
// 3. Fail-loudly on corpus-seed failure (invariant 4) — no silent
//    fallback to empty-corpus seed unless the caller opts in via
//    `{ skipCorpus: true }` (the "Advanced" disclosure path).
//
// The `metadata` field rides through the `CouncilRoom` schema's `_extras`
// passthrough (see `python/errorta_council/schema.py:_split_unknown`),
// so unknown keys round-trip without a backend schema change.
import { postJSON } from "../../lib/api";
import { ensureDemoCorpus } from "../../lib/api/council";

export const DEMO_ROOM_MARKER = "council-demo-room" as const;

// DEMO_PROMPT — pinned demo prompt for the welcome corpus.
//
// Constraints (locked by Task 5 sanity test):
// - References content present in `docs/welcome-corpus-src/` (the
//   pinned welcome-corpus source).
// - At least one full sentence.
// - Does not match `/^hello/i`.
//
// Topic overlap: AIAR + Apache-2.0 license + the local-only data
// promise all appear in `03-built-on-aiar.md` AND `04-faq.md` —
// retrieval should land at least two non-prompt sources once
// F031-RETRIEVAL wires the seam to a real pipeline.
export const DEMO_PROMPT =
  "Errorta is built on AIAR — which open-source license is AIAR distributed under, and does Errorta send my prompts or documents anywhere?";

const NOW = "2026-06-11T00:00:00Z";

export class DemoSeedError extends Error {
  readonly structuredReason: string;
  constructor(reason: string) {
    super(`demo_seed_failed: ${reason}`);
    this.name = "DemoSeedError";
    this.structuredReason = reason;
  }
}

// QA P1 #3 lock: the demo room exists to make the byte-isolation marquee
// visible. Both members at `prompt_only` would silently never trigger
// retrieval, so the inspection drawer's source_counts.retrieved_snippet
// stays zero and the compare strip has nothing meaningful to show. Pair
// one corpus-bearing member with one redacted member instead.
const DEMO_MEMBER_CONTEXT_ACCESS: Record<number, string> = {
  1: "full_context",
  2: "redacted_summary",
};

function fakeMember(idx: number) {
  return {
    id: `m-${idx}`,
    name: `Member ${idx}`,
    role: "answerer",
    enabled: true,
    gateway_route_id: "fake.local.deterministic",
    provider_kind: "local",
    provider_display: "Fake",
    model_display: "deterministic",
    catalog_version: "2026-06-11",
    context_access: DEMO_MEMBER_CONTEXT_ACCESS[idx] ?? "prompt_only",
    transcript_access: "own_messages",
    turn_limits: {
      max_messages: 1,
      max_input_tokens: 1024,
      max_output_tokens: 256,
      max_context_tokens: 1024,
    },
    generation: { temperature: 0.0, top_p: null, seed: null },
    system_prompt: "F031-DEMO-CORPUS demo seed.",
    metadata: {},
  };
}

interface RoomPayload {
  format_version: number;
  id: string;
  name: string;
  description: string;
  preset_id: null;
  status_hint: string;
  members: ReturnType<typeof fakeMember>[];
  topology: Record<string, unknown>;
  context_policy: Record<string, unknown>;
  budget_policy: Record<string, unknown>;
  finalization_policy: Record<string, unknown>;
  ui: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  last_validated_at: null;
  revision: number;
  // Additive — survives backend `_extras` passthrough.
  corpus_ids?: string[];
  metadata?: Record<string, unknown>;
}

function buildBaseRoom(): RoomPayload {
  return {
    format_version: 1,
    id: `demo-${Date.now()}`,
    name: "Demo Room",
    description: "Auto-seeded by the empty-state demo affordance.",
    preset_id: null,
    status_hint: "draft",
    members: [fakeMember(1), fakeMember(2)],
    topology: {
      kind: "round_robin",
      max_rounds: 1,
      max_messages_per_member: 1,
      max_total_turns: 2,
      speaker_order: ["m-1", "m-2"],
      stop_condition: null,
    },
    context_policy: {
      // Members request `full_context` / `redacted_summary` per the
      // QA P1 #3 lock; the room ceiling must permit that or the policy
      // pipeline clamps everything back down and we lose the marquee.
      default_context_access: "prompt_only",
      default_transcript_access: "own_messages",
      allow_full_context: true,
      require_confirmation_for_remote_context: true,
      require_confirmation_for_full_context: false,
    },
    budget_policy: {
      max_rounds: 1,
      max_messages_per_member: 1,
      max_total_model_calls: 2,
      max_remote_calls_per_run: 0,
      max_remote_calls_per_day: null,
      max_input_tokens_per_turn: 1024,
      max_output_tokens_per_turn: 256,
      max_context_tokens_per_member: 1024,
      max_estimated_usd_per_run: 0.0,
      max_estimated_usd_per_month: null,
    },
    finalization_policy: {
      mode: "transcript_only",
      finalizer_member_id: null,
      judge_member_ids: [],
      require_judge_verdict: false,
      allow_minority_report: true,
      allow_grounding_write: false,
      grounding_requires_user_accept: true,
    },
    ui: {},
    created_at: NOW,
    updated_at: NOW,
    last_validated_at: null,
    revision: 1,
  };
}

export interface SeedDemoRoomOptions {
  /** Advanced override — skip the corpus-ensure step and post an empty-corpus room. */
  skipCorpus?: boolean;
}

export async function seedDemoRoom(opts?: SeedDemoRoomOptions): Promise<void> {
  const payload = buildBaseRoom();

  if (opts?.skipCorpus !== true) {
    const corpus = await ensureDemoCorpus();
    if (corpus.status === "failed") {
      throw new DemoSeedError(corpus.error ?? "unknown corpus-seed failure");
    }
    payload.corpus_ids = ["welcome"];
    payload.metadata = { demo_marker: DEMO_ROOM_MARKER };
    payload.description =
      "Auto-seeded demo room (welcome corpus + pre-baked prompt).";
  }

  await postJSON<unknown>("/council/rooms", payload);
}
