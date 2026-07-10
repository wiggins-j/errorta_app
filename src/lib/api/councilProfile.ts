// F047 — declarative Council profile import/export client.

import { getJSON, postJSON } from "../api";

export interface ProfileValidation {
  ok: boolean;
  errors: Array<{ code: string; detail: string }>;
  missing_providers: Array<{
    member_id: string;
    route_id: string | null;
    provider_class: string;
  }>;
  requested_tools: string[];
  missing_tools: string[];
  warnings: Array<{ code: string; detail: string }>;
}

export interface ProfileValidateResult {
  room: Record<string, unknown>;
  validation: ProfileValidation;
}

export interface ProfileExample {
  slug: string;
  profile: Record<string, unknown>;
  yaml: string;
}

export async function exportRoomProfile(
  roomId: string,
): Promise<{ profile: Record<string, unknown>; yaml: string }> {
  return getJSON(`/council/rooms/${encodeURIComponent(roomId)}/profile`);
}

export async function validateProfile(input: {
  profile?: Record<string, unknown>;
  yaml?: string;
}): Promise<ProfileValidateResult> {
  return postJSON<ProfileValidateResult>("/council/profiles/validate", input);
}

export async function listProfileExamples(): Promise<ProfileExample[]> {
  const body = await getJSON<{ examples: ProfileExample[] }>(
    "/council/profiles/examples",
  );
  return body.examples ?? [];
}
