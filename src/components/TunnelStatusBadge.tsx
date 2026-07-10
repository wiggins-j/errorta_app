// F-INFRA-12 Phase B Slice 9 — small reusable chip rendering the live tunnel
// state. Used inline in the Data residency Settings card; the floating
// bottom-left placement is added in a later slice when the badge is mounted
// in `App.tsx` alongside `SidecarStatusBadge` + `AiarPinBadge`.
//
// Visual states (driven by the discriminated union from lib/api/residency):
//   down       → muted gray dot, "Local" label
//   connecting → accent dot (with subtle pulse), "Connecting…" label
//   up         → green dot, "Tunnel: up" label
//   error      → red dot, "Tunnel: error" label, error detail in title
//
// Accessibility:
//   - role="status" + aria-live="polite" so screen readers announce state
//     transitions without being chatty.
//   - The dot itself is aria-hidden; the textual label carries the meaning.
//   - On error, the optional `detail` is exposed via `title` (mouse hover /
//     touch long-press) and is also rendered inline as a subdued span so it
//     stays discoverable for keyboard users.

import {
  normalizeTunnelState,
  type TunnelState,
} from "../lib/api/residency";

export interface TunnelStatusBadgeProps {
  state?: unknown;
}

interface Visual {
  dotClass: string;
  label: string;
}

function visualFor(state: TunnelState): Visual {
  switch (state.kind) {
    case "down":
      return {
        dotClass: "tunnel-badge-dot tunnel-badge-dot-down",
        label: "Local",
      };
    case "connecting":
      return {
        dotClass: "tunnel-badge-dot tunnel-badge-dot-connecting",
        label: "Connecting…",
      };
    case "up":
      return { dotClass: "tunnel-badge-dot tunnel-badge-dot-up", label: "Tunnel: up" };
    case "error":
      return {
        dotClass: "tunnel-badge-dot tunnel-badge-dot-error",
        label: "Tunnel: error",
      };
  }
}

export function TunnelStatusBadge({ state }: TunnelStatusBadgeProps) {
  const normalized = normalizeTunnelState(state);
  const { dotClass, label } = visualFor(normalized);
  const detail = normalized.kind === "error" ? normalized.detail : undefined;
  const title = detail
    ? `${label} — ${detail}`
    : label;
  return (
    <span
      className="tunnel-badge"
      role="status"
      aria-live="polite"
      title={title}
      data-testid="tunnel-badge"
      data-kind={normalized.kind}
    >
      <span className={dotClass} aria-hidden="true" />
      <span className="tunnel-badge-label">{label}</span>
      {detail && <span className="tunnel-badge-detail">{detail}</span>}
    </span>
  );
}

export default TunnelStatusBadge;
