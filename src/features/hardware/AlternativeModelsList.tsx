// F002 — "Faster" and "More Capable" alternatives shown beneath the primary.
import type { ModelTier } from "./types";

export interface AlternativeModelsListProps {
  faster: ModelTier | null;
  capable: ModelTier | null;
  onPick?: (model: ModelTier) => void;
}

function Row({ model, badge, onPick }: { model: ModelTier; badge: string; onPick?: (m: ModelTier) => void }) {
  return (
    <li style={{ padding: "8px 0", borderBottom: "1px solid var(--border, #eee)" }}>
      <strong>{badge} {model.label}</strong>
      <div style={{ fontSize: 13, color: "var(--muted, #666)" }}>
        {model.compatible ? `${model.tok_label} · ${model.install_label}` : (model.incompatible_reason ?? "Incompatible")}
      </div>
      {onPick ? (
        <button type="button" onClick={() => onPick(model)} style={{ marginTop: 4 }}>
          Pick this instead
        </button>
      ) : null}
    </li>
  );
}

export function AlternativeModelsList({ faster, capable, onPick }: AlternativeModelsListProps) {
  if (!faster && !capable) return null;
  return (
    <div className="feature-pane-note">
      <h4>Other options</h4>
      <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
        {faster ? <Row model={faster} badge="Faster" onPick={onPick} /> : null}
        {capable ? <Row model={capable} badge="More capable" onPick={onPick} /> : null}
      </ul>
    </div>
  );
}
