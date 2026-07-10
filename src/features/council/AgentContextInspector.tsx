import { useEffect, useState } from "react";
import {
  getAgentContextCapsule,
  listAgentContextCapsules,
  packAgentContextCapsule,
  type AgentContextCapsule,
  type AgentContextCapsuleSummary,
} from "../../lib/api/agentContext";
import { shortHash } from "./ContextManifestSections";

export default function AgentContextInspector() {
  const [capsules, setCapsules] = useState<AgentContextCapsuleSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [capsule, setCapsule] = useState<AgentContextCapsule | null>(null);
  const [micro, setMicro] = useState<string>("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listAgentContextCapsules()
      .then((items) => {
        if (cancelled) return;
        setCapsules(items);
        setSelectedId((cur) => cur ?? items[0]?.capsuleId ?? null);
      })
      .catch((err) => {
        if (!cancelled) setError(String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setCapsule(null);
    setMicro("");
    if (!selectedId) return;
    Promise.all([
      getAgentContextCapsule(selectedId),
      packAgentContextCapsule(selectedId),
    ])
      .then(([nextCapsule, nextMicro]) => {
        if (cancelled) return;
        setCapsule(nextCapsule);
        setMicro(nextMicro);
      })
      .catch((err) => {
        if (!cancelled) setError(String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  if (error) {
    return <div className="agent-context-error" role="alert">{error}</div>;
  }

  return (
    <section className="agent-context-inspector" aria-label="Agent context capsules">
      <div className="aci-head">
        <h3>Agent context</h3>
        <select
          aria-label="Capsule"
          value={selectedId ?? ""}
          onChange={(e) => setSelectedId(e.target.value || null)}
        >
          {capsules.length === 0 && <option value="">No capsules</option>}
          {capsules.map((c) => (
            <option key={c.capsuleId} value={c.capsuleId}>
              {c.taskTitle || c.capsuleId}
            </option>
          ))}
        </select>
      </div>
      <p className="aci-status">
        Status: saved capsules can be inspected and copied here. Council agents
        are not using capsules during live runs yet; the proposed workflow is to
        generate compact handoff capsules from runs and attach them to future
        agent context to reduce prompt size.
      </p>
      {!capsule && capsules.length === 0 && (
        <p className="cid-empty">No saved capsules yet.</p>
      )}
      {capsule && (
        <div className="aci-body">
          <dl className="cid-kv">
            <dt>ID</dt>
            <dd><code>{capsule.capsuleId}</code></dd>
            <dt>Kind</dt>
            <dd>{capsule.kind}</dd>
            <dt>Hash</dt>
            <dd>
              <code title={String(capsule.digest?.canonical_sha256 ?? "")}>
                {shortHash(String(capsule.digest?.canonical_sha256 ?? ""))}
              </code>
            </dd>
          </dl>
          <div className="aci-task">
            <strong>{String(capsule.task.title ?? "Untitled task")}</strong>
            <p>{String(capsule.task.intent ?? "")}</p>
          </div>
          <StateBuckets state={capsule.state} />
          <RefsTable refs={capsule.refs} />
          <button
            type="button"
            onClick={() => void navigator.clipboard?.writeText(micro)}
            disabled={!micro}
          >
            Copy micro capsule
          </button>
        </div>
      )}
    </section>
  );
}

function StateBuckets({
  state,
}: {
  state: Record<string, Array<Record<string, unknown>>>;
}) {
  const buckets = Object.entries(state ?? {}).filter(([, items]) => items.length > 0);
  if (buckets.length === 0) return null;
  return (
    <div className="aci-buckets">
      {buckets.slice(0, 4).map(([bucket, items]) => (
        <div key={bucket}>
          <h4>{bucket}</h4>
          <ul>
            {items.slice(0, 5).map((item, idx) => (
              <li key={idx}>
                <code>{String(item.id ?? "")}</code>
                {" "}
                {String(item.text ?? "")}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function RefsTable({ refs }: { refs: Array<Record<string, unknown>> }) {
  if (!refs.length) return null;
  return (
    <table className="cid-refs">
      <thead>
        <tr>
          <th>ID</th>
          <th>Class</th>
          <th>Sensitivity</th>
        </tr>
      </thead>
      <tbody>
        {refs.slice(0, 8).map((ref, idx) => (
          <tr key={idx}>
            <td><code>{String(ref.id ?? "")}</code></td>
            <td>{String(ref.class ?? ref.class_ ?? "")}</td>
            <td>{String(ref.sensitivity ?? "")}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
