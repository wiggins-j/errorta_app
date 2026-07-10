// F033 — extended Council room API client for the room editor.
//
// The existing src/lib/api/council.ts handles list / runs / inspection;
// this module adds GET/PUT for full room schemas + validation. Room
// shape is kept as Record<string, unknown> because the schema is the
// backend's source of truth — the editor only edits fields it
// explicitly knows about and round-trips everything else verbatim.
//
// All HTTP routed through sidecarFetch so the dynamically-resolved
// sidecar port (from the Tauri sidecar_port command) is used — raw
// fetch() bypasses that and hits the webview origin, which fails in
// the bundled app on first load before the Tauri port resolves.
import { sidecarFetch } from "../api";

export interface RoomPutResponse {
  room: Record<string, unknown>;
  validation: RoomValidation;
}

export interface RoomValidation {
  status: "ready" | "draft" | "blocked_by_policy" | string;
  errors: Array<{ path?: string; code?: string; detail?: unknown }>;
}

export interface GetRoomResponse {
  room: Record<string, unknown>;
  validation: RoomValidation;
}

export async function getRoomFull(roomId: string): Promise<GetRoomResponse> {
  const res = await sidecarFetch(`/council/rooms/${encodeURIComponent(roomId)}`);
  if (!res.ok) {
    throw new Error(`GET /council/rooms/${roomId} failed (${res.status})`);
  }
  return res.json();
}

export async function putRoom(
  roomId: string,
  expectedRevision: number,
  room: Record<string, unknown>,
): Promise<RoomPutResponse> {
  const res = await sidecarFetch(`/council/rooms/${encodeURIComponent(roomId)}`, {
    method: "PUT",
    body: JSON.stringify({ expected_revision: expectedRevision, room }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(
      `PUT /council/rooms/${roomId} failed (${res.status}): ${text}`,
    );
  }
  return res.json();
}

export async function createRoom(
  room: Record<string, unknown>,
): Promise<RoomPutResponse> {
  const res = await sidecarFetch(`/council/rooms`, {
    method: "POST",
    body: JSON.stringify(room),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`POST /council/rooms failed (${res.status}): ${text}`);
  }
  return res.json();
}

export async function deleteRoom(roomId: string): Promise<void> {
  const res = await sidecarFetch(`/council/rooms/${encodeURIComponent(roomId)}`, {
    method: "DELETE",
  });
  if (!res.ok && res.status !== 404) {
    throw new Error(`DELETE /council/rooms/${roomId} failed (${res.status})`);
  }
}

// A minimal valid room a user can create and then configure in the editor.
// Two local Ollama members on round-robin; the user picks real routes/models
// and tweaks settings before running. Marked `draft` until validated.
export function buildBlankRoom(name: string): Record<string, unknown> {
  const now = new Date().toISOString();
  const member = (idx: number) => ({
    id: `m-${idx}`,
    name: `Member ${idx}`,
    role: "member",
    enabled: true,
    gateway_route_id: "local.ollama.llama3.2:3b",
    provider_kind: "local",
    provider_display: "Ollama",
    model_display: "llama3.2:3b",
    catalog_version: null,
    context_access: "prompt_only",
    transcript_access: "all_messages",
    turn_limits: {
      max_messages: 1,
      max_input_tokens: 8192,
      max_output_tokens: 2048,
      max_context_tokens: 8192,
    },
    generation: { temperature: 0.7, top_p: null, seed: null },
    system_prompt: "",
    metadata: {},
  });
  return {
    format_version: 1,
    id: `room-${Date.now()}`,
    name: name.trim() || "New room",
    description: "",
    preset_id: null,
    status_hint: "draft",
    members: [member(1), member(2)],
    corpus_ids: [],
    topology: {
      kind: "round_robin",
      max_rounds: 1,
      max_messages_per_member: 1,
      max_total_turns: 2,
      speaker_order: ["m-1", "m-2"],
      stop_condition: null,
    },
    context_policy: {
      default_context_access: "prompt_only",
      default_transcript_access: "all_messages",
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
      max_input_tokens_per_turn: 8192,
      max_output_tokens_per_turn: 2048,
      max_context_tokens_per_member: 8192,
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
    created_at: now,
    updated_at: now,
    last_validated_at: null,
    revision: 1,
  };
}

export async function validateRoom(
  roomId: string,
): Promise<RoomValidation> {
  const res = await sidecarFetch(
    `/council/rooms/${encodeURIComponent(roomId)}/validate`,
    { method: "POST" },
  );
  if (!res.ok) {
    throw new Error(
      `POST /council/rooms/${roomId}/validate failed (${res.status})`,
    );
  }
  return res.json();
}
