// F002 — primary "Recommended for you" card.
import type { ModelTier } from "./types";

export interface ModelRecommendationCardProps {
  model: ModelTier;
  rationale: string;
}

export function ModelRecommendationCard({ model, rationale }: ModelRecommendationCardProps) {
  return (
    <div className="feature-pane-note" style={{ border: "1px solid var(--brand)", borderRadius: 8, padding: 16 }}>
      <h3 style={{ marginTop: 0 }}>Recommended for you: {model.label}</h3>
      <p style={{ margin: "4px 0" }}>
        {model.tok_label} · {model.vram_label} · {model.install_label}
      </p>
      <p style={{ margin: 0, fontStyle: "italic" }}>{rationale}</p>
    </div>
  );
}
