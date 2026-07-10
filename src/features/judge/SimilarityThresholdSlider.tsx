// F024-UI — client-side preference for grounding similarity threshold.
//
// NOTE: This is a display/preference only in v0.3. The server still reads its
// threshold exclusively from the ERRORTA_GROUNDING_SIMILARITY environment
// variable until a runtime endpoint lands. The value persisted here is not
// transmitted to the sidecar.
import { useEffect, useState } from "react";

export const SIMILARITY_THRESHOLD_STORAGE_KEY =
  "errorta_grounding_similarity_threshold";
export const SIMILARITY_MIN = 0.7;
export const SIMILARITY_MAX = 0.99;
export const SIMILARITY_DEFAULT = 0.85;
export const SIMILARITY_STEP = 0.01;

function clamp(n: number): number {
  if (Number.isNaN(n)) return SIMILARITY_DEFAULT;
  return Math.min(SIMILARITY_MAX, Math.max(SIMILARITY_MIN, n));
}

function readStored(): number {
  try {
    const raw = localStorage.getItem(SIMILARITY_THRESHOLD_STORAGE_KEY);
    if (raw === null) return SIMILARITY_DEFAULT;
    const parsed = parseFloat(raw);
    return clamp(parsed);
  } catch {
    return SIMILARITY_DEFAULT;
  }
}

function writeStored(v: number): void {
  try {
    localStorage.setItem(SIMILARITY_THRESHOLD_STORAGE_KEY, String(clamp(v)));
  } catch {
    // localStorage unavailable — silently degrade.
  }
}

export default function SimilarityThresholdSlider() {
  const [value, setValue] = useState<number>(SIMILARITY_DEFAULT);

  useEffect(() => {
    setValue(readStored());
  }, []);

  const onChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const next = clamp(parseFloat(e.target.value));
    setValue(next);
    writeStored(next);
  };

  return (
    <div className="similarity-threshold-slider">
      <label htmlFor="similarity-threshold">
        Similarity threshold:{" "}
        <span className="similarity-threshold-value">
          {(value * 100).toFixed(0)}%
        </span>
      </label>
      <input
        id="similarity-threshold"
        type="range"
        min={SIMILARITY_MIN}
        max={SIMILARITY_MAX}
        step={SIMILARITY_STEP}
        value={value}
        onChange={onChange}
      />
      <p className="similarity-threshold-caveat">
        This sets how close a match has to be before a prior correction is
        reused. It applies to this view now; changing it for every run is coming
        soon.
      </p>
      <details className="tech-details">
        <summary>Technical details</summary>
        <p>
          This is a client-side preference. The server-side threshold is read
          from the <code>ERRORTA_GROUNDING_SIMILARITY</code> environment variable
          until a runtime endpoint lands.
        </p>
      </details>
    </div>
  );
}
