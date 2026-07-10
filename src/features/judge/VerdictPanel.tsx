import type { GroundingMatch, Verdict } from "../../lib/api/judge";
import Skeleton from "./Skeleton";
import SimilarityMatchBanner from "./SimilarityMatchBanner";

interface Props {
  verdict: Verdict;
  isLoading?: boolean;
  groundingMatch?: GroundingMatch;
}

function ratingClass(rating: string): string {
  const r = (rating || "").toLowerCase();
  if (r === "pass" || r === "partial" || r === "fail") return r;
  return "unknown";
}

export default function VerdictPanel({
  verdict,
  isLoading,
  groundingMatch,
}: Props) {
  if (isLoading) {
    return (
      <div className="verdict-panel">
        <Skeleton variant="verdict" rows={3} />
      </div>
    );
  }
  const tags = verdict.failure_tags ?? [];
  const ratingLabel = verdict.rating || "unknown";
  return (
    <div
      className="verdict-panel"
      role="region"
      aria-labelledby="verdict-section-title"
    >
      <h4 id="verdict-section-title" className="visually-hidden">
        Verdict
      </h4>
      <SimilarityMatchBanner groundingMatch={groundingMatch} />
      <div className="row">
        <span
          className={`rating-badge ${ratingClass(verdict.rating)}`}
          role="img"
          aria-label={`Verdict rating: ${ratingLabel}`}
        >
          {ratingLabel}
        </span>
        {typeof verdict.confidence === "number" && (
          <span
            className="meta"
            aria-label={`Judge confidence: ${(verdict.confidence * 100).toFixed(0)} percent`}
          >
            confidence: {(verdict.confidence * 100).toFixed(0)}%
          </span>
        )}
        {typeof verdict.latency_ms === "number" && (
          <span
            className="meta"
            aria-label={`Judge took ${(verdict.latency_ms / 1000).toFixed(1)} seconds`}
          >
            latency: {verdict.latency_ms.toFixed(0)} ms
          </span>
        )}
      </div>
      {verdict.reason && <div className="reason">{verdict.reason}</div>}
      {tags.length > 0 && (
        <div className="row">
          {tags.map((t) => (
            <span key={t} className="failure-tag">
              {t}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
