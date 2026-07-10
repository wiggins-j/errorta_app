// F046 — the work rail only appears for tool-heavy / policy-gated rooms.
// Plain Council rooms (no tools, no child runs, no escalation) show no rail.

export function roomUsesWorkRail(room: Record<string, unknown> | null): boolean {
  if (!room) return false;
  const toolPolicy = room.tool_policy as Record<string, unknown> | undefined;
  if (toolPolicy) {
    for (const family of [
      "web_fetch",
      "web_search",
      "code_read",
      "code_write",
      "code_exec",
    ]) {
      const sub = toolPolicy[family] as Record<string, unknown> | undefined;
      if (sub && sub.enabled === true) return true;
    }
    const explicit = toolPolicy.enabled_tool_ids;
    if (Array.isArray(explicit) && explicit.length > 0) return true;
  }
  const childPolicy = room.child_run_policy as Record<string, unknown> | undefined;
  if (childPolicy && childPolicy.enabled === true) return true;
  const esc = room.escalation_policy as Record<string, unknown> | undefined;
  if (esc && esc.enabled === true) return true;
  return false;
}
