// F001-polish — manual retry banner.
//
// Behavior:
//   - Renders a reason-specific message + a single Retry button.
//   - NO auto-fire. The user MUST click. Each click invokes onRetry.
//   - After `attempts >= max` (default 3), the Retry button is hidden and the
//     banner shows a calm cap message. NO model-switch hint is rendered.

export type RetryReason = "timeout" | "unparseable" | "server";

interface Props {
  reason: RetryReason;
  attempts: number;
  max?: number;
  onRetry: () => void;
}

function reasonCopy(reason: RetryReason): string {
  switch (reason) {
    case "timeout":
      return "The judge model took too long to respond.";
    case "unparseable":
      return "The judge response couldn't be parsed.";
    case "server":
      return "The local sidecar returned a server error.";
  }
}

export default function RetryBanner({
  reason,
  attempts,
  max = 3,
  onRetry,
}: Props) {
  const capped = attempts >= max;
  return (
    <div className="retry-banner" role="status" data-reason={reason}>
      <div className="retry-banner-message">{reasonCopy(reason)}</div>
      {capped ? (
        <div className="retry-banner-cap">
          Try again later or check your local model.
        </div>
      ) : (
        <div className="retry-banner-actions">
          <button
            type="button"
            className="retry-banner-retry"
            onClick={onRetry}
          >
            Retry
          </button>
          <span className="retry-banner-attempts">
            Attempt {attempts + 1} of {max}
          </span>
        </div>
      )}
    </div>
  );
}
