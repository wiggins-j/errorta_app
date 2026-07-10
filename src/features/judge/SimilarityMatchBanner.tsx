// F024-UI — Surface similarity-match info on verdict view.
import type { GroundingMatch } from "../../lib/api/judge";

interface Props {
  groundingMatch?: GroundingMatch;
}

function truncate(s: string, n = 80): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

export default function SimilarityMatchBanner({ groundingMatch }: Props) {
  if (!groundingMatch) return null;
  if (groundingMatch.kind !== "similar") return null;
  if (typeof groundingMatch.similarity !== "number") return null;

  const pct = (groundingMatch.similarity * 100).toFixed(0);
  const snippet = groundingMatch.original_signature
    ? truncate(groundingMatch.original_signature, 80)
    : null;

  return (
    <div className="similarity-match-banner" role="note">
      <div className="similarity-match-row">
        <span className="similarity-match-badge">{pct}% match</span>
        <span className="similarity-match-label">
          Reusing a prior correction for a similar prompt
        </span>
      </div>
      {snippet && <code className="similarity-match-snippet">{snippet}</code>}
    </div>
  );
}
