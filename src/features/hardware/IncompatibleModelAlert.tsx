// F002 — list of models flagged incompatible with explicit numeric reason.
import type { ModelTier } from "./types";

export interface IncompatibleModelAlertProps {
  incompatible: ModelTier[];
}

export function IncompatibleModelAlert({ incompatible }: IncompatibleModelAlertProps) {
  if (incompatible.length === 0) return null;
  return (
    <details className="feature-pane-note">
      <summary>Won't run on this hardware ({incompatible.length})</summary>
      <ul style={{ marginTop: 8 }}>
        {incompatible.map((m) => (
          <li key={m.id} style={{ marginBottom: 4 }}>
            <strong>{m.label}</strong> — {m.incompatible_reason}
          </li>
        ))}
      </ul>
    </details>
  );
}
