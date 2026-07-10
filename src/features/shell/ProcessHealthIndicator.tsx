// F006 — compact process-health view used in the Settings pane and consumable
// by the sidebar's SidecarStatusBadge (see lib/api/shell.ts → processes()).
import { useEffect, useState } from "react";
import * as shellApi from "../../lib/api/shell";
import type { ManagedProcess } from "./types";

type State =
  | { kind: "loading" }
  | { kind: "ok"; processes: ManagedProcess[] }
  | { kind: "error"; message: string };

interface Props {
  /** Polling interval in ms; defaults to 5000. Set to 0 for one-shot. */
  intervalMs?: number;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function ProcessHealthIndicator({ intervalMs = 5000 }: Props) {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const r = await shellApi.processes();
        const { processes } = shellApi.normalizeProcessesResponse(r);
        if (!cancelled) setState({ kind: "ok", processes });
      } catch (e) {
        if (!cancelled) setState({ kind: "error", message: String(e) });
      }
    }
    tick();
    if (intervalMs > 0) {
      const id = setInterval(tick, intervalMs);
      return () => {
        cancelled = true;
        clearInterval(id);
      };
    }
    return () => {
      cancelled = true;
    };
  }, [intervalMs]);

  if (state.kind === "loading") {
    return <div className="shell-proc-list shell-proc-loading">checking processes…</div>;
  }
  if (state.kind === "error") {
    return (
      <div className="shell-proc-list shell-proc-error" title={state.message}>
        processes unavailable
      </div>
    );
  }
  return (
    <ul className="shell-proc-list">
      {state.processes.map((p) => (
        <li key={p.pid} className={`shell-proc shell-proc-${p.status}`}>
          <span className="shell-proc-pid">#{p.pid}</span>
          <span className="shell-proc-name">{p.name}</span>
          <span className="shell-proc-role">{p.role}</span>
          <span className="shell-proc-rss">{formatBytes(p.rss_bytes)}</span>
          <span className="shell-proc-status">{p.status}</span>
        </li>
      ))}
      {state.processes.length === 0 && <li className="shell-proc-empty">no managed processes</li>}
    </ul>
  );
}

export default ProcessHealthIndicator;
