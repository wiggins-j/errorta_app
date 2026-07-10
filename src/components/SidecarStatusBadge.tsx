import { useEffect, useState } from "react";
import { sidecarHealth, type SidecarHealth } from "../lib/api";

type State =
  | { kind: "loading" }
  | { kind: "ok"; health: SidecarHealth }
  | { kind: "error"; message: string };

export function SidecarStatusBadge() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    async function ping() {
      try {
        const h = await sidecarHealth();
        if (!cancelled) setState({ kind: "ok", health: h });
      } catch (e) {
        if (!cancelled) setState({ kind: "error", message: String(e) });
      }
    }
    ping();
    const id = setInterval(ping, 5000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // User-voiced labels in the badge; implementation detail (service/version/
  // build/endpoint) stays in the hover title (the "technical details" channel).
  let dotClass = "sidecar-badge-dot sidecar-badge-dot-loading";
  let label = "Connecting…";
  let title = "Pinging the local Errorta backend (sidecar) at /healthz";
  if (state.kind === "ok") {
    dotClass = "sidecar-badge-dot sidecar-badge-dot-ok";
    label = state.health.aiar_available ? "Connected · AIAR" : "Connected";
    const build = state.health.build?.commit_short
      ? ` · build ${state.health.build.commit_short}${state.health.build.dirty ? "+dirty" : ""}`
      : " · build unstamped";
    title = `sidecar ${state.health.service} ${state.health.version}${build} · ${state.health.now}`;
  } else if (state.kind === "error") {
    dotClass = "sidecar-badge-dot sidecar-badge-dot-error";
    label = "Backend offline";
    title = state.message;
  }

  return (
    <div className="sidecar-badge" title={title}>
      <span className={dotClass} aria-hidden />
      <span className="sidecar-badge-label">{label}</span>
    </div>
  );
}
