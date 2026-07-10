import type { AiarStatus } from "../../lib/api/aiarConnection";

export function AiarConnectionBadge({ status }: { status: AiarStatus | null }) {
  if (!status) return null;
  const cls = status.connected ? "aiar-status-badge ok" : "aiar-status-badge warn";
  const model = status.active_model
    ? ` - ${status.active_model}${status.active_model_ready === false ? " not ready" : " ready"}`
    : "";
  return (
    <span className={cls} title={status.error_message ?? undefined}>
      AIAR: {status.connected ? `connected on ${status.display_name}` : "needs attention"}
      {model}
    </span>
  );
}

export default AiarConnectionBadge;
