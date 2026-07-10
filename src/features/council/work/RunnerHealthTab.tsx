// F046 — Runner health tab. Sidecar lifecycle (F048) + MCP server circuit
// state (F045). The F043 ToolRunner health surfaces here once it lands.

import { useEffect, useState } from "react";
import {
  getSidecarLifecycle,
  type SidecarLifecycle,
} from "../../../lib/api/diagnostics";
import { getMcpHealth, type McpServerHealth } from "../../../lib/api/tools";

export default function RunnerHealthTab() {
  const [sidecar, setSidecar] = useState<SidecarLifecycle | null>(null);
  const [mcp, setMcp] = useState<McpServerHealth[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [s, m] = await Promise.all([
          getSidecarLifecycle(),
          getMcpHealth(),
        ]);
        if (cancelled) return;
        setSidecar(s);
        setMcp(m);
        setError(null);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    };
    load();
    const t = setInterval(load, 5000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  if (error) return <p className="work-rail-error" role="alert">{error}</p>;

  return (
    <div className="work-rail-health">
      <section>
        <h4>Sidecar</h4>
        {sidecar ? (
          <p className="work-rail-meta">
            running · v{sidecar.sidecar_version} · {sidecar.residency_mode}
          </p>
        ) : (
          <p className="work-rail-empty">checking…</p>
        )}
      </section>
      <section>
        <h4>MCP servers</h4>
        {mcp.length === 0 ? (
          <p className="work-rail-empty">None configured.</p>
        ) : (
          <ul className="work-rail-list" aria-label="MCP servers">
            {mcp.map((s) => (
              <li key={s.server_id} className="work-rail-item">
                <div className="work-rail-item-head">
                  <strong>{s.server_id}</strong>
                  <span className={`work-rail-status status-${s.circuit.state}`}>
                    {s.circuit.state}
                  </span>
                </div>
                <div className="work-rail-meta">
                  {s.reachable === null
                    ? "not probed"
                    : s.reachable
                      ? `${s.tool_count} tools`
                      : "unreachable"}
                  {s.circuit.last_failure_reason
                    ? ` · ${s.circuit.last_failure_reason}`
                    : ""}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
      <p className="work-rail-note">Tool runner (F043) health appears here when active.</p>
    </div>
  );
}
