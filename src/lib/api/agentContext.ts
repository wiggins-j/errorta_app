import { getJSON, postJSON } from "../api";

export interface AgentContextCapsuleSummary {
  capsuleId: string;
  kind: string;
  parentId?: string | null;
  createdAt: string;
  taskTitle?: string | null;
  canonicalSha256: string;
}

export interface AgentContextCapsule {
  capsuleId: string;
  kind: string;
  parentId?: string | null;
  createdAt: string;
  task: Record<string, unknown>;
  state: Record<string, Array<Record<string, unknown>>>;
  refs: Array<Record<string, unknown>>;
  policy: Record<string, unknown>;
  digest?: Record<string, unknown>;
}

function adaptSummary(raw: Record<string, unknown>): AgentContextCapsuleSummary {
  return {
    capsuleId: String(raw.capsule_id ?? ""),
    kind: String(raw.kind ?? ""),
    parentId: raw.parent_id == null ? null : String(raw.parent_id),
    createdAt: String(raw.created_at ?? ""),
    taskTitle: raw.task_title == null ? null : String(raw.task_title),
    canonicalSha256: String(raw.canonical_sha256 ?? ""),
  };
}

function adaptCapsule(raw: Record<string, unknown>): AgentContextCapsule {
  return {
    capsuleId: String(raw.capsule_id ?? ""),
    kind: String(raw.kind ?? ""),
    parentId: raw.parent_id == null ? null : String(raw.parent_id),
    createdAt: String(raw.created_at ?? ""),
    task: (raw.task ?? {}) as Record<string, unknown>,
    state: (raw.state ?? {}) as Record<string, Array<Record<string, unknown>>>,
    refs: (raw.refs ?? []) as Array<Record<string, unknown>>,
    policy: (raw.policy ?? {}) as Record<string, unknown>,
    digest: raw.digest != null ? (raw.digest as Record<string, unknown>) : undefined,
  };
}

export async function listAgentContextCapsules(): Promise<AgentContextCapsuleSummary[]> {
  const body = await getJSON<{ capsules: Array<Record<string, unknown>> }>(
    "/agent-context/capsules",
  );
  return (body.capsules ?? []).map(adaptSummary);
}

export async function getAgentContextCapsule(
  capsuleId: string,
): Promise<AgentContextCapsule | null> {
  try {
    const body = await getJSON<{ capsule: Record<string, unknown> }>(
      `/agent-context/capsules/${encodeURIComponent(capsuleId)}`,
    );
    return adaptCapsule(body.capsule);
  } catch (err) {
    if (err instanceof Error && err.message.includes("HTTP 404")) return null;
    throw err;
  }
}

export async function packAgentContextCapsule(capsuleId: string): Promise<string> {
  const body = await postJSON<{ text: string }>("/agent-context/pack", {
    capsule_id: capsuleId,
    resolution: "micro",
    destination_scope: "local",
    max_tokens: 1200,
    include_ref_summaries: true,
  });
  return body.text;
}
