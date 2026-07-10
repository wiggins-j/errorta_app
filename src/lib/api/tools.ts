// F045 — tool catalog + MCP health client.
//
// Read-only views for Settings → Tools and the F046 work rail. Invocation
// itself never happens here — it flows through the backend ToolGateway.

import { getJSON } from "../api";

export interface ToolMetadata {
  tool_id: string;
  family: string;
  egress_class: string;
  default_timeout_seconds: number;
  max_output_bytes: number;
  requires_approval: boolean;
  source_class: string;
  display_name: string;
  description: string;
  backend: string;
  server_id?: string;
}

export interface McpCircuit {
  state: string;
  consecutive_failures: number;
  failure_threshold: number;
  cooldown_seconds: number;
  last_failure_reason: string | null;
}

export interface McpServerHealth {
  server_id: string;
  configured: boolean;
  enabled: boolean;
  reachable: boolean | null;
  tool_count: number;
  circuit: McpCircuit;
}

export async function listToolCatalog(
  roomId?: string,
): Promise<ToolMetadata[]> {
  const q = roomId ? `?room_id=${encodeURIComponent(roomId)}` : "";
  const body = await getJSON<{ tools: ToolMetadata[] }>(`/tools/catalog${q}`);
  return body.tools ?? [];
}

export async function getMcpHealth(): Promise<McpServerHealth[]> {
  const body = await getJSON<{ servers: McpServerHealth[] }>(
    "/tools/mcp/health",
  );
  return body.servers ?? [];
}
