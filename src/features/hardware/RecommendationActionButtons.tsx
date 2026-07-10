// F002 — "Use Recommended" and "Choose Differently" CTAs.
import type { ModelTier } from "./types";

export interface RecommendationActionButtonsProps {
  primary: ModelTier;
  allModels: ModelTier[];
  selectedId: string;
  onSelect: (id: string) => void;
  onUseSelected: (model: ModelTier) => void;
}

export function RecommendationActionButtons({
  primary,
  allModels,
  selectedId,
  onSelect,
  onUseSelected,
}: RecommendationActionButtonsProps) {
  const selected = allModels.find((m) => m.id === selectedId) ?? primary;
  return (
    <div className="feature-pane-note" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <label htmlFor="model-picker">Override:</label>
        <select
          id="model-picker"
          value={selectedId}
          onChange={(e) => onSelect(e.target.value)}
        >
          {allModels.map((m) => (
            <option key={m.id} value={m.id} disabled={!m.compatible}>
              {m.label}
              {m.compatible ? "" : " (incompatible)"}
            </option>
          ))}
        </select>
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <button type="button" onClick={() => onUseSelected(selected)} disabled={!selected.compatible}>
          Use {selected.id === primary.id ? "Recommended" : "Selected"}
        </button>
        <button type="button" onClick={() => onSelect(primary.id)}>
          Reset to recommended
        </button>
      </div>
      <p style={{ fontSize: 12, color: "var(--muted, #666)", margin: 0 }}>
        We&rsquo;ll download this model in the next step (Connect your AI), with
        progress shown. Choosing here just records your pick.
      </p>
    </div>
  );
}
