import { useEffect, useMemo, useState } from "react";
import {
  getModelCatalog,
  getModelFamilies,
  putModelCatalog,
  putModelFamilies,
  type ModelCatalogResponse,
  type ModelFamiliesSettings,
} from "../../lib/api/settings";

export default function ModelFamilySettings() {
  const [families, setFamilies] = useState<ModelFamiliesSettings | null>(null);
  const [catalog, setCatalog] = useState<ModelCatalogResponse | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [status, setStatus] = useState("");

  useEffect(() => {
    let cancelled = false;
    Promise.all([getModelFamilies(), getModelCatalog()])
      .then(([nextFamilies, nextCatalog]) => {
        if (cancelled) return;
        setFamilies(nextFamilies);
        setCatalog(nextCatalog);
        setSelected(new Set(nextFamilies.effective));
      })
      .catch((err) => !cancelled && setStatus(err instanceof Error ? err.message : String(err)));
    return () => { cancelled = true; };
  }, []);

  const entries = useMemo(() => catalog?.entries ?? [], [catalog]);

  async function saveFamilies() {
    try {
      const next = await putModelFamilies([...selected].sort());
      setFamilies(next);
      setStatus("Model families saved");
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    }
  }

  async function overrideEntry(routeId: string, field: "capability_tier" | "cost_tier", value: string) {
    if (!catalog) return;
    const overrides = { ...catalog.overrides };
    overrides[routeId] = {
      ...(overrides[routeId] ?? {}),
      [field]: field === "cost_tier" ? Number(value) : value,
    };
    try {
      const next = await putModelCatalog(overrides);
      setCatalog(next);
      setStatus(`Saved tiers for ${routeId}`);
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    }
  }

  if (!families || !catalog) return <p className="shell-muted">Loading model policy…</p>;

  return (
    <div className="settings-model-families">
      <p className="shell-muted">
        Disabled families are blocked during assignment and again immediately before dispatch.
      </p>
      <fieldset>
        <legend>Families available to Multi-model members</legend>
        {families.configured.map((family) => (
          <label key={family} className="settings-model-family-option">
            <input
              type="checkbox"
              checked={selected.has(family)}
              onChange={(event) => {
                const next = new Set(selected);
                if (event.target.checked) next.add(family); else next.delete(family);
                setSelected(next);
              }}
            />
            {family}
          </label>
        ))}
      </fieldset>
      <div className="shell-settings-actions">
        <button type="button" onClick={saveFamilies}>Save families</button>
        <button
          type="button"
          onClick={async () => {
            const next = await putModelFamilies(null);
            setFamilies(next);
            setSelected(new Set(next.effective));
            setStatus("Using configured-provider defaults");
          }}
        >
          Use defaults
        </button>
      </div>
      <details>
        <summary>Model capability and cost tiers</summary>
        <div className="settings-model-catalog">
          {entries.map((entry) => (
            <div key={entry.route_id} className="settings-model-catalog-row">
              <span>{entry.route_id}{entry.tiers_unset ? " (tiers inferred)" : ""}</span>
              <label>
                Capability
                <select
                  value={entry.capability_tier}
                  onChange={(event) => overrideEntry(entry.route_id, "capability_tier", event.target.value)}
                >
                  <option value="light">light</option>
                  <option value="mid">mid</option>
                  <option value="strong">strong</option>
                </select>
              </label>
              <label>
                Cost
                <select
                  value={entry.cost_tier}
                  onChange={(event) => overrideEntry(entry.route_id, "cost_tier", event.target.value)}
                >
                  {[0, 1, 2, 3, 4].map((tier) => <option key={tier} value={tier}>{tier}</option>)}
                </select>
              </label>
            </div>
          ))}
        </div>
      </details>
      {status ? <p role="status" className="shell-muted">{status}</p> : null}
    </div>
  );
}
